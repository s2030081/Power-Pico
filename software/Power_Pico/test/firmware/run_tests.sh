#!/bin/sh
# Builds and runs the two calibration tests on the host with gcc (Linux, or WSL
# on Windows), against the firmware's own User/user_AdcDataStrategy.c and
# User/user_CmdStrategy.c, with the headers in stubs/ standing in for the HAL,
# FreeRTOS and the EEPROM driver. Run from anywhere: sh test/firmware/run_tests.sh
set -e
cd "$(dirname "$0")/../.."
INC="-Itest/firmware/stubs -IUser -IUser/Tasks/Inc -IUser/GUI"
CFLAGS="-std=gnu11 -Wall -Wextra -Werror"
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
gcc $CFLAGS $INC test/firmware/test_adc_calibration.c User/user_AdcDataStrategy.c \
    -lm -o "$OUT/test_adc_calibration"
gcc $CFLAGS $INC test/firmware/test_calibration_protocol.c User/user_CmdStrategy.c \
    User/user_AdcDataStrategy.c -lm -o "$OUT/test_calibration_protocol"
"$OUT/test_adc_calibration"
"$OUT/test_calibration_protocol"
