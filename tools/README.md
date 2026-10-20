# Host tools

Python 3.8 or later, standard library only. The scripts that open the USB port use
pyserial when it is installed; on Windows without it they use `pico_serial.py`, which
talks to the port through the Win32 API. Each script's docstring says how to use it.

| Script | What it does |
| --- | --- |
| `pico_flash.py` | Flashes an application image through the USB bootloader, with checks before and after |
| `pico_serial.py` | The USB serial port, through pyserial or the Win32 API (a module, not a command) |

Tests, no unit needed:

```
py -m unittest discover -s tools
```
