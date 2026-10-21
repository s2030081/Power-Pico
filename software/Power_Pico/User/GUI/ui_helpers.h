// LVGL VERSION: 9.2


#ifndef _UI_HELPERS_H
#define _UI_HELPERS_H

#ifdef __cplusplus
extern "C" {
#endif

#include "lvgl.h"
#include "stdio.h"

///////////////////// ui variables ////////////////////

extern float ui_current_voltage;
extern float ui_current_current;

///////////////////// help functions ////////////////////

void ui_full_screen_refresh(lv_obj_t * screen);

///////////////////// value formatting ////////////////////
// Shared by every page that shows a measurement, so the PD pages print the
// same digits as the main page.

// 4 significant digits: 0.000 / 5.000 / 20.00 / 123.4 / 1234
void ui_fmt_sig4(char *buf, size_t size, float v);
// current given in uA, auto-ranged to uA / mA / A; *unit receives the unit text
void ui_fmt_current(char *buf, size_t size, float current_uA, const char **unit);
// power given in W, auto-ranged to uW / mW / W; *unit receives the unit text
void ui_fmt_power(char *buf, size_t size, float power_W, const char **unit);

///////////// interface for system settings ////////////

// get functions

uint64_t ui_GetElaspseMicroseconds(void);
void ui_GetElapsedTime_HMS(uint8_t *hours, uint8_t *minutes, uint8_t *seconds);
uint8_t ui_get_back_light_level(void);
bool ui_get_key_sound_enable(void);
uint8_t ui_get_language_select(void);
uint16_t ui_get_display_rotation(void);
uint8_t ui_get_current_range_mode(void);
void ui_get_vol_cur(float *voltage, float *current);

// set functions

void ui_clear_microsecond_counter(void);
void ui_set_back_light_level(uint8_t level);
void ui_set_key_sound_enable(bool enable);
void ui_set_language_select(uint8_t lang);
void ui_set_display_rotation(uint16_t rotation);
void ui_set_current_range_mode(uint8_t mode);
void ui_update_vol_cur_varables(float voltage, float current);
void ui_clear_data_monitor(void);

// sys save function
void ui_system_settings_save(void);

///////////////////////////////////////////////////////////////


///////////// interface for com with PD UFP Task //////////////

// Which PD contract the sink currently holds. PD stays on, and its page stays
// reachable from the Set page, for as long as this is not OFF.
typedef enum {
    UI_PD_MODE_OFF = 0,
    UI_PD_MODE_PPS,
    UI_PD_MODE_FIXED,
} ui_pd_mode_t;

void ui_send_pdsink_start_msg(void);
void ui_send_pdsink_stop_msg(void);
void ui_send_pps_set_msg(float voltage, float current);
void ui_send_pd_fixed_set_msg(uint8_t level);
int8_t MsgQueueGet_PPS_ready(void);
int8_t MsgQueueGet_PD_ready(void);
ui_pd_mode_t ui_pd_get_mode(void);
// name of the page that controls the active contract, NULL when PD is off
const char * ui_pd_get_page_name(void);
// counts PD sink starts; a PD page resets its own state when it sees a new value
uint8_t ui_pd_get_session(void);

///////////////////////////////////////////////////////////////


#ifdef __cplusplus
} /*extern "C"*/
#endif

#endif
