# AGENTS.md

给 AI 代理的项目说明：结构、约定、常用命令。

## 项目概述

QQ 宠物自动化托管脚本。技术栈：Python 3 + uiautomator2（画面、输入与控件定位）+
RapidOCR（文字/数字识别）+ PyQt6（GUI）。UI 定位分辨率无关：
优先 u2 控件选择器，游戏内 canvas 自绘按钮靠 OCR 文字（`src/locators.py` 注册表），
少数无文字固定元素用 720×1280 参考坐标按当前分辨率等比换算。
平台：Windows（Git Bash 环境），目标设备：Android 手机（竖屏）。

## 运行与测试命令

```bash
PY=.venv/Scripts/python          # 项目虚拟环境，所有命令用它

$PY -m py_compile <files>        # 改完代码最基本的验证，必做
$PY main.py                      # GUI（scrcpy 嵌入 + 调度控制）
$PY scenarios/runner.py          # 控制台调度器
$PY scenarios/runner.py --test <target>   # 单模块测试：coins / recover / school.X / work.X / adventure.X / care.X
$PY build.py                     # PyInstaller 打包（onefile），--onedir 目录模式
```

- 无设备测试：用 `DeviceScenario.__new__(DeviceScenario)` 跳过 u2 设备连接，
  桩掉 `see()` / `click()` 后调用被测方法（见历史上对 `wait_end` 的测试方式）。
- 无头 GUI 测试：`QT_QPA_PLATFORM=offscreen $PY -c ...`，
  并补丁 `MainWindow._start_all = lambda self: None` 避免拉起 scrcpy/调度器。
- **测试时不要污染真实进度文件**（`runs/*.json`）：涉及计数时把进度文件
  重定向到临时目录（monkeypatch `src.progress` 里的文件常量），或事后回退。

## 目录与模块职责

| 路径 | 职责 |
| --- | --- |
| `main.py` | PyQt6 GUI：scrcpy 窗口嵌入（SetParent）、选项卡（日志/统计/设置）、调度器子进程控制、scrcpy 看门狗（设备重启后自动重拉重嵌入） |
| `src/stats_chart.py` | 统计页：各任务近 N 天次数的平滑折线图（QPainter 自绘 + Catmull-Rom 平滑，数据来自 `runs/accounts/<账号>/*.json` 的 history，多账号下拉切换） |
| `scenarios/runner.py` | 统一调度器：每轮 = 状态检查 → 冒险（定时优先）→ 点数规则 → 金币 OCR → 学习/打工一轮；场景异常分级重试（回主页面重进 → `recover()` 重启恢复）；都失败时主任务（学习/打工）发告警通知（`src/notify.py`）并退出，支线任务（冒险/踩踩/PK）延后 `SIDE_TASK_RETRY_DELAY` 秒重排期 |
| `scenarios/school.py` `work.py` `adventure.py` `care.py` `visit.py` `pk.py` | 各场景，均继承 `DeviceScenario`（`pk.py` 继承 `visit.py` 复用好友导航） |
| `src/scenario.py` | 场景基类：截图/u2+OCR 定位点击/回主页面/等待结束/被雇佣召回/四种进行中状态检测 |
| `src/recover.py` | 异常恢复链路：adb reboot → 等开机 → 启动 QQ → 点 `Q宠-*` 入口（descriptionStartsWith 前缀匹配，后缀数字不固定）回宠物页，返回新 U2Device |
| `src/u2dev.py` | uiautomator2 封装：连接（含 atx-agent 首装）、截图、`rel()` 参考坐标换算、`d.touch` 持续按压 |
| `src/locators.py` | UI 定位注册表 `LOCATORS`：名字 → u2 选择器 / OCR 候选文案 / rel 兜底坐标；`see()` / `see_all()` |
| `src/adb/device.py` | adb 封装：设备在线管理（start-server）、屏幕尺寸读取（scrcpy 嵌入比例用）、`reboot_and_wait()` / `launch_app()`（异常恢复用） |
| `src/ocr.py` / `src/coins.py` | RapidOCR 封装；主页金币 = 顶部状态栏最右侧数值（全屏 OCR） |
| `src/progress.py` | `log()`（控制台+文件+监听器）、每日次数持久化（含 history，跨天归档）、`count_cross` 交叉计数；多账号：识别到账号（status_cache 的 last_account）后 `load/save_progress` 自动重定向到 `runs/accounts/<账号>/`，首次识别迁移单账号时期的旧进度文件；`known_accounts()` 供 GUI 按账号显示（当前账号排最前） |
| `src/status_cache.py` | 账号状态缓存（`runs/status_cache.json`，按账号名称组织，兼容多账号）：体力/清洁/心情（care 状态面板 OCR 后）、金币（主页 OCR 后）、香皂/饼干（喂食/洗澡结束时 OCR 控件附近小图；库存角标无文字，取离 `feed_10`/`shower_10` 控件最近的数字）；一键护理后清空体力/清洁/心情/饼干/香皂；GUI 日志页顶部状态条每 5 秒读一次 |
| `src/config.py` | dataclass 配置 + 路径规划：`APP_ROOT`（可写）/ `RESOURCE_ROOT`（包内资源），`resource_path()` APP_ROOT 优先 |
| `src/settings.py` | ruamel 往返读写 config.yaml（保留注释），GUI 设置页用 |
| `src/notify.py` | 失败告警通知：Windows Toast（winotify）+ OnePush 多渠道推送（Bark/PushPlus/Server酱/SMTP/自定义 webhook 等），发送失败只记日志 |
| `tools/dump_hierarchy.py` | 抓当前屏幕控件树 XML 存到 `xml/page.xml`（校准 locators 的 xpath/content-desc 用；`xml/` 已 git 排除） |
| `tools/fetch_scrcpy.py` | 从官方 GitHub Release 下载解压 scrcpy（win64）到 `scrcpy-win64/`（该目录不入库）；`--version` 指定版本、`--force` 强制覆盖，build.py / CI 打包前自动调用 |
| `tools/test_locator.py` | 测试 locator 的 xpath 在当前页面的命中稳定性（连设备连续多轮 dump，统计 live/snapshot 两种调用方式的命中率与 bounds 漂移，定位深层 xpath 时有时无/位置漂移问题） |

## 关键约定（改动时必须遵守）

- **定位方式**：新 UI 元素登记到 `src/locators.py` 的 `LOCATORS`（沿用 `xxx_in` 进行中标志、
  `xxx_end` 结束标志、`quit`、`back`、`main_sign` 命名）。优先 xpath / u2 选择器
  （`main_sign` 是 xpath `//*[@content-desc="金币胶囊"]` 检测主页面——只有自己主页面
  有该元素，好友宠物页没有；care 解析账号名时另有"加好友"守卫防止把好友昵称当账号）；
  进行中状态文字（`xxx_in`）用整屏 OCR 关键词（状态区域 xpath  bounds 随页面
  结构漂移，裁剪 OCR 不可靠；同一张 screen 连续多次 `see()` 共享一次整屏 OCR，
  见 `_ocr_texts_cached`）；整屏 OCR 统一走 `src/ocr.py` 的 `ocr_fullscreen()`
  （先保持长宽比缩到接近 720×1280 再识别，坐标还原回原图）；游戏内按钮用整屏 OCR 文案（候选列表按优先序）；
  只有无文字纯图形且位置固定的元素才用 `rel` 兜底
  （rel 是"必然命中"，不能用于"检测是否存在"）。
  位置固定、只需点击的元素可加 `'cache': True`（如 `back`）：第一次命中后坐标
  记入 `_locate_cache`，之后 `see()` 直接返回缓存点不再识别。
  位置固定的裁剪区域（如宠物状态面板 `status_region`）登记 xpath + `'cache': True`
  后用 `see_bounds()` 取范围：第一次命中后 bounds 记入 `_bounds_cache`，之后直接复用。
- **参考坐标**：场景里的固定点位一律写 720×1280 参考坐标，点击/拖动走
  `click_rel()` / `swipe()`（内部按当前分辨率换算），不得直接点绝对像素。
  例外：洗澡搓洗点位按分辨率百分比（`care.py` 的 `SCRUB_TOP_PCT` / `SCRUB_BOTTOM_PCT`），
  起点取 `shower_10` 控件中心。
- **一轮语义**：场景的 `run(max_times, max_rounds)` 中一轮 = 一节课 / 一次打工 / 一次冒险，
  结束后回主页面；执行器以 `max_rounds=1` 调用，每轮后重新判断金币/点数。
- **出门处理**：`goto_*` 出门后必须调 `wait_busy_end()` 检测四种进行中状态
  （school/work/adventure/employed）；等完的活动计入对应进度后**本轮直接结束**，
  由执行器重新判断限制条件，不得继续原定任务。
- **计数时机**：检测到 `xxx_end` 点完 `quit` 就计数（`save_progress`），再回主页面；
  被雇佣在召回点 quit 时由基类 `count_cross('employed')` 计数，场景 `run()` 不得重复计。
- **主页面点 back 会退出游戏**：`ensure_main_page` 必须保留 `BACK_GRACE_ATTEMPTS`
  宽限（连续多次识别不到 `main_sign`（"出门"）才允许点 back）。
- **OCR 置信度**：命中下限 `src/locators.py` 的 `OCR_MIN_SCORE`（默认 0.5）。
- **异常分级重试**：场景抛异常 → 先回主页面重进场景重试一次（页面错乱多半能
  自愈，不必重启）→ 仍失败才走 `Runner.recover()`（`src/recover.py`：adb reboot →
  启动 QQ → 等并点 `Q宠-*` 入口回宠物页）→ 最后再试一次；连续 `RECOVERY_LIMIT`
  次恢复仍失败才放弃恢复。恢复成功后必须把新 U2Device 刷新到各场景的 `dev`。
  多次重试均失败按任务类型分流：主任务（学习/打工）发告警通知（`src/notify.py`：
  Windows Toast + OnePush，附当前手机截图存 `runs/alert_*.png`）并退出调度器
  （SystemExit）；支线任务（冒险/踩踩/PK）不退出——抛 `ScenarioFailed`，
  由调度循环重新排期延后 `SIDE_TASK_RETRY_DELAY` 秒重试（`retry_after`，
  参考 qq-farm-copilot 的失败间隔队列机制），先执行其他任务。
- **配置改动**：新配置项加到 `config.yaml` + `src/config.py` 的 dataclass +
  `main.py` 的 `SETTING_FIELDS`（设置页表单）三处。
- **GUI 线程纪律**：调度器是子进程（`scenarios/runner.py`，打包后为 `exe --runner`），
  日志经 stdout → 队列 → QTimer 上屏；worker 线程不直接碰 Qt 控件。
- **踩踩/PK 调度**：执行器主循环在主页面按各自 `start_time` / 当天次数 / 失败延后期调度
  （`visit_due()` / `pk_due()`），跑对应场景 `run()` 完整流程；不做长等待插空
  （好友入口只在主页面，上课/打工/冒险等待页没有）。
- 控制台中文乱码是 Windows GBK 终端显示问题，日志文件（UTF-8）里是正常的，不要当 bug 修。
- 不再修改手机分辨率/密度（wm size/density）：定位分辨率无关，无此需求。

## 打包

`python build.py`（onefile）。`scrcpy-win64/` 不入库（二进制），打包前
`build.py` 自动调 `tools/fetch_scrcpy.py` 从官方 Release 拉取。路径约定：打包后
`APP_ROOT` = exe 所在目录（config.yaml 首启复制、runs/ 生成于此），
`RESOURCE_ROOT` = `sys._MEIPASS`。exe 旁放同名 `scrcpy-win64/` 可覆盖包内资源。
