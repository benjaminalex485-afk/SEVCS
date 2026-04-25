#ifndef HARDWARE_CONTROL_H
#define HARDWARE_CONTROL_H

#include <Arduino.h>

enum LEDState {
    LED_OFF,
    LED_ON,
    LED_BLINK_FAST,
    LED_BLINK_SLOW
};

void hw_init();
void hw_set_relay(bool active);
bool hw_get_relay();
void hw_set_led(LEDState state);
void hw_update_led();

#endif // HARDWARE_CONTROL_H
