// This configuration is for a generic ESP32C3 board with 4MiB (or more) of flash.

#ifndef MICROPY_HW_BOARD_NAME
#define MICROPY_HW_BOARD_NAME               "ESP32C3 module"
#endif
#define MICROPY_HW_MCU_NAME                 "ESP32-C3"

// Enable UART REPL for modules that have an external USB-UART and don't use native USB.
#define MICROPY_HW_ENABLE_UART_REPL         (1)

// minutePing bisection: initial GC heap 56 KB (C3 default 64 KB).
#define MICROPY_GC_INITIAL_HEAP_SIZE        (56 * 1024)
