#ifndef TEST_MAIN_H
#define TEST_MAIN_H

#include <stddef.h> /* NULL, which the HAL headers bring in */
#include <stdint.h>

#define __disable_irq() ((void)0)
#define __enable_irq()  ((void)0)

void HAL_Delay(uint32_t milliseconds);
void NVIC_SystemReset(void);

#endif
