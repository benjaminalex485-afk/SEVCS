#ifndef WEBSOCKET_MANAGER_H
#define WEBSOCKET_MANAGER_H

#include <Arduino.h>

void ws_init();
void ws_loop();
void ws_send(String message);
bool ws_is_connected();

#endif // WEBSOCKET_MANAGER_H
