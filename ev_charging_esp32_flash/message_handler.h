#ifndef MESSAGE_HANDLER_H
#define MESSAGE_HANDLER_H

#include <Arduino.h>

void msg_handle_incoming(const char* payload);
String msg_create_status_update();

#endif // MESSAGE_HANDLER_H
