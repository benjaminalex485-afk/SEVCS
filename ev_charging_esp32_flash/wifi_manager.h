#ifndef WIFI_MANAGER_H
#define WIFI_MANAGER_H

#include <Arduino.h>

void wifi_init();
void wifi_init_ap();
bool wifi_is_connected();
void wifi_loop();

#endif // WIFI_MANAGER_H
