#ifndef WIFI_MANAGER_H
#define WIFI_MANAGER_H

#include <Arduino.h>

void wifi_init();
bool wifi_is_connected();
void wifi_loop();

#endif // WIFI_MANAGER_H
