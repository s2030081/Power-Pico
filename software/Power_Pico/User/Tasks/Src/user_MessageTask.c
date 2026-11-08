/* Private includes -----------------------------------------------------------*/
//includes
#include "adc.h"
#include "user_AdcDataStrategy.h"
#include "user_CmdStrategy.h"
#include "user_TasksInit.h"
#include "user_DebugTrace.h"
#include "user_MessageTask.h"
#include "usb_device.h"
#include "BL24C02.h"
#include "usbd_cdc_if.h"

#include <stdbool.h>
#include <string.h>

/* Private typedef -----------------------------------------------------------*/

/* Private define ------------------------------------------------------------*/

/* Private variables ---------------------------------------------------------*/

/* UI 刷新周期（按时间推导分频，避免依赖固定采样块大小） */
#define UI_UPDATE_PERIOD_MS 25U
#define UI_UPDATE_DIV ((UI_UPDATE_PERIOD_MS + ADC_CHUNK_PERIOD_MS - 1U) / ADC_CHUNK_PERIOD_MS)

/* Private function prototypes -----------------------------------------------*/


/**
  * @brief  task for receiving messages, such as USB update requests.
  * @param  argument: Not used
  * @retval None
  */
void MessageReceiveTask(void *argument)
{
    while (!user_hardware_ready) osDelay(1);
	uint32_t flags;
    CmdRxChunk_t chunk;
	(void)argument;
	CmdStrategy_Init();
	while(1)
	{
    flags = osThreadFlagsWait(FLAG_USB_UPDATE_REQ | FLAG_CMD_RX_READY,
                              osFlagsWaitAny,
                              5U);

    if ((flags & 0x80000000U) == 0U && (flags & FLAG_USB_UPDATE_REQ) != 0U) {
      EEPROM_UpdateCommand_Write(true);
      HAL_Delay(100);
      USER_USB_DEVICE_DeInit();
      // 给予PC足够的时间来识别设备断开
      HAL_Delay(500);
      NVIC_SystemReset();
    }
    if (CmdRxQueue != NULL) {
      /* Bound each batch so continuous host traffic cannot starve LVGL. */
      uint8_t drained = 0U;
      while (drained < CMD_RX_QUEUE_DEPTH &&
             osMessageQueueGet(CmdRxQueue, &chunk, NULL, 0U) == osOK) {
        CDC_ResumeReceive_FS();
        CmdStrategy_ProcessRx(chunk.data, chunk.length);
        drained++;
      }
    }
    CDC_ResumeReceive_FS();
    osDelay(5);
	}
}

/* Measurement packets wait here while USB is busy. Each turn sends a waiting
 * command reply (or debug frame) and every waiting packet in one transfer,
 * reply first, so a reply never costs a packet. Four packets ride out 4 ms of
 * USB stall; after that the oldest is dropped. */
#define ADC_BACKLOG 4U

static USB_ADC_Packet_t adc_backlog[ADC_BACKLOG];
static uint32_t adc_backlog_first, adc_backlog_count;
static uint8_t usb_tx_frame[CMD_TX_FRAME_SIZE + ADC_BACKLOG * sizeof(USB_ADC_Packet_t)];

static void adc_backlog_push(const USB_ADC_Packet_t *packet)
{
  if (packet == NULL) return;
  if (adc_backlog_count == ADC_BACKLOG) {
    adc_backlog_first = (adc_backlog_first + 1U) % ADC_BACKLOG;
    adc_backlog_count--;
  }
  memcpy(&adc_backlog[(adc_backlog_first + adc_backlog_count) % ADC_BACKLOG],
         packet, sizeof(*packet));
  adc_backlog_count++;
}

/**
 * @brief  send adc data to PC via USB,
 * @param  argument: Not used
 */
void MessageSendTask(void *argument)
{
  while (!user_hardware_ready) osDelay(1);
  uint32_t flags;
  uint32_t ui_div_cnt = 0;
  CmdTxFrame_t response;
  bool pending_cmd_valid = false;
  bool half_next = true;  /* the DMA fills the halves in turn */
  while (1)
  {
    dbg_stream_beat();  /* the stream watchdog (user_DebugTrace.c) */

    // Wake for ADC chunks or a queued command response.
    flags = osThreadFlagsWait(FLAG_ADC_HALF_READY | FLAG_ADC_FULL_READY |
                              FLAG_CMD_TX_READY,
                              osFlagsWaitAny,
                              osWaitForever);

    if (flags & 0x80000000) {
        continue;
    }

    /* Oldest half first when a late wake finds both ready. */
    bool first_half = half_next;
    for (uint32_t turn = 0U; turn < 2U; turn++) {
      bool half = (turn == 0U) == first_half;
      uint32_t flag = half ? FLAG_ADC_HALF_READY : FLAG_ADC_FULL_READY;
      if ((flags & flag) == 0U) {
        continue;
      }
      flags &= ~flag;
      adc_backlog_push(half ? Process_ADC_Chunk(&adc_raw_buffer[0][0], 0)
                            : Process_ADC_Chunk(&adc_raw_buffer[ADC_TIMES][0], 1));
      half_next = !half;
    }

    if (!pending_cmd_valid && CmdTxQueue != NULL &&
        osMessageQueueGet(CmdTxQueue, &response, NULL, 0U) == osOK) {
        pending_cmd_valid = true;
    }
    uint16_t length = 0U;
    if (pending_cmd_valid) {
      memcpy(usb_tx_frame, response.data, response.length);
      length = response.length;
    }
    for (uint32_t i = 0U; i < adc_backlog_count; i++) {
      memcpy(&usb_tx_frame[length],
             &adc_backlog[(adc_backlog_first + i) % ADC_BACKLOG],
             sizeof(USB_ADC_Packet_t));
      length += sizeof(USB_ADC_Packet_t);
    }
    if (length != 0U && CDC_Transmit_FS(usb_tx_frame, length) == USBD_OK) {
      pending_cmd_valid = false;
      adc_backlog_first = 0U;
      adc_backlog_count = 0U;
    }

    // Publish UI data after UI_UPDATE_DIV task wakeups.
    if (++ui_div_cnt >= UI_UPDATE_DIV)
    {
        ui_div_cnt = 0;

        PowerData_t newData;
        Data_Monitor_Get_Values(&newData.voltage, &newData.current);

        osStatus_t status = osMessageQueuePut(PowerDataQueue, &newData, 0, 0);
        if (status == osErrorResource) {
            PowerData_t dummy;
            // 队列满：丢弃旧数据，写入新数据
            osMessageQueueGet(PowerDataQueue, &dummy, NULL, 0);
            osMessageQueuePut(PowerDataQueue, &newData, 0, 0);
        }
    }
  }
}
