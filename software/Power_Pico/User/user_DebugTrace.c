/*
 * Debug build only: a crash record that survives a reset, a PD event log sent
 * live over the USB stream, a tick watchdog, and traps that reset instead of
 * freezing. See user_DebugTrace.h.
 */
#include "user_DebugTrace.h"

#include "main.h"
#include "FreeRTOS.h"
#include "task.h"
#include "cmsis_os.h"
#include "user_TasksInit.h"

#include <string.h>

#define DBG_REC_ADDR    0x2001E000UL
#define DBG_RAM_START   0x20000000UL
#define DBG_RAM_END     0x20020000UL
#define DBG_MAGIC       0x44424731UL /* "DBG1" */
#define DBG_CRASH_VALID 0x43525348UL /* "CRSH" */
#define DBG_RING        256U

#define DBG_FRAME_EVENTS     0x01U
#define DBG_FRAME_SUMMARY    0x02U
#define DBG_FRAME_CRASH_A    0x03U
#define DBG_FRAME_CRASH_B    0x04U
#define DBG_FRAME_CRASH_C    0x05U
#define DBG_FRAME_CRASH_RING 0x06U

typedef struct {
    uint32_t t_us;
    uint8_t kind;
    uint8_t a;
    uint16_t b;
    uint32_t c;
} dbg_evt_t;

typedef struct {
    uint32_t magic;
    uint32_t magic_inv;
    uint32_t boot_count;
    uint32_t crash_valid;
    uint32_t crash_kind;
    uint32_t crash_t_us;
    char task[16];
    uint32_t frame[8]; /* r0 r1 r2 r3 r12 lr pc xpsr */
    uint32_t cfsr;
    uint32_t hfsr;
    uint32_t mmfar;
    uint32_t bfar;
    uint32_t exc_return;
    uint32_t sp;
    uint32_t basepri;
    uint32_t primask;
    uint32_t line;
    char file[24];
    uint32_t head; /* events written since boot */
    dbg_evt_t ring[DBG_RING];
} dbg_rec_t;

_Static_assert(sizeof(dbg_evt_t) == 12U, "event layout");
_Static_assert(DBG_REC_ADDR + sizeof(dbg_rec_t) <= DBG_RAM_END, "record outside RAM");

static dbg_rec_t *const rec = (dbg_rec_t *)DBG_REC_ADDR;

extern osThreadId_t KeyTaskHandle;
extern osThreadId_t PDUFPTaskHandle;
extern osThreadId_t LvHandlerTaskHandle;
extern osThreadId_t defaultTaskHandle;

static dbg_rec_t s_prev;
static uint8_t s_prev_valid;
static uint32_t s_boot_csr;
static uint32_t s_live_sent;
static uint32_t s_live_dropped;
static uint8_t s_seq;
static uint8_t s_started;
static uint32_t s_last_summary_ms;
static uint32_t s_last_stack_ms;
static uint32_t s_last_report_ms;
static uint32_t s_report_pos;

static volatile uint32_t s_ticks;
static volatile uint32_t s_beats;
static uint32_t s_wd_last_ticks;
static uint32_t s_wd_last_beats;
static uint32_t s_wd_tick_same;
static uint32_t s_wd_beat_same;

static int in_ram(const void *p, uint32_t len)
{
    uint32_t a = (uint32_t)p;
    return a >= DBG_RAM_START && a + len <= DBG_RAM_END && a + len >= a;
}

static void copy_str(char *dst, uint32_t size, const char *src)
{
    uint32_t i = 0U;

    if (src != NULL) {
        for (; i + 1U < size; i++) {
            if (!in_ram(&src[i], 1U) && !((uint32_t)&src[i] >= 0x08000000UL
                                          && (uint32_t)&src[i] < 0x08080000UL)) {
                break;
            }
            if (src[i] == '\0') {
                break;
            }
            dst[i] = src[i];
        }
    }
    for (; i < size; i++) {
        dst[i] = '\0';
    }
}

void dbg_boot(void)
{
    s_boot_csr = RCC->CSR;
    RCC->CSR |= RCC_CSR_RMVF;
    if (rec->magic != DBG_MAGIC || rec->magic_inv != ~DBG_MAGIC) {
        memset(rec, 0, sizeof(*rec));
        rec->magic = DBG_MAGIC;
        rec->magic_inv = ~DBG_MAGIC;
    }
    rec->boot_count++;
    if (rec->crash_valid == DBG_CRASH_VALID) {
        memcpy(&s_prev, rec, sizeof(s_prev));
        s_prev_valid = 1U;
    }
    rec->crash_valid = 0U;
    rec->crash_kind = 0U;
    rec->head = 0U;
}

void dbg_log(uint8_t kind, uint8_t a, uint16_t b, uint32_t c)
{
    UBaseType_t saved = taskENTER_CRITICAL_FROM_ISR();
    dbg_evt_t *e = &rec->ring[rec->head % DBG_RING];

    e->t_us = TIM5->CNT;
    e->kind = kind;
    e->a = a;
    e->b = b;
    e->c = c;
    rec->head++;
    taskEXIT_CRITICAL_FROM_ISR(saved);
}

static int send_frame(uint8_t type, const void *payload, uint32_t len)
{
    CmdTxFrame_t f;

    if (CmdTxQueue == NULL || MessageSendTaskHandle == NULL || len + 5U > sizeof(f.data)) {
        return 0;
    }
    f.data[0] = 0xAAU;
    f.data[1] = 0x5DU;
    f.data[2] = (uint8_t)(len + 5U);
    f.data[3] = type;
    f.data[4] = s_seq;
    memcpy(&f.data[5], payload, len);
    f.length = (uint16_t)(len + 5U);
    if (osMessageQueuePut(CmdTxQueue, &f, 0U, 0U) != osOK) {
        return 0;
    }
    s_seq++;
    osThreadFlagsSet(MessageSendTaskHandle, FLAG_CMD_TX_READY);
    return 1;
}

static void put32(uint8_t *p, uint32_t v)
{
    memcpy(p, &v, 4U);
}

static int send_summary(void)
{
    uint8_t p[40];

    put32(&p[0], rec->boot_count);
    put32(&p[4], s_boot_csr);
    p[8] = s_prev_valid;
    p[9] = s_prev_valid ? (uint8_t)s_prev.crash_kind : 0U;
    p[10] = 0U;
    p[11] = 0U;
    put32(&p[12], s_prev_valid ? s_prev.boot_count : 0U);
    put32(&p[16], TIM5->CNT);
    put32(&p[20], rec->head);
    put32(&p[24], s_live_dropped);
    put32(&p[28], HAL_GetTick());
    put32(&p[32], (uint32_t)xPortGetFreeHeapSize());
    put32(&p[36], (uint32_t)xPortGetMinimumEverFreeHeapSize());
    return send_frame(DBG_FRAME_SUMMARY, p, sizeof(p));
}

static int send_report_frame(uint32_t pos, uint32_t *done)
{
    uint8_t p[52];
    uint32_t count = s_prev.head < DBG_RING ? s_prev.head : DBG_RING;
    uint32_t chunks = (count + 3U) / 4U;

    *done = 0U;
    if (pos == 1U) {
        put32(&p[0], s_prev.crash_kind);
        put32(&p[4], s_prev.crash_t_us);
        memcpy(&p[8], s_prev.task, 16U);
        put32(&p[24], s_prev.line);
        put32(&p[28], s_prev.basepri);
        put32(&p[32], s_prev.primask);
        put32(&p[36], s_prev.head);
        return send_frame(DBG_FRAME_CRASH_A, p, 40U);
    }
    if (pos == 2U) {
        memcpy(&p[0], s_prev.frame, 32U);
        put32(&p[32], s_prev.cfsr);
        put32(&p[36], s_prev.hfsr);
        put32(&p[40], s_prev.mmfar);
        put32(&p[44], s_prev.bfar);
        return send_frame(DBG_FRAME_CRASH_B, p, 48U);
    }
    if (pos == 3U) {
        put32(&p[0], s_prev.exc_return);
        put32(&p[4], s_prev.sp);
        memcpy(&p[8], s_prev.file, 24U);
        if (chunks == 0U) {
            *done = 1U;
        }
        return send_frame(DBG_FRAME_CRASH_C, p, 32U);
    }
    {
        uint32_t chunk = pos - 4U;
        uint32_t first = chunk * 4U;
        uint32_t n = count - first < 4U ? count - first : 4U;
        uint32_t start = s_prev.head - count; /* oldest event kept */
        uint32_t i;

        p[0] = (uint8_t)(first & 0xFFU);
        p[1] = (uint8_t)(first >> 8);
        p[2] = (uint8_t)n;
        for (i = 0U; i < n; i++) {
            memcpy(&p[3U + 12U * i], &s_prev.ring[(start + first + i) % DBG_RING], 12U);
        }
        if (chunk + 1U >= chunks) {
            *done = 1U;
        }
        return send_frame(DBG_FRAME_CRASH_RING, p, 3U + 12U * n);
    }
}

void dbg_service(void)
{
    uint32_t now = HAL_GetTick();
    int frames;

    for (frames = 0; frames < 2; frames++) {
        uint8_t buf[1U + 4U * 12U];
        uint32_t head = rec->head;
        uint32_t n;
        uint32_t i;
        UBaseType_t saved;

        if (head - s_live_sent > DBG_RING) {
            s_live_dropped += head - s_live_sent - DBG_RING;
            s_live_sent = head - DBG_RING;
        }
        n = head - s_live_sent;
        if (n == 0U) {
            break;
        }
        if (n > 4U) {
            n = 4U;
        }
        buf[0] = (uint8_t)n;
        saved = taskENTER_CRITICAL_FROM_ISR();
        for (i = 0U; i < n; i++) {
            memcpy(&buf[1U + 12U * i], &rec->ring[(s_live_sent + i) % DBG_RING], 12U);
        }
        taskEXIT_CRITICAL_FROM_ISR(saved);
        if (!send_frame(DBG_FRAME_EVENTS, buf, 1U + 12U * n)) {
            break;
        }
        s_live_sent += n;
    }

    if (now - s_last_stack_ms >= 5000U) {
        osThreadId_t tasks[6];
        uint8_t i;

        s_last_stack_ms = now;
        tasks[0] = MessageReceiveTaskHandle;
        tasks[1] = MessageSendTaskHandle;
        tasks[2] = KeyTaskHandle;
        tasks[3] = PDUFPTaskHandle;
        tasks[4] = LvHandlerTaskHandle;
        tasks[5] = defaultTaskHandle;
        for (i = 0U; i < 6U; i++) {
            if (tasks[i] != NULL) {
                dbg_log(DBG_EV_STACK_FREE, i,
                        (uint16_t)uxTaskGetStackHighWaterMark((TaskHandle_t)tasks[i]), 0U);
            }
        }
    }

    if (now - s_last_summary_ms >= 5000U && send_summary()) {
        s_last_summary_ms = now;
    }

    if (s_prev_valid) {
        if (s_report_pos == 0U && now - s_last_report_ms >= 10000U) {
            s_report_pos = 1U;
            s_last_report_ms = now;
        }
        while (s_report_pos != 0U) {
            uint32_t done;

            if (!send_report_frame(s_report_pos, &done)) {
                break;
            }
            s_report_pos = done ? 0U : s_report_pos + 1U;
        }
    }
}

void dbg_watchdog_start(void)
{
    uint32_t pclk1 = HAL_RCC_GetPCLK1Freq();
    uint32_t timclk = ((RCC->CFGR & RCC_CFGR_PPRE1) == RCC_CFGR_PPRE1_DIV1) ? pclk1 : 2U * pclk1;

    if (s_started) {
        return;
    }
    s_started = 1U;
    dbg_log(DBG_EV_BOOT, s_prev_valid ? (uint8_t)s_prev.crash_kind : 0U,
            (uint16_t)rec->boot_count, s_boot_csr);
    __HAL_RCC_TIM3_CLK_ENABLE();
    TIM3->CR1 = 0U;
    TIM3->PSC = timclk / 10000U - 1U; /* 10 kHz */
    TIM3->ARR = 1000U - 1U;           /* 100 ms */
    TIM3->EGR = TIM_EGR_UG;
    TIM3->SR = 0U;
    TIM3->DIER = TIM_DIER_UIE;
    /* Above configMAX_SYSCALL_INTERRUPT_PRIORITY (5), so FreeRTOS critical
       sections do not mask it. It calls no FreeRTOS function. */
    HAL_NVIC_SetPriority(TIM3_IRQn, 2U, 0U);
    HAL_NVIC_EnableIRQ(TIM3_IRQn);
    TIM3->CR1 = TIM_CR1_CEN;
}

void dbg_tick(void)
{
    s_ticks++;
}

void dbg_stream_beat(void)
{
    s_beats++;
}

static void __attribute__((noreturn)) dbg_crash(uint32_t kind, const uint32_t *frame,
                                                uint32_t exc_return, const char *name,
                                                uint32_t pc)
{
    uint32_t primask = __get_PRIMASK();
    uint32_t basepri = __get_BASEPRI();
    uint32_t i;

    __disable_irq();
    /* Open the CC switch first, as PD_CMD_STOP does: the charger then drops
       VBUS instead of holding 5 V on the output. */
    GPIOB->BSRR = (uint32_t)GPIO_PIN_10 << 16U;

    rec->crash_kind = kind;
    rec->crash_t_us = TIM5->CNT;
    rec->exc_return = exc_return;
    rec->sp = (uint32_t)frame;
    rec->basepri = basepri;
    rec->primask = primask;
    for (i = 0U; i < 8U; i++) {
        rec->frame[i] = (frame != NULL && in_ram(frame, 32U)) ? frame[i] : 0U;
    }
    if (pc != 0U) {
        rec->frame[6] = pc;
    }
    rec->cfsr = SCB->CFSR;
    rec->hfsr = SCB->HFSR;
    rec->mmfar = SCB->MMFAR;
    rec->bfar = SCB->BFAR;
    if (name != NULL) {
        copy_str(rec->task, sizeof(rec->task), name);
    } else {
        TaskHandle_t h = xTaskGetCurrentTaskHandle();

        if (h != NULL && in_ram(h, 4U)) {
            copy_str(rec->task, sizeof(rec->task), pcTaskGetName(h));
        } else {
            copy_str(rec->task, sizeof(rec->task), "?");
        }
    }
    rec->crash_valid = DBG_CRASH_VALID;
    __DSB();
    NVIC_SystemReset();
    for (;;) {
    }
}

void dbg_stack_overflow(void *task, const char *name)
{
    (void)task;
    rec->line = 0U;
    copy_str(rec->file, sizeof(rec->file), "");
    dbg_crash(DBG_CRASH_STACK_OVERFLOW, NULL, 0U, name,
              (uint32_t)__builtin_return_address(0));
}

void dbg_assert(const char *file, int line)
{
    const char *tail = file;
    uint32_t n = 0U;

    if (file != NULL) {
        while (file[n] != '\0' && n < 256U) {
            n++;
        }
        tail = n > 23U ? &file[n - 23U] : file;
    }
    rec->line = (uint32_t)line;
    copy_str(rec->file, sizeof(rec->file), tail);
    dbg_crash(DBG_CRASH_ASSERT, NULL, 0U, NULL, (uint32_t)__builtin_return_address(0));
}

void dbg_error_handler(void *caller)
{
    rec->line = 0U;
    copy_str(rec->file, sizeof(rec->file), "");
    dbg_crash(DBG_CRASH_ERROR_HANDLER, NULL, 0U, NULL, (uint32_t)caller);
}

void dbg_fault(uint32_t *frame, uint32_t exc_return)
{
    rec->line = 0U;
    copy_str(rec->file, sizeof(rec->file), "");
    dbg_crash(DBG_CRASH_FAULT, frame, exc_return, NULL, 0U);
}

void dbg_watchdog_irq(uint32_t *frame, uint32_t exc_return)
{
    uint32_t t = s_ticks;
    uint32_t b = s_beats;

    TIM3->SR = 0U;
    if (t == s_wd_last_ticks) {
        s_wd_tick_same++;
    } else {
        s_wd_tick_same = 0U;
        s_wd_last_ticks = t;
    }
    if (b != 0U && b == s_wd_last_beats) {
        s_wd_beat_same++;
    } else {
        s_wd_beat_same = 0U;
        s_wd_last_beats = b;
    }
    if (t != 0U && s_wd_tick_same >= 10U) {          /* no RTOS tick for 1 s */
        rec->line = 0U;
        copy_str(rec->file, sizeof(rec->file), "");
        dbg_crash(DBG_CRASH_TICK_STALLED, frame, exc_return, NULL, 0U);
    }
    if (s_wd_beat_same >= 20U) {                     /* stream task stopped 2 s */
        rec->line = 0U;
        copy_str(rec->file, sizeof(rec->file), "");
        dbg_crash(DBG_CRASH_SEND_STALLED, frame, exc_return, NULL, 0U);
    }
}
