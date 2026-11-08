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

- Modern 与 Classic 两个预设均为 MinSizeRel，只差 POWER_PICO_UI_MODERN：Modern 是 v1.2.0 的界面（电源调节菜单、固定电压页和 PPS 页），Classic 是本 fork 的界面（设置页开启 PD、9 档 PPS 预设）。
- Trace 预设即 Modern 加 POWER_PICO_DEBUG_TRACE：数据流中多出 AA 5D 调试帧（事件、摘要、上次崩溃记录），由 tools/pico_dlog.py 读取。其他预设不发送这些帧，数据流只有 PC 客户端解析的 AA 55 测量包；崩溃记录、事件环、tick 看门狗和陷阱复位在所有预设中都运行。
- POWER_PICO_RESTORE_EEPROM_BASELINE（v1.2.0 起有效）在启动时用 EEPROM 0xC0 的备份改写 0x10 的设置和 0x40 的校准值，日常构建不要打开。POWER_PICO_LTO 打开 -flto（FreeRTOS 除外），默认关闭，未验证。
- Debug 预设使用 -O0，2026-11-09 实测超出 448 KiB 的 APP 区 127384 字节，不能烧录；要调试帧用 Trace 预设。

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

## 2026-11-09 合并 PR #6 v1.2.0（pr/6）

在本 fork 的 main（含 pico_cal.py 和 pps-debug-trace）上合并 EudaiG 的 v1.2.0。Modern 界面采用 v1.2.0，Classic 界面保留本 fork 的流程。另加：

- 新 PD 引擎（BSP/PD/fixed_voltage_control.c）的 PPS 下限由 5 V 降到 3.3 V，固定 PDO 仍从 5 V 起；user_pd_power_request 和电压确认（pd_voltage_check.h）同步。VBUS 低于约 4 V 时 FUSB302 会报断开，所以请求 5 V 以下的 PPS 前关闭 VBUS 检测，合约回到 5 V 及以上时再打开（旧 PD 库在 PPS 模式下也这样做）。关闭期间拔掉充电器检测不到。
- Modern 固定电压页加 3.3、3.7、4.2 V 三个 PPS 1 A 按钮，只在充电器的 PPS 范围覆盖时出现，与充电器的固定电压一起按电压升序排列；超过 8 个按钮时改为每行 5 个。光标首次落在 5 V，之后回到上次所在的电压按钮（只存于 RAM，断电后回到 5 V；按“恢复5V”也回到 5 V）。两个电源页的电流读数改用与主页相同的 uA/mA/A 格式。
- v1.2.0 的电源调节菜单和固定电压页只编进 Modern：Classic 用不到，且其字体会让 Classic 超出 APP 区约 8.7 KiB。
- 调试帧受 POWER_PICO_DEBUG_TRACE 控制（见上文 Trace 预设）。
- MessageSendTask：命令回复（或调试帧）与等待中的测量包合并为一次 USB 传输，回复在前；USB 忙时最多缓存 4 个测量包。之前回复占用的那一轮会丢掉一个测量包。
- MessageSendTask 的优先级由 osPriorityHigh 提到 osPriorityHigh2，高于 PD 和接收任务（v1.2.0 把两者提到 High1）。v1.2.0 的 PD 任务每 20 ms 经 I2C 读一次 FUSB302，约 1.9 ms；发送任务优先级低于它时，ADC 半缓冲有时在被读出前就被 DMA 覆盖。实机上 6 s 丢了 90 个测量包（6003 个中），时间戳每 20 包出现一次约 1.9 ms 的间隔。改后 10 s 收到 10003/10003 个，间隔 994-1006 us。
- test/firmware 的两个校准测试改用 v1.2.0 的 *_scale_ua_per_lsb 字段，补了桩文件，并加 run_tests.sh。
- 新 PD 引擎先处理 GoodCRC，再检查 Specification Revision。实机所用充电器的 GoodCRC 该字段为 0，原代码因此丢弃 GoodCRC，MessageID 不前进；该充电器在 PS_RDY 后约 20 ms 会再发一次 Source_Capabilities，引擎回复的 Request 仍为 id0，被当作重发丢弃，充电器随即硬复位，VBUS 断电约 0.8 s，如此循环（固定电压页状态在“读取能力”和“正在协商”之间跳动）。改后第二个 Request 为 id1，合约保持在 5 V。
- Classic 设置页：PD 协商失败的红框显示 2 秒，期间离开设置页，红框随页面删除，计时器之后再删一次会崩溃（main 上原有）。现在离开页面时删除该计时器。

firmware/PowerPico_Firmware_v1.2.0.bin 是 PR #6 自己的镜像，不含以上改动。

工具链同上。结果：

- Modern：Flash 445480 / 458752 字节，RAM 119416 / 131072 字节；SHA256 9f5fc7b2f3bad521340820d84e675688fd2e2b5c31b293a770332d316f09f184。
- Classic：Flash 408888 字节，RAM 118080 字节；SHA256 85d339898fe63e5b138651784245bb022539082d7d7e9b0c4ebf37ce8ed0f93c，CRC32 0xD2D006DD（合并提交 2a39f80 时为 408760 字节）。
- Trace：Flash 446440 字节；SHA256 c4d0414de858b11ea69736417bf2c31c8670a93c8b50de84496a127ee9db1f8c，CRC32 0x695F9758。
- 以上三个为含发送任务优先级、GoodCRC 修复和按钮排序的构建（Classic 不含固定电压页，排序不影响它）。
- 三个预设 0 error。新增的警告只有两边原有的（fusb302_dev.c 未用变量、ui_SetPage.c 标签后声明等）。
- test/firmware/run_tests.sh 在 WSL Ubuntu 24.04 的 gcc 13.3 下（-Wall -Wextra -Werror）两项测试均通过。
- 校准：0x40 记录格式和有效性判据与 1.1.7 相同，启动时不改写；实机现有值（中档偏移 -67.7 uA，高档增益 0.9527、偏移 -18113 uA）经 v1.2.0 的 float32 往返后读回不变。
- 上板（2026-11-09）：Modern bin 经 pico_boot.py 和 pico_flash.py 写入实机三次（289ab0d、优先级修复、GoodCRC 修复），每次 Bootloader 报告的大小和 CRC32 与文件一致；之后又写入按钮排序后的 bin，以及诊断环形缓冲由 32 条加到 64 条的 bin（即上述 Modern，为实机当前固件）。64 条可容下一次完整的开机协商，包括两次 Source_Capabilities；pdiag 转储的大小由 508 字节变为 892 字节。优先级修复后 10 s 收到 10003/10003 个测量包。烧录前、首次烧录后和优先级修复后各读一次 CAL_GET，三次回答逐字节相同。GoodCRC 修复后用 pdiag 诊断转储确认 5 V 合约在充电器重发 Source_Capabilities 后保持，VBUS 8 s 内保持 5.0-5.2 V，固定电压页电压确认通过。Classic 和 Trace 未上板；其他回归项（PPS、预设、界面）未测。
- Keil 工程未更新，pps-debug-trace 合并后 stm32f4xx_it.c 的 GCC 内联汇编和 user_DebugTrace.c 都不在 Keil 工程里，Keil 构建预计失败。

## 上线前回归

已做：上述 bin 经 Bootloader 升级后冷启动并输出测量数据。仍需：

- 热复位和重复启动。
- 设置页、PD 等待/成功/失败提示、页面切换、屏幕旋转与连续刷新，检查双缓冲是否花屏或卡死。
- 浮点数值显示、USB 数据传输、原有 PD 功能、设置和校准值保持。
- GCC 与现有 Keil 固件双向 OTA；如发布 Keil 固件，单独构建并上板验证。
- 长时间采集和任务栈高水位检查。
- v1.2.0 合并后（Modern）：PPS 降到 3.3 V 并保持（VBUS 检测关闭后不误报断开）；3.3/3.7/4.2 V 预设按钮；从 3.3 V 回到 5 V 和 9 V；PPS 低于 5 V 时拔插充电器；uA 级电流读数。
- v1.2.0 合并后（Classic）：设置页开启 PD、9 档预设、离开页面 PD 保持。
- PC 客户端 0.1.3 能连续采集（烧录前后的 pico_cal.py read 比较已做，见上）。
