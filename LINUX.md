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

# 可选：系统级 adb；不装也行——tools/fetch_scrcpy.py 拉的 scrcpy Linux 包自带 adb
sudo apt install android-tools-adb
```

> adb 有四个来源（按 `find_adb` 的优先序）：随包 `resources/scrcpy-linux/adb`（**scrcpy
> 官方 Linux 包自带，跑过 `tools/fetch_scrcpy.py` 就有**，与 Windows 版的
> `resources/scrcpy-win64/adb.exe` 一个道理）→ 手动放进 `resources/platform-tools/adb`
> 的 platform-tools → `PATH`（如 `apt install android-tools-adb`）→ 常见安装目录。
> `config.yaml` 的 `adb.path` 留空即走上面的自动搜索；找不到 adb 时报错见第 8 节。

PyQt6 与 OpenCV 需要一些系统运行库，桌面版一般已经带上；**最小化安装或容器里**要自己装
（缺 `libGL.so.1` 之类的报错就是这里没装全）：

```bash
sudo apt install libgl1 libegl1 libglib2.0-0 libxkbcommon0 libdbus-1-3 \
    libfontconfig1 libfreetype6 libx11-6 libxext6 libxrender1 libxcb1 libxcb-cursor0
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
.venv/bin/python tools/fetch_scrcpy.py          # scrcpy（Linux 静态包）-> resources/scrcpy-linux/
.venv/bin/python tools/fetch_ocr_models.py      # PP-OCRv6 tiny 模型 -> runs/models/rapidocr/
```

`fetch_scrcpy.py` 默认版本 `4.1`（与 Windows/CI 一致）；想要更新的可以
`.venv/bin/python tools/fetch_scrcpy.py --version 5.0`。
**注意**：这个包**自带 `adb`**（scrcpy 官方 Linux 包用 Google platform-tools 的 adb
打进包里，解到 `resources/scrcpy-linux/adb`），所以 Linux 侧 adb 也不用单独准备；
OCR 模型也可以不预拉——首次 OCR 时会自动下载。

### 2.4 直接用 CI 打好的包（可选）

不想自己搭环境/跑 PyInstaller 的话，GitHub Release 里有 Linux 包：

- `QQPetCopilot-<版本>-linux-x64.tar.gz` —— 由 `.github/workflows/release.yml` 的
  `build-linux` job（ubuntu runner）用 `python build.py` 打包，内含 onefile 可执行文件
  `QQPetCopilot` 与本文档。
- 解压后 `chmod +x QQPetCopilot && ./QQPetCopilot`（首次运行会在同目录生成 `config.yaml`
  和 `runs/`）。adb 已随包（scrcpy Linux 包自带，打进 onefile 里），无需自备。

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
adb devices -l        # adb 不在 PATH 时用随包的：resources/scrcpy-linux/adb devices -l
```

正常输出（能被 adb 认到）：

```
faf55425    device usb:3-2 product:PJZ110 model:PJZ110 device:OP5D0DL1
```

没出现 `device` 时，三条命令定位卡在哪一层：

```bash
adb devices -l            # 列表里有没有？状态是 device / unauthorized / no permissions
lsusb                     # USB 层看不看得到手机（只想看某个厂商：lsusb -d 22d9:）
ls -l /dev/bus/usb/*/*    # 节点权限，如 crw-rw-r-- 1 root plugdev
```

| 现象 | 说明 |
|---|---|
| `device` | 好了，可以启动 GUI |
| `unauthorized` | 手机上点「允许 USB 调试」；没弹窗就开发者选项里「撤消 USB 调试授权」后重插 |
| `no permissions` | 权限问题 → 见 3.3 |
| adb 列表空，但 `lsusb` 能看到手机 | 多半是 USB 调试没开，或通知栏 USB 用途是「仅充电」 |
| `lsusb` 也看不到 | 线/口/供电问题，换个口或换根线 |

### 3.3 权限问题（`no permissions`）

Ubuntu / Pop!_OS 默认不带安卓 udev 规则，普通用户访问 `/dev/bus/usb/*` 会被拒。装发行版维护的
规则包即可（覆盖各厂商、由 udev 按 `uaccess` 发 ACL，**不用重新登录、也不用加组**）：

```bash
sudo apt install android-sdk-platform-tools-common
```

只给某一台手机放权限（或不想装上面那个包）时，自己写一条 —— 把 `22d9` 换成 `lsusb` 里看到的
vendor id（`ID 22d9:2765 OPPO...` 里冒号前那段）：

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="22d9", TAG+="uaccess"' \
  | sudo tee /etc/udev/rules.d/51-android-local.rules >/dev/null
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb --action=add
```

之后把手机拔了重插，再 `adb devices -l` 确认变成 `device`。

> 不建议用 `MODE="0666"`（等于放开给所有用户可写）或「加进 `plugdev` 组」那套老办法：
> 前者权限过宽，后者要重新登录才生效（`uaccess` 是给当前登录会话发 ACL，立即生效）。

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
  和 scrcpy 关面板不冲突，两个都开也不会把面板点亮。抓帧（`screencap`）不依赖面板
  点亮，所以熄屏时也不需要唤醒屏幕——抓帧前的那次 `KEYCODE_WAKEUP` 只在熄屏开关
  关掉时才发（`_wake_device`，仅 Linux 抓帧镜像用；Windows 镜像路径不调它）。
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
| `no permissions` | 装 udev 规则包：`sudo apt install android-sdk-platform-tools-common`（或手写一条 `uaccess` 规则），见 3.3 |
| `unauthorized` | 手机上重新确认授权弹窗；或开发者选项里「撤消 USB 调试授权」后重插 |
| `error: no devices/emulators found` | 没插好 / USB 用途是「仅充电」/ adb server 卡了（`adb kill-server`） |
| `找不到 adb` | 先跑 `tools/fetch_scrcpy.py`（随包 adb 落在 `resources/scrcpy-linux/adb`）；或装系统 adb（`apt install android-tools-adb`），或把 platform-tools 解压到 `resources/platform-tools/` |
| 镜像画面全黑 | 设备上盖了黑窗口（悬浮窗/画中画，见第 7 节）；或面板被熄屏 scrcpy 关了且设备没在渲染（`gui.screen_off: false` 可排除） |
| 镜像不动 | `adb devices` 看看设备还在不在；抓帧线程失败会自己退避重试 |
| GUI 起不来 / 花屏 | 试 `QT_QPA_PLATFORM=xcb .venv/bin/python main.py` 强制走 XWayland |
| 金币/学园阶段识别错 | 十有八九是悬浮窗遮挡，见第 7 节 |
| 手机面板不熄 / 亮回来 | 熄屏 scrcpy 掉了（看门狗 15s 内会重拉）；或「熄屏运行」开关被关了；或 `gui.screen_off: false` |
| 切换「熄屏运行」时镜像窗口闪一下/重开 | Windows 上属正常：镜像进程自带的 `--turn-screen-off` 在启动时固定，开关一变只能重启镜像才能生效 |
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
| `tools/smoke_gui.py` | 新增：无头 GUI 冒烟测试 |
| `tools/test_mirror_pause.py` | 新增：验证窗口隐藏时抓帧暂停（PASS/FAIL 退出码） |

> 早期版本还带过 `tools/linux_android_check.sh`（设备体检）和 `tools/linux_android_udev.sh`
> （写 udev 规则），后已删除：前者要的信息 `adb devices -l` / `lsusb` / `ls -l /dev/bus/usb/*/*`
> 三条命令就能给（见 3.2），后者与发行版包 `android-sdk-platform-tools-common` 重复、且它用的
> `MODE=0660` + `plugdev` 组不如标准的 `TAG+="uaccess"`（后者不必重新登录），现在 3.3 直接给
> 装包与手写规则两条路。
