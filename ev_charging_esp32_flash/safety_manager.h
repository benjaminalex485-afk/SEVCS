#ifndef SAFETY_MANAGER_H
#define SAFETY_MANAGER_H

#include <Arduino.h>

void safety_init();
void safety_feed_watchdog();
void safety_update_vehicle_presence(bool present);
void safety_process_watchdog();
bool safety_is_system_safe();

#endif // SAFETY_MANAGER_H
