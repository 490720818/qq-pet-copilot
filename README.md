# QQ 宠物自动化助手（qq-pet-copilot）

基于 uiautomator2 控件定位 + RapidOCR 文字识别的 QQ 宠物自动托管工具（分辨率无关）。
PyQt6 图形界面内嵌 scrcpy 实时画面，按金币和每日点数规则自动调度，并自动处理**被雇佣召回**和**体力/清洁照顾**。

## 功能

- **统一调度器**：每轮先做状态检查（体力/清洁/被雇佣），再按 冒险定时 → 金币/点数规则 → 学习/打工一轮 推进；
  金币充足优先学习、不足先打工，学习×20 + 打工×45 超过每日点数上限后当天只打工，第二天自动清零；
  支线任务（冒险/踩踩/PK）按各自定时与次数排期，失败自动延后重试，互不阻塞。
- **学习场景**：出门 → 学校 → 拖动归位选课（力量/智力/魅力）→ 上课 → 下课计数。
- **打工场景**：出门 → 小镇 → OCR 识别打工地点进入 → 选最高收益工作 → 雇佣好友 → 开工。
- **冒险场景**：每天到达配置时间后优先冒险，满次数后等第二天；可开启遇到"天色不对"自动召回计入次数。
- **被雇佣召回**：出门时检测到被雇佣中，按配置选择"等到 25/75"（分成收益最高时召回）或"立刻召回"，
  召回自动计数后回主页面。
- **踩踩 / PK**：好友互动按各自 `start_time` 与每天次数调度；踩踩自动跳过已踩过的好友，
  PK 每个好友可打 3 次、打完自动切换下一个好友。
- **状态照顾**：任务前读取体力/清洁/心情，按阈值自动喂食、洗澡（持续按压搓洗）；支持一键护理。
- **异常自动恢复**：页面错乱先回主页面重进场景自愈，仍失败走 adb reboot → 重启 QQ → 点 `Q宠-*` 入口回宠物页，
  恢复后自动重连继续调度；模拟器模式恢复时改用 qqpet-module-opener 注入打开宠物主页。
- **模拟器模式**（`--emulator`）：Root 模拟器里手机 QQ 的"QQ宠物"搜索卡片可能不下发跳转地址
  （提示"请在手机端使用"），通过 Frida 注入已登录的 QQ 进程、直接打开宠物主页，页面打开后立即解除注入。
  hook 脚本取自 [qqpet-module-opener](https://github.com/yikehuang/qqpet-module-opener)（只保留这一个 JS，手动更新），
  frida-server 默认离线打包 x86_64，无需联网。
- **失败告警通知**：主任务多次重试仍失败时发 Windows Toast + OnePush 多渠道推送
  （Bark / PushPlus / Server酱 / SMTP / 自定义 webhook），并附当前手机截图。
- **多账号支持**：识别账号后进度与状态自动分流到 `runs/accounts/<账号>/`（旧单账号进度自动迁移），
  GUI 统计页可按账号切换。
- **每日计数持久化**：各场景次数按天记录在 `runs/*.json`（含历史），
  中途停止重跑接着计数，跨天自动归档清零。
- **GUI**：左侧 scrcpy 实时画面嵌入（关屏镜像，设备重启自动重拉重嵌），右侧日志 + 开始/停止按钮，
  顶部状态条实时显示体力/清洁/金币等；统计页展示各任务近 N 天次数的平滑折线图；
  设置页可视化修改 config.yaml，保存后即时生效。
- **分辨率无关定位**：优先 u2 控件选择器，游戏内自绘按钮用 RapidOCR（PP-OCRv6 tiny）整屏文字识别，
  无文字固定元素按 720×1280 参考坐标等比换算，换分辨率/机型无需改代码。

## 快速开始

两种方式二选一：**直接下载 Releases 打包好的 exe 使用**（推荐，无需 Python），或 **源码运行**（开发者 / 需要改代码）。

### 方式一：直接下载 Releases 打包好的 exe（无需 Python）

1. 打开 [Releases 发布页](https://github.com/490720818/qq-pet-copilot/releases)，下载对应版本并解压：
   - `QQPetCopilot-<版本>-windows-x64.zip` —— **普通版**：真机（物理手机）使用，无需 Root；
   - `QQPetCopilotEmulator-<版本>-windows-x64.zip` —— **模拟器版**：Root 模拟器（MuMu/雷电 等）使用，
     内置 qqpet-module-opener hook + frida-server 离线包，绕过模拟器 QQ 搜索卡片空入口。
   - **模拟器版使用前提**：推荐使用最新版本 MuMu 模拟器（下载地址 [https://mumu.163.com/](https://mumu.163.com/)），
     模拟器内安装 **QQ 9.3.25 及以上版本**并登录账号后，再开启脚本。
2. 双击解压出的 `QQPetCopilot.exe` / `QQPetCopilotEmulator.exe` 启动：
   - 首次运行会自动在 exe 旁生成 `config.yaml` 和 `runs/` 目录；scrcpy、OCR 模型、frida-server 等资源都已打进包内，无需联网下载。
   - Windows 若提示"已保护你的电脑"（exe 未签名），点 **更多信息 → 仍要运行**。
3. 手机开 USB 调试并连接电脑（或启动 Root 模拟器），在 GUI 设置页（或直接编辑 exe 旁的 `config.yaml`）填好 `adb.device_serial`。
4. 点**开始**运行。模拟器版 exe 启动即默认模拟器模式，无需带参数。
   - **多开**（多台设备 / 多个账号）：把整个解压目录复制成多份，每份配置各自的设备序列号，
     各实例的 `config.yaml` / `runs/` 互相独立，互不影响。

### 方式二：源码运行（开发者 / 需要改代码）

需要 **Python 3.12**：

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # 含 frida（模拟器模式注入用）
```

1. 手机开 USB 调试并连接电脑（可用 `resources/scrcpy-win64/adb.exe devices` 确认）。
2. 编辑 `config.yaml`（或在运行后在 GUI 设置页里改）。
3. 启动：

```bash
.venv/Scripts/python main.py            # GUI：画面 + 日志 + 控制按钮
.venv/Scripts/python scenarios/runner.py  # 控制台模式（无 GUI）
```

GUI 打开后自动嵌入手机画面，点**开始**启动调度器（子进程），点**停止**立即结束。
（scrcpy、模拟器版的 frida-server 首次运行缺失时会自动下载到 `resources/`，无需手动拉取；
如需手动拉取也可运行 `tools/fetch_scrcpy.py` / `tools/fetch_frida_server.py`。）

**模拟器模式**（Root 模拟器，如 MuMu）：推荐使用最新版本 MuMu 模拟器（下载地址
[https://mumu.163.com/](https://mumu.163.com/)），模拟器内安装 **QQ 9.3.25 及以上版本**并登录账号后再启动；
QQ 搜索卡片打不开宠物主页时，带参数启动：

```bash
.venv/Scripts/python main.py --emulator --emulator-device 127.0.0.1:7555
.venv/Scripts/python scenarios/runner.py --emulator --emulator-device 127.0.0.1:7555
```

`--emulator-device` 可省略（默认用 `config.yaml` 的 `adb.device_serial`，模拟器填 127.0.0.1:7555 这类地址）。
首次运行会把内置的 frida-server 推送到模拟器并启动（需已开启 Root 与 ADB）；源码运行时若
`resources/frida-server/` 里没有 xz，会自动用 `tools/fetch_frida_server.py` 下载（GitHub 失败自动试镜像），
`main.py` 启动时也会后台检查补齐 scrcpy / frida-server。
**打开宠物主页后 hook 保持注入不解除**（好友访问/踩踩/PK 的跳转接管需要持续生效；
QQ 重启后恢复流程会自动重新注入）。注入前会等 QQ 启动稳定（避免被启动流程顶回主界面）。
（Frida 17 起 Java 桥不再内置，注入前会自动用 `frida-tools` 自带的 `frida-java-bridge` 补桥，效果与上游一致。）

> 提示：两种方式首次连接手机时，uiautomator2 都会自动往手机安装 atx-agent，需在手机弹窗上允许安装。

## 配置（config.yaml）

| 配置 | 说明 |
| --- | --- |
| `adb.path` / `adb.device_serial` | adb 路径（默认用 scrcpy 自带）/ 设备序列号（空 = 第一台） |
| `school.attribute` | 属性点课程：力量 / 智力 / 魅力 |
| `school.times_per_day` | 每天学习次数上限，0 不限 |
| `work.location` | 打工地点（OCR 文字匹配），如 风铃旅社 / 闪耀星屋 |
| `work.times_per_day` | 每天打工次数上限，0 不限 |
| `work.employ_scroll_limit` | 找雇佣按钮的拖动次数上限 |
| `schedule.coin_threshold` | 金币阈值：>= 优先学习，< 先打工 |
| `schedule.school_factor` / `work_factor` / `daily_point_limit` | 每日点数规则：学习×系数 + 打工×系数 > 上限后只打工 |
| `schedule.check_interval` | 上课/打工/冒险/被雇佣进行中状态的统一检查间隔（秒） |
| `adventure.times_per_day` / `start_time` | 每天冒险次数 / 冒险调度时间（HH:MM） |
| `care.energy_threshold` / `clean_threshold` | 体力 / 清洁阈值，低于则喂食 / 洗澡 |

## 运行状态文件

| 路径 | 内容 |
| --- | --- |
| `runs/school_progress.json` 等 4 个 | 各场景每日次数 + 历史记录（跨天自动归档） |
| `runs/logs/YYYY-MM-DD.log` | 按天的运行日志 |

## 单模块测试

```bash
.venv/Scripts/python scenarios/runner.py --test coins              # 只测主页金币 OCR
.venv/Scripts/python scenarios/runner.py --test opener             # 模拟器：直接用 opener 打开宠物主页
.venv/Scripts/python scenarios/runner.py --test work.select_place  # 只跑某个阶段方法
.venv/Scripts/python scenarios/runner.py --test care.read_status   # 只测体力/清洁识别
```

场景脚本也可单独跑：`python scenarios/school.py --times 1`（work / adventure / care 同理）。

模拟器诊断（QQ 更新后排查"访问好友"跳转）：用 [capture_visit_jump.py](tools/capture_visit_jump.py)
在真机/模拟器上抓 `mqqapi://qpet/open` 跳转的完整 URL 与 attrs，对比差异定位问题：

```bash
.venv/Scripts/python tools/capture_visit_jump.py -s 127.0.0.1:7555 -c   # 模拟器自动点
.venv/Scripts/python tools/capture_visit_jump.py -s ba286ada            # 真机手动点
```

## 打包 exe

```bash
.venv/Scripts/python build.py              # 单文件：dist/QQPetCopilot.exe（普通版）
.venv/Scripts/python build.py --onedir     # 目录模式
.venv/Scripts/python build.py --emulator   # 模拟器版：dist/QQPetCopilotEmulator.exe（内置 opener + frida）
.venv/Scripts/python build.py --all        # 普通版 + 模拟器版一起打包
```

`build.py` 打包前会自动下载 scrcpy（`tools/fetch_scrcpy.py`）、OCR 模型
（`tools/fetch_ocr_models.py`）；模拟器版会把 `assets/qqpet-module-opener/` 的 hook JS 和
`resources/frida-server/` 的离线包打进 exe（默认 x86_64，本地已有 xz 直接用，缺失时尝试从 GitHub 下载）。
hook JS 更新：QQ 更新导致打不开时，从上游
[qqpet-module-opener](https://github.com/yikehuang/qqpet-module-opener) 的
`src/open_qqpet_module.js` 手动同步到 `assets/qqpet-module-opener/open_qqpet_module.js` 后重新打包；
frida-server 换版本需同时改 `requirements.txt` 的 frida 版本与 `build.py` 的 `FRIDA_VERSION`。
打包后 `config.yaml` 首次运行自动复制到 exe 旁，`runs/` 也生成在 exe 旁；exe 旁 `runs/` 目录放同名资源
可覆盖包内资源（如 `runs/resources/scrcpy-win64/`、`runs/resources/frida-server/`），无需重新打包。冷启动需解压资源，会慢几秒。
模拟器版 exe 启动即默认开启模拟器模式，无需带参数。
注意：打包前请先关闭正在运行的 `QQPetCopilot.exe`（Windows 不允许覆盖被占用的 exe）。

## 目录结构

```
main.py               # PyQt6 GUI 入口（scrcpy 嵌入 + 日志 + 开始/停止/设置）
build.py              # PyInstaller 打包脚本（--emulator / --all 打模拟器版）
config.yaml           # 全部可调配置
scenarios/
  runner.py           # 统一调度器（金币/点数/冒险定时/状态检查）
  school.py           # 学习场景
  work.py             # 打工场景（OCR 选地点）
  adventure.py        # 冒险场景
  care.py             # 体力/清洁检查与喂食/洗澡
src/
  u2dev.py            # uiautomator2 封装：连接/截图/点击/滑动/持续按压
  locators.py         # UI 定位注册表（u2 控件选择器 + OCR 文字 + 相对坐标兜底）
  opener.py     # 模拟器模式：frida 注入打开宠物主页（设备/Root/frida-server/注入全流程）
  adb/device.py       # adb 封装：设备在线管理、屏幕属性读取、远程模拟器 connect
  scenario.py         # 场景基类：定位导航、回主页面、等待结束、被雇佣召回
  ocr.py              # RapidOCR 封装
  coins.py            # 主页金币 OCR
  progress.py         # 日志 + 每日次数持久化（含历史）
  settings.py         # config.yaml 读写（保留注释）
  config.py           # 配置加载与路径规划（兼容 PyInstaller）
assets/
  qqpet-module-opener/
    open_qqpet_module.js     # hook JS（取自上游 qqpet-module-opener，手动更新，入库）
resources/                   # 第三方二进制/离线包（不入库，build 时下载或本地放入）
  scrcpy-win64/              # scrcpy 二进制
  frida-server/
    frida-server-*.xz        # frida-server 离线包（build.py --emulator 打包时带上）
```

## 定位方式

界面元素定位登记在 `src/locators.py` 的 `LOCATORS` 表：优先 u2 控件选择器
（原生弹窗等），游戏内 canvas 自绘按钮靠 OCR 文字，少数无文字纯图形元素
用 720x1280 参考坐标按当前分辨率等比换算。游戏更新后如识别失败，
用 `--test` 单测真机逐屏校准该表即可，无需重新截图做模板。

## 致谢

- [scrcpy](https://github.com/Genymobile/scrcpy)
  Android 画面镜像与控制工具，本项目的实时画面嵌入和 adb 能力实现。
- [qqpet-module-opener](https://github.com/yikehuang/qqpet-module-opener)
  模拟器初始化 QQ 宠物 SDK 并直接打开宠物主页，本项目的模拟器模式基于这一方案实现
  （只保留其 hook JS `assets/qqpet-module-opener/open_qqpet_module.js` 并手动跟随更新）。
- [frida](https://frida.re) / [frida-tools](https://github.com/frida/frida-tools)
  注入框架；Frida 17 起 Java 桥不再内置，运行时用 frida-tools 自带的 `frida-java-bridge` 补桥。
- [RapidOCR](https://github.com/RapidAI/RapidOCR) / [PP-OCRv6](https://github.com/PaddlePaddle/PaddleOCR)
  文字识别引擎与模型（本项目用 PP-OCRv6 tiny），游戏内自绘按钮、金币/状态等数字识别全靠它。
- [uiautomator2](https://github.com/openatx/uiautomator2)
  Android UI 自动化框架，控件定位、点击/滑动与截图实现。

## 免责声明

本项目仅供学习研究自动化与 OCR 识别技术使用。自动化操作可能违反游戏服务条款，
由此产生的一切后果由使用者自行承担。

## 许可证

本项目采用 GNU General Public License v3.0 (GPLv3)，详见根目录 [LICENSE](LICENSE)。
