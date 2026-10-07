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
| 关屏 | 镜像开着也能 `--turn-screen-off` | **镜像开着不能息屏**（息屏只抓到黑帧）；镜像关掉才用无头 scrcpy 真关屏 |
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

首页会显示画面镜像。**注意**：镜像开着时手机保持亮屏（见第 1 节）。

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

## 6. ⚠️ 运行前必读：关掉悬浮窗和画中画

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

## 7. 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `no permissions` | 装 udev 规则，见 3.3 |
| `unauthorized` | 手机上重新确认授权弹窗；或开发者选项里「撤消 USB 调试授权」后重插 |
| `error: no devices/emulators found` | 没插好 / USB 用途是「仅充电」/ adb server 卡了（`adb kill-server`） |
| 镜像画面全黑 | 手机息屏了（`_wake_device` 会自动唤醒+stayon，但手动息屏后可能失效）；或设备上盖了黑窗口 |
| 镜像不动 | `adb devices` 看看设备还在不在；抓帧线程失败会自己退避重试 |
| GUI 起不来 / 花屏 | 试 `QT_QPA_PLATFORM=xcb .venv/bin/python main.py` 强制走 XWayland |
| 金币/学园阶段识别错 | 十有八九是悬浮窗遮挡，见第 6 节 |
| 一键护理找不到按钮 | 体力/清洁都正常时本来就不显示，属于正常跳过 |

---

## 8. 本次为 Linux 适配做的改动

| 文件 | 改动 |
|---|---|
| `main.py` | 按平台导入 Win32 模块；scrcpy 路径分平台；`_kill_scrcpy_by_marker` 加 POSIX 实现；新增 `DeviceMirror` 抓帧线程 + `ScrcpyContainer` 双模绘制；`_start_all` / `_try_embed` / `_check_scrcpy` / `_enable_scrcpy` / `_disable_scrcpy` / `_restart_scrcpy` / `closeEvent` 各加 Linux 分支 |
| `src/config.py` | 加 POSIX adb 搜索路径；`bundled_adb_rel()` / `_bundled_adb_candidates()` 按平台返回随包 adb 位置；错误提示分平台 |
| `src/settings.py` | `DEFAULTS['adb.path']` 改用 `bundled_adb_rel()` |
| `src/emulator.py` | `winreg` 平台桩（非 Windows 时 `_reg_open` 统一返回 `None`） |
| `src/u2dev.py` | `subprocess.CREATE_NO_WINDOW` 加平台守卫（原来在 Linux 上会直接 `AttributeError`） |
| `tools/fetch_scrcpy.py` | 加 Linux 分支（下 `scrcpy-linux-x86_64-v*.tar.gz`，解压后补 `0o755`） |
| `config.example.yaml` | `adb.path` 默认改为 `""`（自动搜索） |
| `tools/linux_android_check.sh` | 新增：设备体检 |
| `tools/linux_android_udev.sh` | 新增：装 udev 权限 |
| `tools/smoke_gui.py` | 新增：无头 GUI 冒烟测试 |
