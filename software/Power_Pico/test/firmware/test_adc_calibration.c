#include <math.h>
#include <stdbool.h>
#include <stdio.h>

#include "gate.h"
#include "user_AdcDataStrategy.h"

static int failures;
static ADC_Calibration_t current_calibration;
static uint8_t gate_mode = GATE_MODE_AUTO;
static uint8_t gate_range = HIGH_CUR;

#define CHECK_TRUE(expression)                                                   \
    do {                                                                         \
        if (!(expression)) {                                                     \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #expression);       \
            failures++;                                                         \
        }                                                                        \
    } while (0)

#define CHECK_FALSE(expression) CHECK_TRUE(!(expression))
#define CHECK_NEAR(expected, actual, tolerance)                                  \
    CHECK_TRUE(fabs((double)(expected) - (double)(actual)) <= (tolerance))
/* float32 resolution: the scales are stored as float, SCALE_* are doubles */
#define CHECK_REL(expected, actual)     CHECK_NEAR((expected), (actual), fabs((double)(expected)) * 1e-6)

void Sys_Get_AdcCalibration(ADC_Calibration_t *calibration)
{
    *calibration = current_calibration;
}

bool Sys_Set_AdcCalibration(const ADC_Calibration_t *calibration)
{
    if (!ADC_Calibration_IsValid(calibration)) {
        return false;
    }
    current_calibration = *calibration;
    return true;
}

bool USER_USB_is_Configured(void)
{
    return false;
}

bool pico_diag_usb_service(void)
{
    return false;
}

void flow_route_selection(uint8_t selection)
{
    gate_range = selection;
}

uint8_t Gate_get_status(void)
{
    return gate_range;
}

void Gate_Set_Mode(uint8_t mode)
{
    gate_mode = mode;
    if (mode != GATE_MODE_AUTO) {
        gate_range = mode;
    }
}

uint8_t Gate_Get_Mode(void)
{
    return gate_mode;
}

uint64_t GetMicrosecondCounter(void)
{
    return 0U;
}

static void test_defaults_and_boundaries(void)
{
    ADC_Calibration_t calibration;

    ADC_Calibration_SetDefault(&calibration);
    CHECK_TRUE(sizeof(ADC_Calibration_t) == 6U * sizeof(float));
    CHECK_REL(SCALE_LOW, calibration.low_scale_ua_per_lsb);
    CHECK_REL(SCALE_MID, calibration.mid_scale_ua_per_lsb);
    CHECK_REL(SCALE_HIGH, calibration.high_scale_ua_per_lsb);
    CHECK_NEAR(0.0f, calibration.low_offset_ua, 1e-7f);
    CHECK_NEAR(0.0f, calibration.mid_offset_ua, 1e-7f);
    CHECK_NEAR(0.0f, calibration.high_offset_ua, 1e-7f);
    CHECK_TRUE(ADC_Calibration_IsValid(&calibration));

    /* Just inside the limits: gain 0.75-1.25, offset 32 LSB of the nominal
       scale (pr/6 dropped the gain from the offset limit). */
    calibration.low_scale_ua_per_lsb = (float)(SCALE_LOW * 0.7501);
    calibration.mid_scale_ua_per_lsb = (float)(SCALE_MID * 1.2499);
    calibration.high_offset_ua = (float)(31.99 * SCALE_HIGH);
    CHECK_TRUE(ADC_Calibration_IsValid(&calibration));

    calibration.high_offset_ua = (float)(33.0 * SCALE_HIGH);
    CHECK_FALSE(ADC_Calibration_IsValid(&calibration));
}

static void test_invalid_floats_and_gain(void)
{
    ADC_Calibration_t calibration;

    ADC_Calibration_SetDefault(&calibration);
    calibration.low_scale_ua_per_lsb = (float)(SCALE_LOW * 0.749);
    CHECK_FALSE(ADC_Calibration_IsValid(&calibration));

    ADC_Calibration_SetDefault(&calibration);
    calibration.mid_scale_ua_per_lsb = (float)(SCALE_MID * 1.251);
    CHECK_FALSE(ADC_Calibration_IsValid(&calibration));

    ADC_Calibration_SetDefault(&calibration);
    calibration.high_offset_ua = NAN;
    CHECK_FALSE(ADC_Calibration_IsValid(&calibration));

    ADC_Calibration_SetDefault(&calibration);
    calibration.low_scale_ua_per_lsb = INFINITY;
    CHECK_FALSE(ADC_Calibration_IsValid(&calibration));
}

static void test_current_conversion_uses_sys_settings(void)
{
    ADC_Calibration_SetDefault(&current_calibration);
    current_calibration.low_scale_ua_per_lsb = (float)(SCALE_LOW * 1.20);
    current_calibration.mid_scale_ua_per_lsb = (float)(SCALE_MID * 0.80);
    current_calibration.high_scale_ua_per_lsb = (float)(SCALE_HIGH * 1.10);
    current_calibration.low_offset_ua = 3.0f;
    current_calibration.mid_offset_ua = -4.0f;
    current_calibration.high_offset_ua = 5.0f;

    CHECK_REL(2.0 * SCALE_LOW * 1.20 + 3.0,
              ADC_Convert_Current_uA(2050U, 2048U, LOW_CUR));
    CHECK_REL(-3.0 * SCALE_MID * 0.80 - 4.0,
              ADC_Convert_Current_uA(2045U, 2048U, MID_CUR));
    CHECK_REL(4.0 * SCALE_HIGH * 1.10 + 5.0,
              ADC_Convert_Current_uA(2052U, 2048U, HIGH_CUR));
}

int main(void)
{
    test_defaults_and_boundaries();
    test_invalid_floats_and_gain();
    test_current_conversion_uses_sys_settings();

    if (failures != 0) {
        printf("%d ADC calibration test(s) failed\n", failures);
        return 1;
    }
    puts("ADC calibration tests passed");
    return 0;
}
