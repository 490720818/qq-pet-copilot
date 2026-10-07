# Linux 部署与使用说明

本项目原版面向 Windows（PyQt6 + Win32 窗口嵌入 + 随包 `scrcpy-win64`）。
本文档说明**在 Linux 上跑真机模式**的完整流程，以及为此做的改动。

> 验证环境：Pop!_OS 24.04（Ubuntu noble 系）/ Wayland 会话 / 一加 13（PJZ110，Android 15，arm64-v8a）
> 实测通过：uiautomator2 连接、截图、整屏 OCR、GUI 启动、画面镜像、完整调度一轮（学习 → 踩踩 → PK）

---

## 1. 与 Windows 版的差异

| 方面 | Windows 版 | Linux 版 |
|---|---|---|
| 画面镜像 | 起 scrcpy 窗口，用 `win32gui.SetParent` 嵌进 Qt | `adb exec-out screencap -p` 后台抓帧，Qt 直接绘制 |
| 镜像帧率 | scrcpy 原生（30~60 fps） | **约 1 fps**（`screencap` 单帧 ~1s，设备端 PNG 编码是瓶颈） |
| 关屏 | `--turn-screen-off` | 同样 `--turn-screen-off`，**实测不影响截图/OCR/自动化**，见第 6 节 |
| 设备控制 | `injectInputEvent`（默认）/ `minitouch` | 同左，真机用默认 `injectInputEvent` 即可 |
| 模拟器模式 | MuMu / 雷电 / 夜神 / 蓝叠 | **不可用**（这些模拟器只有 Windows 版）。Linux 上要用得自己搭 AVD/Genymotion |
| 桌面通知 | `winotify` Toast | 不可用，改配 `notify.onepush_config` |
| 打包 | PyInstaller + `scrcpy-win64` | 未验证（源码运行是主要路径） |

**真机模式不需要**：Root、frida、minitouch 二进制、模拟器探测。
只有把 `control.method` 改成 `minitouch` 时才会去拉 `resources/minitouch/`。

---

## 2. 依赖安装

### 2.1 系统依赖

```bash
# Python 3.12（源码运行要求；3.13+ 部分依赖还没轮子）
python3.12 --version

# 可选：系统级 adb（本项目自带 resources/platform-tools/adb，装不装都行）
sudo apt install android-tools-adb
```

`scrcpy` **不需要** `apt install`——本项目的 `tools/fetch_scrcpy.py` 会拉官方 Linux 静态包
（自带 SDL / libavcodec，不依赖系统库），解到 `resources/scrcpy-linux/`。

### 2.2 Python 环境

```bash
cd qq-pet-copilot
uv venv --python /usr/bin/python3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

用 `python3 -m venv .venv` 也可以。`pywin32` / `winotify` 有 `sys_platform == 'win32'`
标记，在 Linux 上会自动跳过。

### 2.3 随包资源

```bash
.venv/bin/python tools/fetch_scrcpy.py          # adb + scrcpy -> resources/
.venv/bin/python tools/fetch_ocr_models.py      # PP-OCRv6 tiny 模型 -> runs/models/rapidocr/
```

`fetch_scrcpy.py` 默认版本 `4.1`（与 Windows/CI 一致）；想要更新的可以
`.venv/bin/python tools/fetch_scrcpy.py --version 5.0`。
**注意**：OCR 模型也可以不预拉——首次 OCR 时会自动下载。

---

## 3. 连接安卓真机

### 3.1 手机端

1. 设置 → 关于手机 → 连点「版本号」7 次，打开开发者选项
2. 开发者选项 → 打开 **USB 调试**
3. 插 USB，通知栏把 USB 用途选成 **文件传输 / MTP**（「仅充电」不会暴露 ADB 接口）
4. 弹出「允许 USB 调试吗？」→ 勾选「始终允许」→ 确定

> 没弹窗不一定是坏事——如果这台手机以前授权过，会直接进 `device` 状态。

### 3.2 电脑端

```bash
bash tools/linux_android_check.sh
```

这个脚本会：跑 `adb devices -l`；按 **ADB 接口签名**（`bInterfaceClass=ff` +
`bInterfaceSubClass=42` + `bInterfaceProtocol=01`）扫 USB 找设备；打印设备节点权限；
列出已有 udev 规则；给出结论和下一步命令。

正常输出：

```
faf55425    device usb:3-2 product:PJZ110 model:PJZ110 device:OP5D0DL1
  22d9:2765  一加 13
      节点: /dev/bus/usb/003/009  权限: crw-rw-r-- root:plugdev
  ✅ adb 已认到设备，可以启动 GUI 了
```

### 3.3 权限问题（`no permissions`）

Ubuntu / Pop!_OS 默认不带安卓 udev 规则，普通用户访问 `/dev/bus/usb/*` 会被拒：

```bash
sudo bash tools/linux_android_udev.sh 22d9      # 参数是上面看到的 vendor id
```

不加参数会写入 30+ 常见厂商的全量规则。脚本会写
`/etc/udev/rules.d/51-android.rules`、把 `$SUDO_USER` 加进 `plugdev` 组、
然后 `udevadm control --reload-rules` + `trigger`。**之后把手机拔了重插**。

---

## 4. 配置

首次运行 `main.py` 会从 `config.example.yaml` 生成 `config.yaml`。

真机模式建议：

| 配置项 | 建议值 | 说明 |
|---|---|---|
| `adb.path` | `""` | 留空 = 自动搜索（随包 adb → PATH → 常见目录） |
| `adb.device_serial` | `""` 或具体序列号 | 只接一台设备时留空即可 |
| `control.method` | `injectInputEvent` | 真机默认值，**不要**改成 `minitouch` |
| `recover.method` | `重启游戏` | 真机上比 `重启设备` 轻得多（几秒 vs 一两分钟） |
| `notify.win_toast` | `false` | Windows Toast 在 Linux 无效 |
| `notify.onepush_config` | 按需 | 要告警就配 Bark / Server酱 / Telegram / SMTP 等 |
| `schedule.daily_hour_limit` | `8` | 每天学习+打工时长上限（小时），`0` = 不限 |

### 护理勋章注意

游戏里**护理相关勋章不能一键护理**——要拿这些勋章必须把 `care.method` 和
`friend_care.method` 配成 `ocr检测`，手动喂食/洗澡。默认的 `一键护理` 拿不到勋章。

---

## 5. 运行

### 5.1 GUI

```bash
.venv/bin/python main.py
```

首页会显示画面镜像。手机屏幕是否点亮由工具栏的**「熄屏运行」**开关控制，见第 6 节。

### 5.2 命令行调度器

```bash
.venv/bin/python scenarios/runner.py
```

### 5.3 单模块测试

```bash
.venv/bin/python scenarios/runner.py --test coins
.venv/bin/python scenarios/runner.py --test recover
.venv/bin/python scenarios/runner.py --test school.goto_school
.venv/bin/python scenarios/runner.py --test school.select_course
.venv/bin/python scenarios/runner.py --test work.select_place
```

`--test` 支持 `coins` / `recover` / `opener`，或
`<school|work|adventure|care|friend_care|hire_friend|visit|pk>.<方法名>`。

### 5.4 无头冒烟测试（不开窗口）

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/smoke_gui.py 10
```

---

## 6. 熄屏运行（OLED 防烧屏）

**这个项目可以完全熄屏跑。** 工具栏「熄屏运行」开关默认打开（`gui.screen_off: true`）。

### 原理

用无头 scrcpy 把手机面板关掉：

```bash
scrcpy --turn-screen-off --no-video --no-audio --stay-awake --no-window
```

关键是 `--turn-screen-off` 只关**面板电源**（SurfaceFlinger 里 `powerMode=Off`），
**不是让设备休眠**。渲染管线照常工作，所以：

| 能力 | 熄屏时 |
|---|---|
| `adb exec-out screencap -p` 截图 | ✅ 正常，返回实时画面 |
| uiautomator2 控件树（`content-desc` / `resource-id`） | ✅ 正常 |
| RapidOCR 整屏识别 | ✅ 正常 |
| `injectInputEvent` 点击 | ✅ 正常 |
| 整个调度器（学习/打工/冒险/踩踩/PK…） | ✅ 正常 |

### 实测证据（一加 13 / Android 15 / scrcpy 5.0）

- scrcpy 日志：`[server] INFO: Device display turned off`
- `dumpsys SurfaceFlinger` → `Display ... (HWC display 0): ... powerMode=Off`
- `dumpsys power` → `mWakefulness=Awake`（设备没睡）
- 熄屏后截图 `mean=37.15 std=66.60`，与亮屏基线 `mean=37.15 std=66.61` 平均绝对差 **0.04**
- 隔 30 秒再截 `mean=80.86`，与上一张差异 **80.97** → 是**实时画面**，不是缓存帧
- 熄屏下完整跑 `scenarios/runner.py`：`已在主页面 (score=1.00)`、好友列表 OCR 认出 5 人、
  一键护理、PK 连打 12 局 —— **零失败**

> 反直觉的一点：`dumpsys display` 在 scrcpy 关面板时**仍显示** `Display State=ON`。
> scrcpy 绕过 DisplayManager 直接调 SurfaceFlinger，只有 `dumpsys SurfaceFlinger`
> 的 `powerMode=Off` 才是真实面板状态。别被 `dumpsys display` 骗了。

### 怎么用

- **开**（默认）：面板关闭，手机看起来是黑屏，但自动化照跑。省 OLED 寿命 + 省电。
- **关**：面板保持点亮。调试/想盯着手机看的时候用。
- 开关状态持久化到 `config.yaml` 的 `gui.screen_off`，重启保持。

两个开关是**互相独立**的：

| | 画面镜像开 | 画面镜像关 |
|---|---|---|
| **熄屏开** | 面板灭，GUI 里仍能看到画面 | 面板灭，GUI 无预览 |
| **熄屏关** | 面板亮，GUI 有画面 | 面板亮，GUI 无预览 |

> 「画面镜像」只是**电脑上**的预览窗口；「熄屏运行」只管**手机面板**电源。
> 关掉镜像不会点亮手机屏幕，关掉熄屏也不会让电脑多耗电。

### 掉线自愈

熄屏 scrcpy 进程有 5 秒看门狗（`_check_screen_off`）：设备重插/重启后它会掉，
看门狗 15 秒退避重拉。**它一退出手机面板就自动亮回来**，所以掉了不会让手机卡在黑屏。

### 退出清理（避免面板卡在黑屏）

GUI 正常关闭（点右上角 ×）和收到 `SIGTERM` / `SIGINT`（终端 `Ctrl+C`、`kill <pid>`、
会话注销、systemd 停服务）都会走同一条 `closeEvent`，把熄屏 scrcpy 结束掉、面板恢复点亮。

万一被 `SIGKILL -9` 或崩溃强杀，熄屏 scrcpy 会孤儿化、面板一直黑着。
**下次启动 GUI 会自动清掉它**（`kill_previous_screen_off()`，靠命令行里的
`QQPetCopilotScrcpyOff-*` 标记识别）。要立刻手动恢复：

```bash
pkill -f 'scrcpy.*--turn-screen-off'
```

### 顺带修的寿命问题

- **`svc power stayon true` 与熄屏共存**：`stayon` 只管「USB 供电时别自动息屏」，
  和 scrcpy 关面板不冲突，两个都开也不会把面板点亮。
- **熄屏模式下不再发 `KEYCODE_WAKEUP`**：原来镜像启动前会唤醒屏幕，现在熄屏开着时跳过，
  否则刚关掉的面板立刻又被点亮。
- **窗口最小化时暂停抓帧**：单帧 `screencap` 约 1s，是**手机端** PNG 编码在烧 CPU。
  GUI 最小化/隐藏后抓帧线程自动暂停（`DeviceMirror.set_paused`），还原时继续。
- **抓帧间隔下限 0.3s**：抓一帧本来就要 1s，再背靠背连抓只是白烧电脑 CPU。

### 还需要你自己做的（代码管不到）

1. **充电上限设成 80%**：一加/OPPO 在 设置 → 电池 → 充电设置 里有「充电上限」。
   长期插着 USB 还充到 100%，是锂电池衰减最快的方式。
2. **别让它一直插着电又满电**：如果要挂几天，考虑用带开关的 USB hub 定时断电，
   或者接受 80% 上限。本项目需要 USB 连接（adb 走 USB），所以这个只能靠充电上限缓解。
3. **注意散热**：手机长时间跑游戏 + 充电会热。别放在被子里/密闭抽屉里。

---

## 7. ⚠️ 运行前必读：关掉悬浮窗和画中画

**这是实测踩到的第一个坑，也是最容易踩的。**

本项目靠 OCR 认像素。任何盖在游戏界面上的东西都会污染识别结果。

实测案例：手机上有一块 B 站**画中画**窗口（`com.android.purebilibili`），
frame `[443,322]-[1032,654]`，正好压住主页的**金币胶囊** `[648,306]-[849,390]`。
结果 `read_coins()` 在顶部扫描带里抓到了左边的**等级数字**，把金币报成 `4`（实际 `6200`），
调度器据此判断「金币不足」——整个调度逻辑就错了。

**跑之前请关掉：**

- 画中画 / 小窗视频（B 站、YouTube、微信视频等）
- 悬浮球、悬浮窗（游戏助手、清理大师之类）
- **自动点击器 / 按键精灵**（`com.ksxkq.autoclick` 之类）——会和本项目的触摸注入打架，
  它的辅助功能层还可能拦截点击
- 屏幕录制 / 投屏的隐私遮挡层

**排查方法**：

```bash
# 看谁在画面上叠了窗口
adb -s <序列号> shell dumpsys window windows | grep -E "Window #|mAttrs="

# 找画中画
adb -s <序列号> shell dumpsys window windows | grep -i pip

# 直接看有没有大块黑区（截图后用 Python 扫）
adb -s <序列号> exec-out screencap -p > /tmp/s.png
```

> 顺带说明：判断「在不在宠物主页」用的 `main_sign` 走的是**控件树**
> （`content-desc="金币胶囊"`），不受遮挡影响；受影响的是走 OCR 的那些逻辑
> （金币、学园阶段、照顾区域 exp、踩踩次数等）。

---

## 8. 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `no permissions` | 装 udev 规则，见 3.3 |
| `unauthorized` | 手机上重新确认授权弹窗；或开发者选项里「撤消 USB 调试授权」后重插 |
| `error: no devices/emulators found` | 没插好 / USB 用途是「仅充电」/ adb server 卡了（`adb kill-server`） |
| 镜像画面全黑 | 手机息屏了（`_wake_device` 会自动唤醒+stayon，但手动息屏后可能失效）；或设备上盖了黑窗口 |
| 镜像不动 | `adb devices` 看看设备还在不在；抓帧线程失败会自己退避重试 |
| GUI 起不来 / 花屏 | 试 `QT_QPA_PLATFORM=xcb .venv/bin/python main.py` 强制走 XWayland |
| 金币/学园阶段识别错 | 十有八九是悬浮窗遮挡，见第 7 节 |
| 手机面板不熄 / 亮回来 | 熄屏 scrcpy 掉了（看门狗 15s 内会重拉）；或「熄屏运行」开关被关了；或 `gui.screen_off: false` |
| 熄屏后 GUI 画面卡住不动 | 检查 `adb devices`；抓帧线程会自己退避重试，设备回来就恢复 |
| 一键护理找不到按钮 | 体力/清洁都正常时本来就不显示，属于正常跳过 |

---

## 9. 本次为 Linux 适配做的改动

| 文件 | 改动 |
|---|---|
| `main.py` | 按平台导入 Win32 模块；scrcpy 路径分平台；`_kill_scrcpy_by_marker` 加 POSIX 实现；新增 `DeviceMirror` 抓帧线程 + `ScrcpyContainer` 双模绘制；`_start_all` / `_try_embed` / `_check_scrcpy` / `_enable_scrcpy` / `_disable_scrcpy` / `_restart_scrcpy` / `closeEvent` 各加 Linux 分支 |
| `main.py`（熄屏） | 新增 `_scrcpy_env()`（给 scrcpy 传 `ADB` 环境变量，**否则 Linux 上找不到 adb**）；`start_scrcpy` / `start_scrcpy_screen_off` 受 `gui.screen_off` 控制并传 `env`；新增「熄屏运行」开关 `btn_screen_off` 与 `_screen_off_wanted` / `_apply_screen_off` / `_stop_screen_off` / `_check_screen_off` / `_toggle_screen_off`；`_wake_device` 熄屏时不发 `KEYCODE_WAKEUP`；`DeviceMirror` 加 `set_paused` + `_idle`，`MainWindow` 加 `changeEvent` / `showEvent` / `hideEvent` 同步暂停 |
| `src/config.py` | 加 POSIX adb 搜索路径；`bundled_adb_rel()` / `_bundled_adb_candidates()` 按平台返回随包 adb 位置；错误提示分平台；`GuiConfig` 新增 `screen_off: bool = True` |
| `src/settings.py` | `DEFAULTS['adb.path']` 改用 `bundled_adb_rel()`；新增 `DEFAULTS['gui.screen_off']` 与校验白名单 |
| `src/emulator.py` | `winreg` 平台桩（非 Windows 时 `_reg_open` 统一返回 `None`） |
| `src/u2dev.py` | `subprocess.CREATE_NO_WINDOW` 加平台守卫（原来在 Linux 上会直接 `AttributeError`） |
| `tools/fetch_scrcpy.py` | 加 Linux 分支（下 `scrcpy-linux-x86_64-v*.tar.gz`，解压后补 `0o755`） |
| `config.example.yaml` | `adb.path` 默认改为 `""`（自动搜索）；`gui.screen_off: true` |
| `tools/linux_android_check.sh` | 新增：设备体检 |
| `tools/linux_android_udev.sh` | 新增：装 udev 权限 |
| `tools/smoke_gui.py` | 新增：无头 GUI 冒烟测试 |
| `tools/test_mirror_pause.py` | 新增：验证窗口隐藏时抓帧暂停（PASS/FAIL 退出码） |
