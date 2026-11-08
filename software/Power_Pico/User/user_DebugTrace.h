#ifndef __USER_DEBUGTRACE_H__
#define __USER_DEBUGTRACE_H__

/*
 * Debug build only: a crash record and a PD event log.
 *
 * The record lives at a fixed RAM address above the application's stack top
 * (0x2001D820) and above the bootloader's RAM (below 0x20003B18), which the
 * startup code does not clear, so it survives the reset that every trap below
 * ends in. Each trap opens the CC switch first, so the charger drops VBUS.
 *
 * Every event is also sent live over the USB stream as a frame
 * AA 5D <len> <type> <seq> <payload>, next to the 81-byte AA 55 ADC packets.
 */

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* event kinds */
#define DBG_EV_SET_PPS       0x01U  /* a = current (50 mA), b = voltage (20 mV) */
#define DBG_EV_PD_START      0x02U
#define DBG_EV_PD_STOP       0x03U
#define DBG_EV_TX_REQUEST    0x04U  /* a = 0 flagged, 1 keep-alive, 2 answer to caps; b = header, c = RDO */
#define DBG_EV_TX_OTHER      0x05U  /* a = 0 Get_Source_Cap, 1 response; b = header, c = obj0 */
#define DBG_EV_RX            0x06U  /* b = header, c = obj0 */
#define DBG_EV_PSRDY_TIMEOUT 0x07U  /* b = ms waited */
#define DBG_EV_PSRDY_DONE    0x08U  /* a = PDO type, b = PPS voltage now stored, c = selected PDO */
#define DBG_EV_HARDRST_RX    0x09U
#define DBG_EV_HARDRST_TX    0x0AU
#define DBG_EV_ATTACH        0x0BU  /* a = cc */
#define DBG_EV_DETACH        0x0CU
#define DBG_EV_FUSB_STATUS   0x0DU  /* a = INTERRUPTA, b = INTERRUPTB << 8 | INTERRUPT, c = STATUS0A..STATUS1 */
#define DBG_EV_KEY           0x0EU  /* a = key id, b = event type */
#define DBG_EV_STACK_FREE    0x0FU  /* a = task index, b = free stack words */
#define DBG_EV_BOOT          0x10U  /* b = boot count, c = RCC CSR at boot */
#define DBG_EV_LIVE_DROPPED  0x11U  /* c = events not sent live */

/* crash kinds */
#define DBG_CRASH_STACK_OVERFLOW 1U
#define DBG_CRASH_ASSERT         2U
#define DBG_CRASH_FAULT          3U
#define DBG_CRASH_ERROR_HANDLER  4U
#define DBG_CRASH_TICK_STALLED   5U
#define DBG_CRASH_SEND_STALLED   6U

void dbg_boot(void);
void dbg_log(uint8_t kind, uint8_t a, uint16_t b, uint32_t c);
void dbg_service(void);
void dbg_watchdog_start(void);
void dbg_tick(void);
void dbg_stream_beat(void);

void dbg_stack_overflow(void *task, const char *name) __attribute__((noreturn));
void dbg_assert(const char *file, int line) __attribute__((noreturn));
void dbg_error_handler(void *caller) __attribute__((noreturn));
void dbg_fault(uint32_t *frame, uint32_t exc_return) __attribute__((noreturn));
void dbg_watchdog_irq(uint32_t *frame, uint32_t exc_return);

#ifdef __cplusplus
}
#endif

#endif
