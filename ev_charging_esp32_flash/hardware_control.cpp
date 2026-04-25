#include "hardware_control.h"
#include "config.h"

static bool relay_state = false;
static LEDState current_led_state = LED_OFF;

void hw_init() {
    pinMode(PIN_RELAY, OUTPUT);
    pinMode(PIN_LED_STATUS, OUTPUT);
    digitalWrite(PIN_RELAY, LOW);
    digitalWrite(PIN_LED_STATUS, LOW);
}

void hw_set_relay(bool active) {
    relay_state = active;
    digitalWrite(PIN_RELAY, active ? HIGH : LOW);
}

bool hw_get_relay() {
    return relay_state;
}

void hw_set_led(LEDState state) {
    current_led_state = state;
}

void hw_update_led() {
    static unsigned long last_toggle = 0;
    static bool led_on = false;
    unsigned long interval = 0;

    switch (current_led_state) {
        case LED_OFF:
            digitalWrite(PIN_LED_STATUS, LOW);
            return;
        case LED_ON:
            digitalWrite(PIN_LED_STATUS, HIGH);
            return;
        case LED_BLINK_FAST:
            interval = 200;
            break;
        case LED_BLINK_SLOW:
            interval = 1000;
            break;
    }

    if (millis() - last_toggle > interval) {
        last_toggle = millis();
        led_on = !led_on;
        digitalWrite(PIN_LED_STATUS, led_on ? HIGH : LOW);
    }
}
