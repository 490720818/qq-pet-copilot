# QQ 宠物自动化助手（qq-pet-copilot）

基于 uiautomator2 控件定位 + RapidOCR 文字识别的 QQ 宠物自动托管工具（分辨率无关）。
PyQt6 图形界面内嵌 scrcpy 实时画面，任务队列自动调度，按金币和**学习/工作时长**规则推进，
并自动处理**被雇佣召回**、**体力/清洁照顾**、**好友护理/雇佣**等日常。

> **游戏机制注意：护理相关勋章如果要拿的话不能一键！！！**
> 一键护理不计入勋章进度，要拿勋章必须把护理方式配成"ocr检测"手动喂食/洗澡
> （配置项 `care.method` / `friend_care.method`）。
> 推荐使用调度任务中设置时间段来规划每天的任务。

## 功能

- **任务队列调度（默认 `task_queue`）**：按 `tasks.order` 顺序扫描执行，每个任务独立
  enabled / trigger（interval 间隔 / daily 每日时间点窗口）/ 执行时间窗 / 成功失败退避
  （失败退避统一由设置页"任务失败重试间隔"`tasks.failure_interval` 控制）；
  冒险/学习/打工/雇佣好友互斥，作为主任务组统一调度且**非阻塞延时收尾**（进行中 OCR 剩余
  时间登记 pending，到点自动收尾计数，期间先跑其他任务）；踩踩/PK/好友护理/被雇佣检查按各自
  定时与次数排期，失败自动延后重试。另有旧 `legacy` 引擎（顺序写死）可切换。
- **学习场景**：出门 → 学校 → OCR 识别学园阶段（初级/中级/高级/进修，选课顺序自动适配）→
  归位选课（力量/智力/魅力）→ 上课 → 下课计数；毕业自动切下一阶段课程。
  每节按学园累计学习时长：初级 10 / 中级 20 / 高级 30 / 进修 45 分钟。
- **打工场景**：出门 → 小镇 → OCR 识别打工地点进入（设置页下拉可选 8 个地点）→
  按 `work.duration` 选时长（10分钟/45分钟/2小时）→ 顺带雇佣好友 → 开工。
  每次按所选时长累计打工时长。
- **学习工作时长上限**：学习/打工时长按学园与所选时长结算累计，
  累计 >= `schedule.daily_hour_limit`（小时，0=不限）后**今天不再学习只打工**；
  首次运行新版本时，老进度只有次数会按旧版点数系数自动换算成时长（只迁移一次）。
- **冒险场景**：每天到达配置时间后优先冒险，连跑 `adventure.batch` 次；
  可开启"天色不对"自动召回（点完"确认召回"会验证生效，召回后直接回出门页连跑）。
- **被雇佣检查/召回**：按时间段定时出门检测被雇佣中，按配置"等到 25/75"或"立刻召回"处理，
  召回自动计数后回主页面；被雇佣期间主任务不触发。
- **好友护理 / 雇佣好友**：按时间段 + 调度间隔巡检指定好友家（ocr检测/一键护理）；
  雇佣好友会检测雇佣 CD、出门前预检进行中活动，主动延后不误点。
- **踩踩 / PK**：好友互动按各自 `start_time` 与每天次数调度；踩踩自动跳过已踩过的好友，
  PK 每个好友可打 3 次、打完自动切换下一个好友。
- **状态照顾**：任务前读取体力/清洁/心情，按阈值自动喂食、洗澡（持续按压搓洗，
  搓洗按回合复测、连续不提升自动抬手重按自愈，达到上限仍不达标则跳过本次洗澡）；支持一键护理。
- **控制方案**（设置页下拉）：`injectInputEvent`（默认，真机推荐，uiautomator2 事件注入）/
  `minitouch`（模拟器推荐，openstf minitouch socket 直发，更快更稳）；minitouch 因
  非 Root/SELinux 不可用时会自动回退 injectInputEvent 并写回配置。
- **异常自动恢复**：页面错乱先回主页面重进场景自愈，仍失败走"重启设备/重启游戏"
  （模拟器多实例自动探测分步停/启）→ 重开 QQ → 回宠物页；模拟器模式用
  qqpet-module-opener（frida 注入）打开宠物主页并**保持注入**（好友访问/踩踩/PK 持续可用）；
  模拟器重启后 adb 抖动会先等回线，不再误判整机重启；多开实例互不干扰。
- **失败告警通知**：主任务多次重试仍失败时发 Windows Toast + OnePush 多渠道推送
  （Bark / PushPlus / Server酱 / SMTP / 自定义 webhook），并附当前手机截图。
- **每日计数与时长持久化**：各场景次数与累计时长按天记录在 `runs/*.json`（含历史），
  中途停止重跑接着计数，跨天自动归档清零（单账号，不按账号拆分）。
- **GUI**：左侧 scrcpy 实时画面嵌入，右侧日志，顶部状态条实时显示体力/清洁/金币与
  调度状态；日志页"今日"显示 `已学习/工作/总时长（小时）0.0/0.0/0.0`；
  调度/任务/设置页可视化修改 config.yaml，保存后**热加载即时生效**（无需重启）；
  右上角"手动重启"按配置执行一次异常恢复，恢复后自动重启调度器；
  设置页"检查更新"：启动后自动检查一次、之后每 6 小时一次，发现新版本显示 Release 下载链接。
- **分辨率无关定位**：优先 u2 控件选择器，游戏内自绘按钮用 RapidOCR（PP-OCRv6 tiny）
  整屏文字识别，换分辨率/机型无需改代码。

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
   - 首次运行会自动在 exe 旁生成 `config.yaml` 和 `runs/` 目录；scrcpy、OCR 模型、frida-server、
     minitouch 等资源都已打进包内，无需联网下载。
   - Windows 若提示"已保护你的电脑"，点"更多信息 → 仍要运行"（exe 未签名属正常现象）。
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
（scrcpy、模拟器版的 frida-server、minitouch 首次运行缺失时会自动下载到 `resources/`，无需手动拉取；
如需手动拉取也可运行 `tools/fetch_scrcpy.py` / `tools/fetch_frida_server.py` / `tools/fetch_minitouch.py`。）

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
`main.py` 启动时也会后台检查补齐 scrcpy / frida-server / minitouch。
**打开宠物主页后 hook 保持注入不解除**（好友访问/踩踩/PK 的跳转接管需要持续生效；
QQ 重启后恢复流程会自动重新注入）。注入前会等 QQ 启动稳定（避免被启动流程顶回主界面）。
（Frida 17 起 Java 桥不再内置，注入前会自动用 `frida-tools` 自带的 `frida-java-bridge` 补桥，效果与上游一致。）

> 提示：两种方式首次连接手机时，uiautomator2 都会自动往手机安装 atx-agent，需在手机弹窗上允许安装。

## 配置（config.yaml，主要项）

| 配置 | 说明 |
| --- | --- |
| `adb.path` / `adb.device_serial` | adb 路径（默认用 scrcpy 自带）/ 设备序列号（空 = 第一台） |
| `control.method` | 控制方案：`injectInputEvent`（真机推荐，默认）/ `minitouch`（模拟器推荐） |
| `school.attribute` | 属性点课程：力量 / 智力 / 魅力 |
| `school.times_per_day` | 每天学习次数上限，0 不限 |
| `work.location` | 打工地点（设置页下拉：风铃旅社/彩虹画室/迷雾侦探所/星尘魔法塔/咕噜厨房/竹影武馆/云朵梦舍/闪耀星屋） |
| `work.duration` | 打工时长：10分钟 / 45分钟 / 2小时 |
| `work.times_per_day` | 每天打工次数上限，0 不限 |
| `schedule.coin_threshold` | 金币阈值：>= 优先学习，< 先打工 |
| `schedule.daily_hour_limit` | 学习工作时长上限（小时，0=不限）：累计学习+打工时长 >= 上限后只打工 |
| `schedule.check_interval` | 上课/打工/冒险/被雇佣进行中状态的统一检查间隔（秒） |
| `schedule.back_method` | 返回方式：`系统返回`（Android 返回键，默认）/ `返回图标`（定位 back 按钮点击） |
| `adventure.times_per_day` / `start_time` / `batch` | 每天冒险次数 / 调度时间（HH:MM）/ 单轮连跑次数 |
| `adventure.skip_bad_weather` | 遇到"天色不对"自动召回计入一次冒险 |
| `care.energy_threshold` / `clean_threshold` | 体力 / 清洁阈值，低于则喂食 / 洗澡 |
| `friend_care.*` / `hire_friend.*` | 好友护理 / 雇佣好友的开关、时间段、好友名、调度间隔、次数 |
| `employed.*` | 被雇佣检查的开关、时间段、间隔、召回策略 |
| `tasks.failure_interval` | 所有任务统一的失败重试间隔（秒，设置页"任务失败重试间隔"） |
| `recover.method` / `recover.emulator_restart_cmd` | 异常恢复方式：重启设备 / 重启游戏；模拟器可配 MuMuManager 重启命令 |

> 旧版 `school_factor` / `work_factor` / `daily_point_limit` 仅保留用于首次运行迁移老进度
> （把已有次数换算成时长），不再参与调度、也不在设置页显示。

## 运行状态文件

| 路径 | 内容 |
| --- | --- |
| `runs/school_progress.json` | 学习次数 + 历史 + 当前学园（`school`）+ 今日学习时长（`study_secs`） |
| `runs/work_progress.json` | 打工次数 + 历史 + 本次打工时长（`duration`）+ 今日打工时长（`work_secs`） |
| `runs/adventure_progress.json` 等 | 冒险/踩踩/PK/被雇佣/雇佣好友/经验日常的每日次数 + 历史 |
| `runs/status_cache.json` | 宠物状态缓存（体力/清洁/心情/金币/库存），GUI 状态条读取 |
| `runs/queue_status.json` | 任务队列状态（当前任务/下一任务/倒计时），GUI 调度页读取 |
| `runs/logs/YYYY-MM-DD.log` | 按天的运行日志 |

## 单模块测试

```bash
.venv/Scripts/python scenarios/runner.py --test coins              # 只测主页金币 OCR
.venv/Scripts/python scenarios/runner.py --test recover           # 只测异常恢复链路
.venv/Scripts/python scenarios/runner.py --test opener            # 模拟器：直接用 opener 打开宠物主页
.venv/Scripts/python scenarios/runner.py --test work.select_place # 只跑某个阶段方法
.venv/Scripts/python scenarios/runner.py --test care.read_status  # 只测体力/清洁识别
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
（`tools/fetch_ocr_models.py`）、minitouch（`tools/fetch_minitouch.py`）；模拟器版还会把
`assets/qqpet-module-opener/` 的 hook JS 和 `resources/frida-server/` 的离线包打进 exe
（默认 x86_64，本地已有 xz 直接用，缺失时尝试从 GitHub 下载）。
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
main.py               # PyQt6 GUI 入口（scrcpy 嵌入 + 日志/调度/统计/任务/设置 + 手动重启）
build.py              # PyInstaller 打包脚本（--emulator / --all 打模拟器版）
config.yaml           # 全部可调配置
scenarios/
  runner.py           # 统一调度器（task_queue 任务队列 / legacy 两种引擎）
  school.py           # 学习场景（学园识别、选课、毕业处理）
  work.py             # 打工场景（OCR 选地点、选时长、雇佣好友）
  adventure.py        # 冒险场景（连跑、天色不对召回）
  care.py             # 体力/清洁检查与喂食/洗澡（搓洗）
  visit.py            # 踩踩（好友列表导航基类）
  pk.py               # PK
  friend_care.py      # 好友护理
  hire_friend.py      # 雇佣好友
  employed.py         # 被雇佣检测
src/
  u2dev.py            # uiautomator2 封装 + 控制方案（injectInputEvent/minitouch）+ 设备掉线重连
  locators.py         # UI 定位注册表（u2 控件选择器 + OCR 文字 + 相对坐标兜底）
  scenario.py         # 场景基类：定位导航、回主页面、等待/延时收尾、被雇佣召回、鼓励宠物
  recover.py          # 异常恢复链路（重启设备/游戏、模拟器实例重启、opener 重试）
  emulator.py         # 多模拟器实例自动探测与分步停/启（MuMu/雷电/夜神/蓝叠/逍遥）
  opener.py           # 模拟器模式：frida 注入打开宠物主页（保持注入）
  adb/device.py       # adb 封装：设备在线管理、屏幕属性读取、远程模拟器 connect
  ocr.py              # RapidOCR 封装（整屏 OCR、剩余时间/面板解析）
  coins.py            # 主页金币 OCR
  progress.py         # 日志 + 每日次数/时长持久化（含历史、学园、时长迁移）
  status_cache.py     # 宠物状态缓存
  queue_status.py     # 任务队列状态缓存
  notify.py           # 失败告警通知（Windows Toast + OnePush）
  settings.py         # config.yaml 读写（保留注释）
  config.py           # 配置加载与路径规划（兼容 PyInstaller）
assets/
  qqpet-module-opener/
    open_qqpet_module.js     # hook JS（取自上游 qqpet-module-opener，手动更新，入库）
resources/                   # 第三方二进制/离线包（不入库，build 时下载或本地放入）
  scrcpy-win64/              # scrcpy 二进制
  frida-server/              # frida-server 离线包（模拟器版打包时带上）
  minitouch/                 # minitouch 控制方案二进制（x86_64 / arm64-v8a）
tools/
  fetch_scrcpy.py / fetch_frida_server.py / fetch_minitouch.py / fetch_ocr_models.py
  dump_hierarchy.py / test_locator.py / capture_visit_jump.py
```

## 定位方式

界面元素定位登记在 `src/locators.py` 的 `LOCATORS` 表：优先 u2 控件选择器
（原生弹窗等），游戏内 canvas 自绘按钮靠 OCR 文字。游戏更新后如识别失败，
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
- [minitouch](https://github.com/DeviceFarmer/minitouch)
  底层触摸注入工具（模拟器控制方案），socket 直发触摸事件。

## 免责声明

本项目仅供学习研究自动化与 OCR 识别技术使用。自动化操作可能违反游戏服务条款，
由此产生的一切后果由使用者自行承担。

## 许可证

本项目采用 GNU General Public License v3.0 (GPLv3)，详见根目录 [LICENSE](LICENSE)。
