# AGENTS.md

给 AI 代理的项目说明：结构、约定、常用命令。

## 项目概述

QQ 宠物自动化托管脚本。技术栈：Python 3 + uiautomator2（画面、输入与控件定位）+
RapidOCR（文字/数字识别）+ PyQt6（GUI）。UI 定位分辨率无关：
优先 u2 控件选择器，游戏内 canvas 自绘按钮靠 OCR 文字（`src/locators.py` 注册表），
少数无文字固定元素用 720×1280 参考坐标按当前分辨率等比换算。
平台：Windows（Git Bash 环境），目标设备：Android 手机（竖屏）。

游戏机制注意：**护理相关勋章如果要拿的话不能一键！！！**（一键护理不计入勋章进度，
要拿勋章必须把护理方式配成"ocr检测"手动喂食/洗澡，配置项 `care.method` /
`friend_care.method`）。

## 运行与测试命令

```bash
PY=.venv/Scripts/python          # 项目虚拟环境，所有命令用它（Python 3.12）

$PY -m py_compile <files>        # 改完代码最基本的验证，必做
$PY main.py                      # GUI（scrcpy 嵌入 + 调度控制）
$PY scenarios/runner.py          # 控制台调度器
$PY scenarios/runner.py --test <target>   # 单模块测试：coins / recover / opener / school.X / work.X / adventure.X / care.X / friend_care.X / hire_friend.X
$PY build.py                     # PyInstaller 打包（onefile），--onedir 目录模式
$PY build.py --emulator          # 模拟器版（内置 hook JS + frida-server 离线包 + frida），--all 普通版+模拟器版一起打
# 模拟器模式（Root 模拟器）：main.py / runner.py 加 --emulator [--emulator-device 127.0.0.1:7555]
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
| `main.py` | PyQt6 GUI：scrcpy 窗口嵌入（SetParent）、选项卡（日志/调度/统计/任务/设置；调度页 = 每任务 开关/执行间隔（每日时间）/启用时段/下次执行 只读表格，数据 = config.yaml + `runs/queue_status.json` 的 tasks 段；任务页 = 任务队列顺序 + 场景任务设置，设置页 = 连接/调度引擎/全局规则/告警）、调度器子进程控制、scrcpy 看门狗（设备重启后自动重拉重嵌入）、右上角"手动重启"按钮
（按 `recover.method` 执行一次异常恢复 `reenter_pet`，调度器在跑先停，恢复期间开始/停止按钮禁用，恢复完成自动启动调度器） |
| `src/stats_chart.py` | 统计页：各任务近 N 天次数的平滑折线图（QPainter 自绘 + Catmull-Rom 平滑，数据来自 `runs/*_progress.json` 的 history） |
| `scenarios/runner.py` | 统一调度器，两种引擎（`runner.engine`）：`task_queue`（默认，`TaskQueueRunner`：执行顺序由 `tasks.order` 配置，> 分隔越靠前越优先，不在 order 里不调度；每任务独立 enabled / trigger（interval 间隔 / daily 每日时间点窗口）/ enabled_time_range / success_interval / failure_interval，见 `tasks` 段）/ `legacy`（`Runner.run` 老主循环，顺序写死：护理 → 冒险 → 踩踩 → PK → 好友雇佣 → 好友护理 → 学习/打工）。共通：场景异常分级重试（回主页面重进 → `recover()` 重启恢复）；都失败时主任务（学习/打工）发告警通知（`src/notify.py`）并退出，支线任务延后重试（legacy 用 `SIDE_TASK_RETRY_DELAY`，队列用各任务 `failure_interval`） |
| `scenarios/school.py` `work.py` `adventure.py` `care.py` `visit.py` `pk.py` `friend_care.py` `hire_friend.py` `employed.py` | 各场景，均继承 `DeviceScenario`（`pk.py`/`friend_care.py` 继承 `visit.py` 复用好友导航；`hire_friend.py` 继承 `friend_care.py` 复用指定好友导航；`employed.py` 只做被雇佣检测，召回复用基类） |
| `src/scenario.py` | 场景基类：截图/u2+OCR 定位点击/回主页面/等待结束（阻塞 `wait_end` / 非阻塞延时收尾 `defer_busy_end`+`finish_pending`，OCR 剩余时间登记 `pending`）/被雇佣召回/四种进行中状态检测 |
| `src/recover.py` | 异常恢复链路：adb reboot → 等开机 → 启动 QQ → 点 `Q宠-*` 入口（descriptionStartsWith 前缀匹配，后缀数字不固定；入口紧凑双击用 minitouch 两连击——`d.click` JSON-RPC 往返慢，0.3s 间隔会被识别成两次单击进单击页，点不进主页 back 退回重试）回宠物页，返回新 U2Device；模拟器模式"重启设备"改为重启模拟器整机（MuMu 不支持 adb reboot，会把 adb 服务卡死；**opener 失败但设备在线时先强停 QQ 重开一次**——冷启动 QQ 首启失败比整机重启便宜得多），优先级：配置的 `recover.emulator_restart_cmd` > 留空自动探测模拟器实例分步停/启（`src/emulator.py`，serial 匹配多个实例时依次用 `emulator.type/name/path`、端口实际监听进程命令行消歧）> 回退 adb reboot，随后 connect → 等开机（`_adb_back_online` **不默认 kill-server**——kill-server 会把所有设备（其它模拟器实例/USB 真机）踢下线且 host:port 不会自动恢复，只在 connect **连续 `CONNECT_TIMEOUT_KILL_AFTER`=3 次**超时（服务疑似卡死，单次/偶发超时只是开机慢）才 kill-server 兜底并恢复之前在线过的所有远程设备） |
| `src/emulator.py` | 多模拟器实例自动探测（参考 ALAS module/device/platform）：MuMu 12/6/X、雷电 3/4/9、夜神、蓝叠 4/5、逍遥；exe→类型靠 `path_to_type`（exe 名+上级目录名），安装目录来源 = 卸载项注册表（子键名精确匹配）+ MuiCache/UserAssist（ROT13）+ 雷电 InstallDir 注册表；serial 算法 = vbox/nemu/memu 的 hostport→5555 转发正则（MuMu12 兜底 16384+32*index、雷电 5555+2*index、蓝叠4 固定 5555、蓝叠5 读 bluestacks.conf）；`scan_serials()`（GUI 设备下拉合并）、`find_instance(serial, type/name/path 消歧)`、`get_serial_pair()`（127.0.0.1:5555+X ↔ emulator-5554+X）、`restart_instance()` 按类型分步停/启（有控制台 exe 走控制台：MuMuManager/ldconsole/bsconsole/memuc，蓝叠5/MuMu6/X 杀进程再用主 exe 拉起）；serial 命中多个实例且配置消歧不够时，按端口实际监听进程命令行反查（`_match_by_listener`：MuMu12 每个实例的 .nemu 都转发 7555，MuMuVMMHeadless.exe 的 --comment 即实例名，0.0.0.0 通配与 127.0.0.1 精确绑定并存时精确绑定优先） |
| `src/u2dev.py` | uiautomator2 封装：连接（含 atx-agent 首装）、截图、`rel()` 参考坐标换算、`d.touch` 持续按压；**控制方案**（`control.method`，设置页下拉）：`injectInputEvent`（默认，`d.touch.down/up`，**不用 `d.click`**——UiDevice.click 走 JSON-RPC 模拟器上偶发失效；down/up 间保持 `CLICK_PRESS_SECONDS`=0.05s）/ `minitouch`（openstf minitouch，`MiniTouchSession`：二进制 `resources/minitouch/minitouch-<abi>` 推送设备 → `adb forward localabstract:minitouch` → socket 直发；forward 学习 ALAS 先 `forward --list` 复用、没有才在 20000-21000 随机高端口新建（低端口 Windows bind 10013）；坐标按握手 `^ max_contacts max_x max_y` 等比换算，不硬编码屏幕分辨率；minitouch 单连接限制，连接超时=被 atx-agent /minitouch 等占用），`click/touch_down/move/up` 按方案分派，minitouch 会话懒加载；**自动回退**：minitouch 因非 root/SELinux 权限拒绝打不开 `/dev/input/event*`（`_start_server` 读启动日志分类，抛 `MinitouchUnavailableError`）时自动回退 `injectInputEvent` 并把 `control.method` 写回 config.yaml（下次启动/热加载也走默认方案）；minitouch 自身错误（不可用/被占用/会话断开）不算 u2 连接故障，`_is_conn_error` 不触发 u2 重连自愈；**设备掉线重连**：`_reconnect` 先 `_wait_device_online`——远程模拟器（host:port）adb 抖动时先 `adb connect` + 轮询等 `DEVICE_ONLINE_WAIT`=45s 回线再重连 u2，避免"device not found"被误判成需要整机重启（USB 真机掉线不等待直接抛） |
| `src/locators.py` | UI 定位注册表 `LOCATORS`：名字 → u2 选择器 / OCR 候选文案 / rel 兜底坐标；`see()` / `see_all()` |
| `src/adb/device.py` | adb 封装：设备在线管理（start-server）、屏幕尺寸读取（scrcpy 嵌入比例用）、`reboot_and_wait()` / `launch_app()`（异常恢复用） |
| `src/ocr.py` / `src/coins.py` | RapidOCR 封装；主页金币 = 顶部状态栏最右侧数值（全屏 OCR） |
| `src/progress.py` | `log()`（控制台+文件+监听器）、每日次数持久化（含 history，跨天归档）、`count_cross` 交叉计数；进度文件固定 `runs/*.json` 单文件（曾按账号重定向到 `runs/accounts/<账号>/`，账号名靠状态面板 OCR 识别不稳定、数据被拆散，已取消多账号区分） |
| `src/opener.py` | 模拟器模式集成：自实现设备/Root/frida-server/启动 QQ/注入全流程（frida Python API），hook JS 从 `assets/qqpet-module-opener/open_qqpet_module.js` 读取（只保留上游这一个 JS，手动更新）；Frida 17 起 Java 桥不再内置，注入前用 frida-tools 的 `frida-java-bridge`（`frida_tools/bridges/java.js`，打包时随包）包一层暴露全局 `Java` 再拼 hook；打开宠物主页后**保持注入不解除**（`_KEEPALIVE` 持有引用防 GC，好友访问/踩踩/PK 的 doAction 接管持续生效），注入前 `_wait_qq_settle` 等 QQ 启动稳定；`_start_qq` 冷启动会重试 `am start`（`START_QQ_ATTEMPTS`=3），轮询 `pidof` 前先确认设备在线，adb 抖动不误判成"QQ 没启动"；注入失败（script is destroyed/会话断开/SDK 超时）会强停 QQ 重试 `OPEN_PET_ATTEMPTS`=3 次；frida 设备发现加固（`_frida_device`）：项目 adb 目录前置 PATH（frida 枚举 adb 设备用 PATH 里的 adb）+ 每次重试前 `get-state`/重连确认设备在线 + `get_device` 重试 2 次 + 兜底 `adb forward tcp:27042` + frida remote device（模拟器刚开机 frida adb 枚举不稳时） |
| `src/status_cache.py` | 宠物状态缓存（`runs/status_cache.json`，单 default 条目——曾按账号名称组织兼容多账号，账号 OCR 误识别导致状态条多行，已取消）：体力/清洁/心情（care 状态面板 OCR 后）、金币（主页 OCR 后）、香皂/饼干（喂食/洗澡结束时 OCR 控件附近小图；库存角标无文字，取离 `feed_10`/`shower_10` 控件最近的数字）；一键护理后清空体力/清洁/心情/饼干/香皂；GUI 日志页顶部状态条每秒读一次 |
| `src/queue_status.py` | 任务队列状态缓存（`runs/queue_status.json`）：TaskQueueRunner 每轮调度后写当前任务/下一任务（含等待点时间，HH:MM:SS + next_ts 时间戳，GUI 显示"xx秒后"倒计时）/待执行数量（在等退避/每日窗口/pending 收尾时间，主任务组 pending 也算一项）/等待中数量（现在就可执行、等调度器轮到），执行中任务在 `_execute` 里先写一次；GUI 状态条加一行每秒读一次（调度器未运行时不读，显示"调度器未运行"）；legacy 引擎不写 |
| `src/config.py` | dataclass 配置 + 路径规划：`APP_ROOT`（可写）/ `RESOURCE_ROOT`（包内资源），`resource_path()` APP_ROOT 优先 |
| `src/settings.py` | ruamel 往返读写 config.yaml（保留注释），GUI 设置页用 |
| `src/notify.py` | 失败告警通知：Windows Toast（winotify）+ OnePush 多渠道推送（Bark/PushPlus/Server酱/SMTP/自定义 webhook 等），发送失败只记日志 |
| `tools/dump_hierarchy.py` | 抓当前屏幕控件树 XML 存到 `xml/page.xml`（校准 locators 的 xpath/content-desc 用；`xml/` 已 git 排除） |
| `tools/fetch_scrcpy.py` | 从官方 GitHub Release 下载解压 scrcpy（win64）到 `resources/scrcpy-win64/`（不入库）；`--version` 指定版本、`--force` 强制覆盖，build.py / CI 打包前自动调用 |
| `tools/fetch_frida_server.py` | 下载 frida-server 离线包到 `resources/frida-server/`（不入库）；`--version`/`--arch`（可多个）/`--force`，GitHub 直连失败自动试镜像；源码运行 `src/opener.py` 缺失时自动调用，build.py --emulator 打包前也会调用 |
| `tools/fetch_minitouch.py` | 下载 minitouch 预编译二进制到 `resources/minitouch/minitouch-<abi>`（不入库，jsDelivr/unpkg/GitHub 多源）；`--arch`（可多个）/`--force`；源码运行 `src/u2dev.py` 控制方案选 minitouch 且缺失时自动调用，build.py 打包前也会调用 |
| `assets/qqpet-module-opener/` | hook JS（取自上游，QQ 更新后手动同步，入库） |
| `resources/` | 第三方二进制/离线包（不入库）：`scrcpy-win64/`（tools/fetch_scrcpy.py 拉取）、`frida-server/`（离线 xz，build.py --emulator 打包时带上）、`minitouch/`（minitouch 二进制，tools/fetch_minitouch.py 拉取，普通版/模拟器版都带上） |
| `tools/test_locator.py` | 测试 locator 的 xpath 在当前页面的命中稳定性（连设备连续多轮 dump，统计 live/snapshot 两种调用方式的命中率与 bounds 漂移，定位深层 xpath 时有时无/位置漂移问题） |
| `tools/capture_visit_jump.py` | 抓取 QQ 宠物"访问好友"跳转参数（doJumpAction URL + doAction attrs），真机/模拟器对比、QQ 更新后排查用；`-s` 设备、`-c` 自动点 好友->访问 |

## 关键约定（改动时必须遵守）

- **定位方式**：新 UI 元素登记到 `src/locators.py` 的 `LOCATORS`（沿用 `xxx_in` 进行中标志、
  `xxx_end` 结束标志、`quit`、`back`、`main_sign` 命名）。优先 xpath / u2 选择器
  （`main_sign` 是 xpath `//*[@content-desc="金币胶囊"]` 检测主页面——只有自己主页面
  有该元素，好友宠物页没有；care 解析面板名称时另有"加好友"守卫，防止把好友昵称
  当自己宠物的名称写进状态缓存）；
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
  起点取 `shower_10` 控件中心；搓洗中清洁连续 `SCRUB_STALL_REPRESS` 回合不提升判定
  按压失效（模拟器 minitouch 会话静默中断，touch_move 全丢），自动抬手重按肥皂自愈；
  喂食/洗澡达到尝试上限仍不达阈值时**跳过本次护理**（不抛异常——游戏有概率显示 bug，
  实际已达标但界面/OCR 没刷新，抛异常会误触发重启恢复/告警退出）。
- **一轮语义**：场景的 `run(max_times, max_rounds)` 中一轮 = 一节课 / 一次打工 / 一次冒险，
  结束后回主页面；执行器以 `max_rounds=1` 调用，每轮后重新判断金币/学习工作时长。
  学习场景的毕业处理（"去找同学玩"面板）不算一轮：关闭后立即重新进学校选下一阶段
  课程（不等 success_interval），连续毕业由 `goto_school` 的 `_graduated_once` 抛异常防循环。
- **出门处理**：`goto_*` 出门后必须调 `wait_busy_end()` 检测四种进行中状态
  （school/work/adventure/employed）；等完的活动计入对应进度后**本轮直接结束**，
  由执行器重新判断限制条件，不得继续原定任务。出门后也可能直接出现**结算页**
  （上次活动已结束未收尾，如调度器重启丢失 pending 后）：`_detect_settlement()`
  按 OCR 文案区分——"教师评语"=学习、"打工总结"=打工（文案含配置的雇佣好友名称
  时同时计一次雇佣好友）、都没有但有"分享"按钮=冒险，点 quit 收尾（学习/打工
  顺带尝试鼓励，结算页实测没有鼓励按钮，快速 3 轮不中即放弃）后返回对应类型，
  计数语同等完活动。
- **计数时机**：检测到 `xxx_end` 点完 `quit` 就计数（`save_progress`），再回主页面；
  被雇佣在召回点 quit 时由基类 `count_cross('employed')` 计数，场景 `run()` 不得重复计。
- **主页面点 back 会退出游戏**：`ensure_main_page` 必须保留宽限——连续
  `schedule.main_page_checks` 次（默认 1，即识别不到立即点 back）识别不到
  `main_sign`（金币胶囊）才允许点 back，总尝试上限随检测次数放大
  （`MAIN_PAGE_ATTEMPTS * checks`）。
- **OCR 置信度**：命中下限 `src/locators.py` 的 `OCR_MIN_SCORE`（默认 0.5）。
- **异常分级重试**：场景抛异常 → 先回主页面重进场景重试一次（页面错乱多半能
  自愈，不必重启）→ 仍失败才走 `Runner.recover()`（`src/recover.py`：adb reboot →
  启动 QQ → 等并点 `Q宠-*` 入口回宠物页）→ 最后再试一次；连续 `RECOVERY_LIMIT`
  次恢复仍失败才放弃恢复（距上次恢复超过 `RECOVERY_RESET_AFTER` 秒计数重置，
  只拦短时间连续恢复的死循环，支线长期失败不永久锁死恢复能力）。
  恢复成功后必须把新 U2Device 刷新到各场景的 `dev`。
  多次重试均失败按任务类型分流：主任务（学习/打工）发告警通知（`src/notify.py`：
  Windows Toast + OnePush，附当前手机截图存 `runs/alert_*.png`）并退出调度器
  （SystemExit）；支线任务（冒险/踩踩/PK/好友护理/好友雇佣）不退出——抛 `ScenarioFailed`，
  由调度循环重新排期延后 `SIDE_TASK_RETRY_DELAY` 秒重试（`retry_after`，
  参考 qq-farm-copilot 的失败间隔队列机制），先执行其他任务。
- **配置改动**：新配置项加到 `config.yaml` + `src/config.py` 的 dataclass +
  `main.py` 的 `SETTING_FIELDS`（设置页表单，全局设置）或 `TASK_SETTING_FIELDS`
  （任务页表单，场景任务相关）+ `src/settings.py` 的 `DEFAULTS`/`validate_field` 四处。
- **配置热加载**：新增配置项除了上面四处，还必须同步到 `scenarios/runner.py` 的
  `Runner.reload_config()`（两种引擎每轮调度前都会调用，GUI 设置页保存后下一轮生效）
  ——否则运行时改了不生效（历史教训：`work.duration` / `care.interval_seconds` /
  `schedule.encourage_times` / `schedule.main_page_checks` 曾漏同步）。规则：
  - 场景 `__init__` 里从 `self.cfg.xxx` 拷成 `self.xxx` 的**副本属性**必须逐字段同步
    （如 `work.duration`、`care.energy_threshold/clean_threshold/method`、
    `school.attribute/times_per_day`、`adventure.times_per_day/skip_bad_weather/batch`、
    `visit/pk.times_per_day`）；
  - 运行时直接读 `scen.cfg.xxx` 的字段也要同步对应场景的 cfg（如 `care_due()` 读
    `care.cfg.care.interval_seconds`、`hire_friend._select_job()` 读
    `hire_friend.cfg.work.duration`）；
  - **优先整体替换**，新增字段放进整体替换对象就不用逐个同步。当前已整体替换：
    各场景 `cfg.schedule`（`scen.cfg.schedule = sched`）、`cfg.employed`、
    `cfg.recover.*`、`cfg.emulator`，以及 `friend_care.cfg.friend_care`、
    `hire_friend.cfg.hire_friend`、`care.cfg.care`、`hire_friend.cfg.work`；
  - 非场景 cfg 的共享层配置单独同步（如 `control.method`：reload_config 里
    `scen.dev.control_method = cfg.control.method`，minitouch 会话懒加载，
    切换方案后下次点击自动按新方案走）；
  - 枚举/取值范围字段先校验、非法回退旧值并记日志（如 `school.attribute`、
    `work.duration`、`work.location`——地点下拉选项统一在 `src/settings.py` 的 `WORK_LOCATIONS`，
    main.py 下拉和 validate_field 共用，新增地图时只改这一处），不要直接赋值导致运行时 KeyError；
  - 例外（无需热加载）：`adb.*`（连接层，改完需重启 GUI）、`runner.engine`
    （切换调度引擎需重启调度器）。
  - **统一失败重试间隔**：`tasks.failure_interval`（设置页"任务失败重试间隔"，`SETTING_FIELDS` + `DEFAULTS`/`validate_field`）是各任务 `failure_interval` 的**唯一入口**——`load_config` 构造完各 `TaskItemConfig` 后用全局值覆盖，改 config.yaml 里各任务单独的 `failure_interval` 无效（config 文件里已删除这些行）。
- **GUI 线程纪律**：调度器是子进程（`scenarios/runner.py`，打包后为 `exe --runner`，
  两种入口都走 `run_scheduler()` 按 `runner.engine` 配置选引擎，不要写死 Runner），
  日志经 stdout → 队列 → QTimer 上屏；worker 线程不直接碰 Qt 控件。
- **任务队列调度**（默认引擎 `task_queue`）：`TaskQueueRunner` 每轮按 `tasks.order`
  顺序扫描，执行第一个"可执行"的任务（`_eligible`：enabled / enabled_time_range /
  trigger 窗口 / 退避间隔 → `_task_due`：任务自身配额与场景时间窗），跑完一个回顶部
  重扫；成功按 `success_interval`（interval 触发再叠加 `interval_seconds` 最小间隔；
  登记了 pending 的延时收尾任务除外——节奏由 `pending.until` 控制，`next_at`
  立即到期，结算完成的同一轮调度即可接力，避免短活动时凭空多等）、
  失败按统一 `tasks.failure_interval` 退避（**全局唯一入口**：设置页"任务失败重试间隔"，`load_config` 里覆盖所有任务的 `failure_interval`，不再单独配各任务）；daily 触发到点打开执行窗口（窗口内可反复执行直到
  任务返回 False，下一个时间点重开窗口并清除当天不可继续标记）；没有任务可执行时
  睡到最近等待点（上限 `QUEUE_POLL_INTERVAL` 轮询热加载配置）；冒险/学习/打工/雇佣好友
  互斥（不能同时做），作为**主任务组**统一调度（`_main_choice`）：组内优先级由
  `tasks.main_order` 配置（默认 `school>hire_friend>adventure>work`，> 分隔越靠前越优先，
  没列出的按默认顺序兜底；`MAIN_TASK_KEYS` 在 `src/config.py`），先过 `_eligible`
  （退避/时间窗未到点的任务跳过，否则幻影命中会把排它后面的主任务卡住——如雇佣好友
  CD 复测退避 60 秒但 hire_friend_due 的调度间隔只有几秒），再按配置顺序逐个判定——
  冒险（到点且当天次数未满）/ 学习（学习工作时长未达上限且金币 >= 阈值，或打工不可继续时回退）/
  雇佣好友（到点且次数未满）/ 打工（兜底，当天可继续就可执行），
  不受 `tasks.order` 里四者相对位置影响，金币/学习工作时长每轮循环只读一次（ctx 缓存）；
  主任务组**非阻塞等待（延时收尾）**：`defer_wait=True` 时场景出发后识别到进行中状态
  （"正在学习/打工/冒险"）就 OCR 剩余时间（"剩余00:02:50"，复用
  `parse_employed_remaining`）登记场景 `pending`（`defer_busy_end()`；**`until` 必须按 OCR 读取剩余时间的时刻算**——鼓励宠物点击耗时不能算进余量，否则上课/打工（有鼓励）估算偏晚鼓励耗时那么多秒、冒险（无鼓励）准，历史教训见 `_defer_busy` 的 `read_at`，OCR 失败兜底
  `DEFER_FALLBACK_SECONDS`=15 秒，避免一开始预估余量太大导致收尾偏晚），立即回主页面调度其他任务；**收尾优先**：`_run_first_due` 每轮先查主任务 pending——已到点先 `finish_pending()` 收尾，`PENDING_FINISH_HORIZON`=15s 内即将到点则不执行支线任务、睡到收尾点（否则护理/好友护理一轮 30~40s 会把收尾挤后几十秒，冒险短任务实测偏晚 ~30s）；到点后由 `_main_choice` 调 `finish_pending()`
  收尾：回主页面**出门后**才出现结算页（学习"教师评语"/打工"打工总结"（含雇佣
  好友名称），即 `end_name` 的"分享"按钮），见结算页点 quit（落在出门页面）、
  `on_finish()` 计数，还在进行中（计时误差）则 OCR 剩余时间重估 `until` 下轮再来；
  多轮检测既没结算页也没进行中状态则直接丢弃该 pending（不计数，不重估时间——
  识别不到剩余时间会按 60 秒兜底永远卡在重估循环；结算页若稍后真出现，后续主任务
  出门时会被 wait_busy_end 的结算检测兜底计数）；
  **鼓励宠物**：按钮只在学习/打工进行中页面常驻（结算页实测没有），非阻塞调度在
  登记 pending 离开进行中页面前就地按 `schedule.encourage_times` 快速点够
  （`_encourage_burst()`，0 为不鼓励），finish_pending 重估时还在进行中也会就地再点，
  结算页路径仅作兜底；收尾后再选下一个
  主任务，组内有 pending 未到收尾时间本轮不调度主任务；
  计数统一走 pending 的 `on_finish`（`count_cross` / `_count_hire_and_work`），
  场景本地计数在 defer 分支一律跳过，防重复计数；`pending['until']` 也算等待点；
  主任务组当天结束后只等支线任务的失败退避，没有则退出调度器。
- **踩踩/PK 调度**：执行器主循环在主页面按各自 `start_time` / 当天次数 / 失败延后期调度
  （`visit_due()` / `pk_due()`），跑对应场景 `run()` 完整流程；不做长等待插空
  （好友入口只在主页面，上课/打工/冒险等待页没有）。
- **护理调度**：每次护理检查（`check_and_care`，体力/清洁不足则喂食/洗澡）按
  `care.interval_seconds`（默认 60 秒，距上次检查起算，`care_due()`）节流，
  两种引擎共用（legacy 主循环每轮开头、队列引擎 care 任务）；场景上次检查时间
  记在 `CareScenario.last_care_at`。
- **体力/清洁不足弹窗**：点 `*_start`（上课/打工/冒险/PK，含雇佣好友复用的
  work_start）开始任务时，识别 `_start` 的同时同帧检测
  `//*[@content-desc="你的宠物体力不足，请回家补充体力"]` /
  `//*[@content-desc="你的宠物清洁值不足，请回家洗澡"]`（`src/locators.py` 的
  `pet_low_energy` / `pet_low_clean`）；命中则点 back 关弹窗 -> 回主页面 ->
  护理一次（`care_once`，同调度器护理检查）-> 抛 `StatBlocked`
  （`src/scenario.py`），调度器 `run_one` 立即重试当前任务一次（不算失败/不重启，
  主任务/支线共用），护理后重试仍被拦截则按常规失败分流（主任务告警退出、
  支线 `ScenarioFailed` 退避）。
- **好友护理调度**：`friend_care.enabled` 开启且配置了 `friend_name` 时，主循环按
  `friend_care.time_range`（HH:MM-HH:MM，支持跨零点）+ `friend_care.interval_seconds`
  调度间隔（`friend_care_due()`，距上次巡检完成时间起算）调度；每次调度只做一次
  护理巡检（进好友家按方式护理一次即回主页面，场景内不再等待/切换好友刷新状态），
  巡检完成无论是否执行护理动作都返回 True（返回 False 会被任务队列标记当天不可
  继续，导致间隔后不再复查）；护理方式与护理自己一致（ocr检测 护理到 90 / 一键护理），
  好友的体力/库存不写自己的状态缓存；好友家有概率卡顿（喂食/洗澡面板打不开、
  页面卡死），场景内失败后回主页面重新进指定好友家再试（`FRIEND_CARE_RETRIES` 次），
  仍失败才抛给调度器走恢复链路。
- **好友雇佣调度**：`hire_friend.enabled` 开启且配置了 `friend_name` 时，主循环在
  `hire_friend.time_range`（HH:MM-HH:MM，支持跨零点）时间段内按
  `hire_friend.interval_seconds`（默认 5 秒，距上次执行起算，`last_hire_at` 在执行处记录，
  `hire_friend_due()` 保持纯查询无副作用——`_main_choice` 每轮被主任务组内多个任务的
  扫描重复评估，判定里记时间会把雇佣卡死）/ 当天次数
  （`times_per_day`，进度存 `runs/hire_friend_progress.json`）/ 失败延后期调度
  （`hire_friend_due()`）；**雇佣前预检**：场景先出门检测宠物是否正在打工/学习/冒险/
  被雇佣中（基类 `detect_busy_remaining()`，OCR 剩余时间，识别不到兜底 60 秒），
  命中则抛 `TaskDeferred(until)`（`src/scenario.py`，与 `ScenarioFailed` 失败退避
  语义不同）延后到活动结束——队列引擎 catch 后设 `task.next_at = until`，legacy
  引擎记 `retry_after['雇佣好友']`，都不算失败，先调度其他任务；场景内进指定好友家，OCR `hire` 控件上的
  雇佣剩余 CD（如 28:05），有 CD 同样抛 `TaskDeferred` 延后 `HIRE_CD_POLL_SECONDS`
  （60 秒）复测——不原地等待（CD 可能提前结束）；没有 CD 才点 hire 进打工面板（面板加载固定等 3 秒，
  期间可能弹职业升级/获得新职业弹窗，先 `dismiss_career_popup()` 处理再检测，
  未进面板重试点击），按打工流程 select_place 确认/重选打工地点（已是配置地点直接用，
  不是则 back 重置重选）后按 `work.duration` 选工作选择框（10分钟/45分钟/2小时 ->
  select_box_1/2/3，打工与雇佣好友共用）、点 work_start 打工一轮；打工结束点 quit
  后才计数（雇佣好友 + 打工各计一次，不做打工流程里的雇佣部分）。
- **被雇佣检查调度**：`employed.enabled` 开启时，被雇佣时间段（`employed.time_range`，
  HH:MM-HH:MM 支持跨零点）内按 `employed.interval_seconds`（默认 60 秒）间隔出门
  检查是否被雇佣中（`employed_due()`，`scenarios/employed.py` 场景只做检测）；
  **非阻塞**：`employed_recall_ready` 单次判定到召回时机（`employed.action` 策略）
  才 `_recall_employed` 召回计数，没到就回主页面等间隔后再查，中间可跑其他任务
  （基类 `wait_employed_back` 仍是阻塞版，供主任务流程 `wait_busy_end` 用）；
  不在 `tasks.order` 里——队列引擎到点优先于队列任务先检查（`_run_employed_check`），
  巡检无论是否检测到都返回 True（同好友护理，False 会被标记当天不可继续）；
  - **学习/工作时长规则（替代旧“每日点数”）**：学习结算按持久化的学园字段累计
    （`school_progress.json` 的 `school`：初级10/中级20/高级30/进修45 分钟，学习开始时
    `set_current_school` 不一致才更新）；打工结算按持久化的 `work.duration` 累计
    （`work_progress.json` 的 `duration`：10分钟/45分钟/2小时）。累计时长（秒）存
    `study_secs`/`work_secs`，`load_durations()` 读取（GUI 日志页“今日”显示
    `已学习/工作/总时长（小时）0.0/0.0/0.0` 1 位小数）；`_duration_over` 判断
    `schedule.daily_hour_limit`（小时，0=不限）达上限后**今天不再学习只打工**；
    首次运行新版本时老进度只有次数（learned）没有时长字段，按旧版
    `schedule.school_factor`/`work_factor`（每节/每次的分钟数）自动迁移补上（只做一次）；
    旧版 `school_factor`/`work_factor`/`daily_point_limit` 仍留在 config.yaml 仅为
    迁移/兼容老配置，不在设置页显示、不参与调度。
  **被雇佣时间段内主任务（冒险/学习/打工/雇佣好友）不触发**（队列 `_main_choice`
  返回 None，pending 收尾不受影响；legacy 跳过冒险/雇佣好友/学习打工段睡到下次检查）。
- 控制台中文乱码是 Windows GBK 终端显示问题，日志文件（UTF-8）里是正常的，不要当 bug 修。
- **模拟器模式**（`--emulator`）：模拟器里 QQ 搜索卡片的宠物入口是空的（点不到 `Q宠-*`），
  由 `src/opener.py` 用 frida 注入已登录 QQ 进程直接打开宠物主页。
  上游 qqpet-module-opener 只保留 hook JS（`assets/qqpet-module-opener/open_qqpet_module.js`），
  QQ 版本兼容性修复都在这个 JS 里，QQ 更新打不开时从上游手动同步该文件后重新打包。
  frida-server 默认离线打包 x86_64（`resources/frida-server/frida-server-<版本>-android-x86_64.xz`，
  不入库）；frida 客户端版本必须与它一致（`requirements.txt` 的 `frida` 与 `build.py` 的 `FRIDA_VERSION`）。
  Frida 17 起 `Java` 桥不再内置在运行时里（脚本里没有全局 `Java`），注入前由
  `_wrap_java_bridge` 把 frida-tools 的 `frida-java-bridge` 暴露为全局 `Java` 再拼 hook；
  frida-tools 版本随 frida 一起锁定（`requirements.txt`），普通版打包时排除 frida_tools。
  模拟器模式下启动与 `recover()` 都走 opener 打开宠物主页（`src/recover.py` 的 `use_opener` 分支），
  不再依赖 `Q宠-*` 入口；GUI 手动重启恢复成功后再拉起调度器会传 `--skip-opener`
  跳过启动时的 opener 打开（宠物主页已由恢复流程打开，避免一次手动重启开两次宠物主页）；
  打包的模拟器版 exe（`build.py --emulator`）内置
  `emulator_mode.txt` 标记，启动默认开启模拟器模式（`src/config.py` 的 `is_emulator_build()`）。
  模拟器没有物理屏幕：模拟器模式下 scrcpy 不带 `--turn-screen-off`（`start_scrcpy`
  的 `emulator` 参数），关镜像时也不再拉无头关屏 scrcpy（`start_scrcpy_screen_off` 直接跳过）。
- 不再修改手机分辨率/密度（wm size/density）：定位分辨率无关，无此需求。

## 打包

`python build.py`（onefile）。`scrcpy-win64/` 不入库（二进制），打包前
`build.py` 自动调 `tools/fetch_scrcpy.py` 从官方 Release 拉取。路径约定：打包后
`APP_ROOT` = exe 所在目录（config.yaml 首启复制、runs/ 生成于此），
`RESOURCE_ROOT` = `sys._MEIPASS`。exe 旁 `runs/` 目录放同名资源可覆盖包内资源（如 `runs/resources/scrcpy-win64/`、`runs/resources/frida-server/`）。
