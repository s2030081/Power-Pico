# Host tools

Python 3.8 or later, standard library only. The scripts that open the USB port use
pyserial when it is installed; on Windows without it they use `pico_serial.py`, which
talks to the port through the Win32 API. Each script's docstring says how to use it.

| Script | What it does |
| --- | --- |
| `pico_flash.py` | Flashes an application image through the USB bootloader, with checks before and after |
| `pico_boot.py` | Asks the running firmware to restart into the bootloader (command 0x15), so no button is needed |
| `pico_vlog.py` | Logs the USB stream to a file and prints input VBUS once a second |
| `pico_dlog.py` | The same, and prints the debug build's PD events as they arrive |
| `pico_vtrace.py` | Analyses a logged stream: gaps, VBUS under 1 V and over 4.75 V, plateaus, and an SVG plot |
| `pico_serial.py` | The USB serial port, through pyserial or the Win32 API (a module, not a command) |
| `pico_stream.py` | Parses the USB stream: measurement packets, command frames and the debug build's frames (a module) |

Tests, no unit needed:

```
py -m unittest discover -s tools
```
