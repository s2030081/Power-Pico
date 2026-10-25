# CMake / GCC 构建

## 来源

本 fork 的 CMake / GCC 构建来自 EudaiG 的 PR #6 首个提交，原样合入（d5f2f64）：
https://github.com/No-Chicken/Power-Pico/pull/6
https://github.com/EudaiG/Power-Pico/commit/d5f2f644e38a072c6729e283662276474949d291

包括源码收集修正、NuttX 排除、浮点 printf、FreeRTOS GCC ARM_CM4F port（保留上游许可证）、链接栈布局及断言、LVGL 双缓冲，以及 Modern/Classic、LTO、EEPROM 恢复三个 CMake 选项。之后本 fork 补了缺失的声明（a0a4c22、02a2e85）和 ui_ChartPage.h 的 include 大小写（d8c1388）。

上游 No-Chicken/Power-Pico 在 741cf34 中以自己的方式集成了同一提交（不含上述三个选项），于 2026-10-25 合并进本 fork；冲突处保留本 fork 的内容，因此三个选项仍在。

## 编译

安装 CMake >= 3.22、Ninja 和 Arm GNU Toolchain，将其 bin 目录加入 PATH，在本目录执行：

```powershell
cmake --preset Modern -DCMAKE_C_FLAGS=-Werror=implicit-function-declaration
cmake --build --preset Modern
```

产物为 build/Modern/output/power_pico.bin（及 .hex），ELF 为 build/Modern/power_pico.elf。

- Modern 与 Classic 两个预设均为 MinSizeRel，只差 POWER_PICO_UI_MODERN。目前源码中没有读取该宏的代码，两者生成相同的 bin。
- POWER_PICO_RESTORE_EEPROM_BASELINE 只定义同名宏，目前同样没有代码读取；POWER_PICO_LTO 打开 -flto（FreeRTOS 除外），默认关闭，未验证。
- Debug 预设使用 -O0，未验证能否装入 448 KiB 的 APP 区。

## 烧录

Bootloader（firmware/PowerPico_BootLoader.hex）需先经 SWD 烧录。之后可用 PC 客户端（YMODEM），或在仓库根目录用 tools 下的脚本（Python 3.8+，用法见 tools/README.md）：

```powershell
py tools/pico_boot.py
py tools/pico_flash.py flash software/Power_Pico/build/Modern/output/power_pico.bin --run
```

pico_boot.py 让运行中的固件重启进 Bootloader，不需要按键；pico_flash.py 写入前检查镜像，写入后比较 Bootloader 报告的大小和 CRC32，--run 启动新固件并读回测量数据流。

## 固件与内存

- APP Flash 起始地址 0x08010000，容量 448 KiB；bin 只含 APP，不含 Bootloader。
- 初始 MSP 0x2001D820，主栈最低预留 0x2800（10 KiB），这不改变 FreeRTOS 任务栈。PR #6 之前（161827a）为 RAM 末端 0x20020000 和 0x400；MDK 启动文件的 Stack_Size 仍为 0x400，官方 v1.1.7 bin 的 MSP 为 0x2001D8A0。
- 链接断言检查栈顶在 RAM 内、静态数据和堆不与预留主栈重叠。链接检查不替代运行时栈余量检查。
- LVGL：原 57600 字节单缓冲（BUFFER_METHOD 1）改为两个各 19200 字节（40 行）的缓冲（BUFFER_METHOD 2），共 38400 字节，省 19200 字节。lv_port_disp.c 为 GCC/Keil 共用，Keil 构建同样使用此设置，未在 Keil 下重新验证。

## 2026-10-25 验证记录

合并上游 741cf34 后的源码。工具链 Arm GNU Toolchain 14.2.Rel1（GCC 14.2.1）、CMake 4.4.4、Ninja 1.13.2。

- Modern（加 -Werror=implicit-function-declaration）与 Classic 均构建成功，0 error，159 条其他警告未处理。
- Flash 402128 / 458752 字节（87.66%）；RAM 112344 / 131072 字节（85.71%，含链接预留，不代表实际最大栈用量）。
- 两个预设的 bin 相同，也与合并前（6448b80）相同，SHA256：
  D779EBB5CD37F2B733A4134C89931F86B347CF85157FBC79A1982C80A1ACDDAA
- 该 bin 已经 pico_boot.py 和 pico_flash.py 写入实机：Bootloader 报告 402128 字节、CRC32 0xD9C29FE4，与文件一致；--run 后新固件从 Bootloader 启动，3 秒收到 3002 个测量包。
- test/firmware 下的两个校准测试来自上游，没有构建脚本。用 arm-none-eabi-gcc -fsyntax-only 检查可编译（include 顺序：test/firmware/stubs、User、User/Tasks/Inc、User/GUI）；本机无主机 C 编译器，未运行。

## 上线前回归

已做：上述 bin 经 Bootloader 升级后冷启动并输出测量数据。仍需：

- 热复位和重复启动。
- 设置页、PD 等待/成功/失败提示、页面切换、屏幕旋转与连续刷新，检查双缓冲是否花屏或卡死。
- 浮点数值显示、USB 数据传输、原有 PD 功能、设置和校准值保持。
- GCC 与现有 Keil 固件双向 OTA；如发布 Keil 固件，单独构建并上板验证。
- 长时间采集和任务栈高水位检查。
