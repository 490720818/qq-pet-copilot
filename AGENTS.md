# AGENTS.md

给 AI 代理的项目说明：结构、约定、常用命令。

## 项目概述

QQ 宠物自动化托管脚本。技术栈：Python 3 + uiautomator2（画面、输入与控件定位）+
RapidOCR（文字/数字识别）+ PyQt6（GUI）。UI 定位分辨率无关：
优先 u2 控件选择器，游戏内 canvas 自绘按钮靠 OCR 文字（`src/locators.py` 注册表）。
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
$PY build.py --emulator          # 模拟器版（内置 frida 客户端；frida-server xz 不随包，兜底触发时按提示放到 exe 旁 runs/resources/frida-server/），--all 普通版+模拟器版一起打
# 模拟器模式：main.py / runner.py 加 --emulator [--emulator-device 127.0.0.1:7555]（Root 按需，见 src/opener.py 行）
```

- 无设备测试：用 `DeviceScenario.__new__(DeviceScenario)` 跳过 u2 设备连接，
  桩掉 `see()` / `click()` 后调用被测方法（见历史上对 `wait_end` 的测试方式）。
- 无头 GUI 测试：`QT_QPA_PLATFORM=offscreen $PY -c ...`，
  并补丁 `MainWindow._start_all = lambda self: None` 避免拉起 scrcpy/调度器。
  注意 offscreen 平台**字体库为空**（截图全是豆腐块）：验收截图需先
  `QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')`（和 segoeui.ttf）；
  另外 offscreen 下 Mica 无 DWM backdrop，深色主题截图前 `w.setMicaEffectEnabled(False)`
  走纯色回退底色。
- **测试时不要污染真实进度文件**（`runs/*.json`）：涉及计数时把进度文件
  重定向到临时目录（monkeypatch `src.progress` 里的文件常量），或事后回退。

## 目录与模块职责

| 路径 | 职责 |
| --- | --- |
| `main.py` | PyQt6 GUI，**界面基于 PyQt6-Fluent-Widgets**（requirements 已加 `PyQt6-Fluent-Widgets>=1.11,<2`，不要装 [full] 扩展会拉 scipy）：`MSFluentWindow` + 左侧导航栏（主页/调度/统计/任务/设置；设置页固定导航底部；主题按 `gui.theme` 配置（跟随系统/深色/浅色，`THEME_MAP`），启动时 `setTheme`、设置页改动即时生效）。**顶部全局工具栏**（`_install_toolbar` 把 stackedWidget 包进右侧容器：上工具栏下页面，切页不受影响）：开始 PrimaryPushButton、停止、画面镜像 SwitchButton（开关状态持久化 `gui.mirror`，`_toggle_scrcpy` 里写回，启动时 `load_config().gui.mirror` 恢复）、连接测试、手动重启 + 右侧运行时间 label。主页 = 左侧 scrcpy 画面卡片（SetParent 嵌入，**9:16 竖屏**：`ScrcpyContainer` 报竖屏 sizeHint/heightForWidth，`embed` 优先取 scrcpy 窗口客户区真实尺寸做嵌入比例（自适应任何设备/--max-size，用设备物理分辨率比例会留两侧黑边），`_aspect` 未知按 (9,16) 兜底 `_fit`，画面卡宽度随高度自适应收拢（`_fit_screen_card`：fixed width = 高度×画面比例，嵌入后/窗口缩放重算；**不用 QSplitter**——把手在深色主题下渲染成白色竖条；容器未嵌入时透明背景跟随主题，不写死黑色）+ 右侧卡片列（宠物状态横排一行/任务队列卡/今日统计（两行网格：学习(h)/工作(h) 时长拆分 + 各任务当日次数）/日志卡——三张卡内容都按列等宽均分（单元格 addWidget(cell, 1)，不要挤在左侧）——`log_view` 在主页吃剩余空间，状态/队列/今日卡垂直 sizePolicy Maximum 紧贴内容；**任务队列卡**（当前任务/下一任务/待执行/等待中）调度器运行时读 `runs/queue_status.json`、未运行按配置推算（`_predict_queue_summary` 复用调度页 `_predict_next`，按 `tasks.order` 顺序取第一个非"—"任务为下一任务）；分组卡片全用 `CompactCardWidget`（紧凑版 HeaderCardWidget：标题栏 48→34、内容边距 24→16/10/16/12，原版 chrome 占高 ~96px 一页放不了几组）；注意 `HeaderCardWidget.viewLayout` 是 QHBoxLayout，竖排内容要包一层 body widget）；调度页 = 每任务 开关（SwitchButton 开/关）/执行间隔（每日时间）/启用时段 可直接编辑（保存 config.yaml 热加载生效），下次执行 = 调度器运行时读 `runs/queue_status.json` 的 tasks 段、未运行时按配置推算的详细时间；任务/设置页 = `SETTING_FIELDS`/`TASK_SETTING_FIELDS` 数据驱动表单，按配置键第一段分组进 CompactCardWidget（分组标题映射 `SETTING_GROUP_TITLES`，任务页 = 任务队列顺序 + 场景任务设置，设置页 = 连接/调度引擎/全局规则/告警 + 关于与更新卡片），**分组卡片两列排布**（`TwoColumnCardsPanel`：QHBoxLayout 两个竖列、列尾 stretch 顶格，`_build_settings_form` 填完字段后 `finalize()` 按 sizeHint 高度把卡片平衡进较矮列，等宽；别用 FlowLayout——行高=该行最高卡片会在同列卡片间留白）；切页加载配置按页面 objectName 判定（`_on_tab_changed`，不依赖页序）；表单控件全用 fluent 类（SwitchButton 信号是 `checkedChanged` 不是 stateChanged；devices 下拉用 `_NoInsertEditableComboBox`——EditableComboBox 回车默认把输入追加进下拉，已改写拦截；**fluent ComboBox.addItem 签名是 (text, icon=None, userData=None)，userData 必须关键字传**（位置传参被当 icon、data 全 None，设备序列号下拉曾因此选啥都存成空）；HyperlinkLabel 的 (url, text) 重载要求 url 传 QUrl，传 str 会被当成 (text, parent) 重载（显示原始 URL、点击无效）；非 editable 的 fluent ComboBox **不是** QComboBox 子类但同名 API 基本兼容）、调度器子进程控制、scrcpy 看门狗（设备重启后自动重拉重嵌入；进程活着但没嵌上——多开同时拉起窗口创建慢、嵌入轮询已超时——看门狗补挂嵌入轮询，窗口出现即自动嵌入）、"手动重启"按钮
（按 `recover.method` 执行一次异常恢复 `reenter_pet`，调度器在跑先停，恢复期间开始/停止按钮禁用，恢复完成自动启动调度器）；设置页"检查更新"按钮 + 启动自动检查一次/每 6 小时一次（`src/update_checker.py`，
有更新时设置页显示 Release 链接并打日志）；标题栏带版本号（`src/version.py` 的 `APP_VERSION`）；**scrcpy 必须带 `--port=按序列号分配的固定端口`（`_scrcpy_port`，含无头关屏 scrcpy）**：默认范围 27183:27199 在 Windows 下多个 scrcpy 能同时绑定 27183（SO_REUSEADDR 语义），各设备 adb reverse 回连被投递到错误的 scrcpy 进程——双开同时开镜像画面串台/两窗口同一画面/Server connection failed |
| `src/stats_chart.py` | 统计页：各任务近 N 天次数的平滑折线图（QPainter 自绘 + Catmull-Rom 平滑，数据来自 `runs/*_progress.json` 的 history）；坐标轴文字/网格颜色跟随 Fluent 明暗主题（`_text_color()`/`_grid_color()` 读 `isDarkTheme()`，自绘不吃样式表） |
| `scenarios/runner.py` | 统一调度器，两种引擎（`runner.engine`）：`task_queue`（默认，`TaskQueueRunner`：执行顺序由 `tasks.order` 配置，> 分隔越靠前越优先，不在 order 里不调度；每任务独立 enabled / trigger（interval 间隔 / daily 每日时间点窗口）/ enabled_time_range / success_interval / failure_interval，见 `tasks` 段）/ `legacy`（`Runner.run` 老主循环，顺序写死：护理 → 冒险 → 踩踩 → PK → 好友雇佣 → 好友护理 → 学习/打工）。共通：场景异常分级重试（回主页面重进 → `recover()` 重启恢复）；都失败时主任务（学习/打工）发告警通知（`src/notify.py`）并退出，支线任务延后重试（legacy 用 `SIDE_TASK_RETRY_DELAY`，队列用各任务 `failure_interval`） |
| `scenarios/school.py` `work.py` `adventure.py` `care.py` `visit.py` `pk.py` `friend_care.py` `hire_friend.py` `employed.py` | 各场景，均继承 `DeviceScenario`（`pk.py`/`friend_care.py` 继承 `visit.py` 复用好友导航；`hire_friend.py` 继承 `friend_care.py` 复用指定好友导航；`employed.py` 只做被雇佣检测，召回复用基类） |
| `src/scenario.py` | 场景基类：截图/u2+OCR 定位点击/回主页面/等待结束（阻塞 `wait_end` / 非阻塞延时收尾 `defer_busy_end`+`finish_pending`，OCR 剩余时间登记 `pending`）/被雇佣召回/四种进行中状态检测 |
| `src/recover.py` | 异常恢复链路：adb reboot → 等开机 → 启动 QQ → **先试官方 scheme 直开宠物主页**（`mqqapi://qpet/open`，JumpActivity 零点击零权限，`SCHEME_TRY_ROUNDS`=2 轮，平板身份 `ro.build.characteristics` 含 tablet 会被告门禁拦、跳过直开）→ 失败回退点 `Q宠-*` 入口（descriptionStartsWith 前缀匹配，后缀数字不固定；入口紧凑双击用 minitouch 两连击——`d.click` JSON-RPC 往返慢，0.3s 间隔会被识别成两次单击进单击页，点不进主页 back 退回重试）回宠物页，返回新 U2Device；模拟器模式"重启设备"改为重启模拟器整机（MuMu 不支持 adb reboot，会把 adb 服务卡死；**opener 失败但设备在线时先强停 QQ 重开一次**——冷启动 QQ 首启失败比整机重启便宜得多），优先级：配置的 `recover.emulator_restart_cmd` > 留空自动探测模拟器实例分步停/启（`src/emulator.py`，serial 匹配多个实例时依次用 `emulator.type/name/path`、端口实际监听进程命令行消歧）> 回退 adb reboot，随后 connect → 等开机（`_adb_back_online` **不默认 kill-server**——kill-server 会把所有设备（其它模拟器实例/USB 真机）踢下线且 host:port 不会自动恢复，只在 connect **连续 `CONNECT_TIMEOUT_KILL_AFTER`=3 次**超时（服务疑似卡死，单次/偶发超时只是开机慢）才 kill-server 兜底并恢复之前在线过的所有远程设备）；`launch_emulator_if_offline(adb, emulator_cfg)` 启动时目标设备不在线则自动探测并**启动**所属实例（只启动不重启，等开机完成；探测不到返回 False 由调用方按原逻辑报错）——模拟器模式下 GUI `_start_all`（后台线程，设备上线后 scrcpy 看门狗自动重拉）与 `Runner.__init__`（U2Device 连接前）都会调用 |
| `src/emulator.py` | 多模拟器实例自动探测（参考 ALAS module/device/platform）：MuMu 12/6/X、雷电 3/4/9、夜神、蓝叠 4/5、逍遥；exe→类型靠 `path_to_type`（exe 名+上级目录名），安装目录来源 = 卸载项注册表（子键名精确匹配）+ MuiCache/UserAssist（ROT13）+ 雷电 InstallDir 注册表；serial 算法 = vbox/nemu/memu 的 hostport→5555 转发正则（MuMu12 兜底 16384+32*index、雷电 5555+2*index、蓝叠4 固定 5555、蓝叠5 读 bluestacks.conf）；`scan_serials()`（GUI 设备下拉合并）、`find_instance(serial, type/name/path 消歧)`、`get_serial_pair()`（127.0.0.1:5555+X ↔ emulator-5554+X）、`restart_instance()` 按类型分步停/启 = stop 半边 + `launch_instance()`（仅启动半边，设备未运行时拉起用——模拟器模式启动链路 `launch_emulator_if_offline`，见 recover 行）（有控制台 exe 走控制台：MuMuManager/ldconsole/bsconsole/memuc，蓝叠5/MuMu6/X 杀进程再用主 exe 拉起）；serial 命中多个实例且配置消歧不够时，按端口实际监听进程命令行反查（`_match_by_listener`：MuMu12 每个实例的 .nemu 都转发 7555，MuMuVMMHeadless.exe 的 --comment 即实例名，0.0.0.0 通配与 127.0.0.1 精确绑定并存时精确绑定优先） |
| `src/u2dev.py` | uiautomator2 封装：连接（含 atx-agent 首装）、截图、`d.touch` 持续按压；**控制方案**（`control.method`，设置页下拉）：`injectInputEvent`（默认，`d.touch.down/up`，**不用 `d.click`**——UiDevice.click 走 JSON-RPC 模拟器上偶发失效；down/up 间保持 `CLICK_PRESS_SECONDS`=0.05s）/ `minitouch`（openstf minitouch，`MiniTouchSession`：二进制 `resources/minitouch/minitouch-<abi>` 推送设备 → `adb forward localabstract:minitouch` → socket 直发；forward 学习 ALAS 先 `forward --list` 复用、没有才在 20000-21000 随机高端口新建（低端口 Windows bind 10013）；坐标按握手 `^ max_contacts max_x max_y` 等比换算，不硬编码屏幕分辨率；minitouch 单连接限制，连接超时=被 atx-agent /minitouch 等占用），`click/touch_down/move/up` 按方案分派，minitouch 会话懒加载；**自动回退**：minitouch 因非 root/SELinux 权限拒绝打不开 `/dev/input/event*`（`_start_server` 读启动日志分类，抛 `MinitouchUnavailableError`）时自动回退 `injectInputEvent` 并把 `control.method` 写回 config.yaml（下次启动/热加载也走默认方案）；minitouch 自身错误（不可用/被占用/会话断开）不算 u2 连接故障，`_is_conn_error` 不触发 u2 重连自愈；**设备掉线重连**：`_reconnect` 先 `_wait_device_online`——远程模拟器（host:port）adb 抖动时先 `adb connect` + 轮询等 `DEVICE_ONLINE_WAIT`=45s 回线再重连 u2，避免"device not found"被误判成需要整机重启（USB 真机掉线不等待直接抛） |
| `src/locators.py` | UI 定位注册表 `LOCATORS`：名字 → u2 选择器 / OCR 候选文案；`see()` / `see_all()` |
| `src/adb/device.py` | adb 封装：设备在线管理（start-server）、屏幕尺寸读取（scrcpy 嵌入比例用）、`reboot_and_wait()` / `launch_app()`（异常恢复用） |
| `src/ocr.py` / `src/coins.py` | RapidOCR 封装；主页金币 = 顶部状态栏最右侧数值（全屏 OCR） |
| `src/version.py` / `src/update_checker.py` | 版本常量（`APP_VERSION`/`APP_GITHUB_REPO`，release 工作流打包前由 `tools/write_version.py --tag <tag>` 写入）；GitHub `releases/latest` 更新检查（纯 stdlib urllib，版本号分段比较） |
| `src/progress_store.py` | 进度文件统一管理（`runs/*_progress.json`）：跨天规整（旧日期次数归档进 history、清掉旧日期的 `study_secs/work_secs`；`school/duration` 作为会话元数据跨天保留供跨零点结算累计）、原子写入（先写 `.tmp` 再 `os.replace`，进程被杀不留损坏文件）、损坏兜底（解析失败备份成 `*.corrupted.bak` 后按空档继续）；全部进度读写走这里，`src/progress.py` 只做兼容层 |
| `src/progress.py` | 进度持久化对外兼容入口（常量 + 日志 + 各场景函数签名，内部转发 `src/progress_store.py`）：`log()`（控制台+文件+监听器）、`load_progress/save_progress/increment_progress`（含 history 跨天归档）、`count_cross` 交叉计数、学习/工作时长累计与迁移；进度文件固定 `runs/*.json` 单文件（曾按账号重定向到 `runs/accounts/<账号>/`，账号名靠状态面板 OCR 识别不稳定、数据被拆散，已取消多账号区分） |
| `src/opener.py` | 模拟器模式集成（**零注入方案**：MuMu 机型伪装 / 设备门禁本地翻转 + 官方 scheme 跳转，旧版常驻 frida 注入被 QQ 风控"使用外挂插件"）：**Root 按需**——`_has_root()` 软检查（`su -c id`），有 Root 才做 `ensure_device_spoof()` 伪装重挂与 MMKV 补丁自检，无 Root 直接 scheme 直开（伪装/补丁都已持久化的设备日常零权限运行；scheme 失败且无 Root 时报错提示开一次 Root 打补丁，之后可永久关闭）；伪装改 MuMu app 级机型映射（`emulator.device_spoof` 开关控制，**默认关闭**——门禁补丁已翻转的设备不需要，设置页"MuMu 机型伪装"开关，opener 每次运行重新 load_config 天然热生效）（`/system/etc/mumu-configs/app-device-prop-*.config` 里 QQ 行从 `yyb.config` 原始模拟器 dump 换成真实手机 profile，root `mount --bind` 覆盖，重启模拟器失效、每次运行幂等重挂，非 MuMu 静默跳过），QQ 以真机身份运行、门禁原生通过、搜索卡片"宠物"入口可用；无伪装时模拟器被 QQ 判成 TABLET（`ro.build.characteristics` 含 tablet），门禁 `PetQQMC.e()` 读 UnitedConfig 107805 的 `enable_tablet`（默认 0）拦截官方跳转，此时 `ensure_gate_open()` 用 root 改本地 MMKV 缓存（`united_config_mmkv_<uin>` 追加 `enable_tablet:1` 记录 + 重算 CRC，`_patch_gate_mmkv` 有格式自检，不符就放弃不冒损坏风险）翻转门禁兜底；然后 `am start -a VIEW -d "mqqapi://qpet/open?..."`（JumpActivity，普通 shell 可发）由 QQ 官方路径打开宠物主页并初始化 SDK；三级调度：scheme 直开 → MMKV 补丁后 scheme → frida 一次性 SDK init + `am_start_pet_page` fragment 直开兜底（伪装名 `perf_daemon`、随机端口、几秒注入窗口，格式变化时启用）；`EMULATOR_MODE`/`GATE_OPEN` 标记由 `run_scheduler`/`--test`/`open_pet_page` 置位，visit 据此选游戏内导航或 am start 好友入口分支；好友入口 uin 由 `ensure_friend_entry()` 一次性注入捕获缓存 `runs/friend_entry.json`（仅兜底模式需要）；`_start_qq` 冷启动重试（`START_QQ_ATTEMPTS`=3）；QQ 服务端重发 107805 会覆盖补丁，每次启动自检重补 |
| `src/status_cache.py` | 宠物状态缓存（`runs/status_cache.json`，单 default 条目——曾按账号名称组织兼容多账号，账号 OCR 误识别导致状态条多行，已取消）：体力/清洁/心情（care 状态面板 OCR 后）、金币（主页 OCR 后）、香皂/饼干（喂食/洗澡结束时 OCR 控件附近小图；库存角标无文字，取离 `feed_10`/`shower_10` 控件最近的数字）；一键护理后清空体力/清洁/心情/饼干/香皂；GUI 日志页顶部状态条每秒读一次 |
| `src/queue_status.py` | 任务队列状态缓存（`runs/queue_status.json`）：TaskQueueRunner 每轮调度后写当前任务/下一任务（含等待点时间，HH:MM:SS + next_ts 时间戳，GUI 显示"xx秒后"倒计时）/待执行数量（在等退避/每日窗口/pending 收尾时间，主任务组 pending 也算一项）/等待中数量（现在就可执行、等调度器轮到），执行中任务在 `_execute` 里先写一次；GUI 状态条加一行每秒读一次（调度器未运行时不读，显示"调度器未运行"）；legacy 引擎不写 |
| `src/config.py` | dataclass 配置 + 路径规划：`APP_ROOT`（可写）/ `RESOURCE_ROOT`（包内资源），`resource_path()` APP_ROOT 优先 |
| `src/settings.py` | ruamel 往返读写 config.yaml（保留注释），GUI 设置页用 |
| `src/notify.py` | 失败告警通知：Windows Toast（winotify）+ OnePush 多渠道推送（Bark/PushPlus/Server酱/SMTP/自定义 webhook 等），发送失败只记日志 |
| `tools/dump_hierarchy.py` | 抓当前屏幕控件树 XML 存到 `xml/page.xml`（校准 locators 的 xpath/content-desc 用；`xml/` 已 git 排除） |
| `tools/fetch_scrcpy.py` | 从官方 GitHub Release 下载解压 scrcpy（win64）到 `resources/scrcpy-win64/`（不入库）；`--version` 指定版本、`--force` 强制覆盖，build.py / CI 打包前自动调用 |
| `tools/fetch_frida_server.py` | 下载 frida-server 离线包到 `resources/frida-server/`（不入库）；`--version`/`--arch`（可多个）/`--force`，GitHub 直连失败自动试镜像；源码运行 `src/opener.py` 缺失时自动调用（xz 不随 exe 打包，打包版兜底触发时按提示手动放置） |
| `tools/fetch_minitouch.py` | 下载 minitouch 预编译二进制到 `resources/minitouch/minitouch-<abi>`（不入库，jsDelivr/unpkg/GitHub 多源）；`--arch`（可多个）/`--force`；源码运行 `src/u2dev.py` 控制方案选 minitouch 且缺失时自动调用，build.py 打包前也会调用 |
| `resources/` | 第三方二进制/离线包（不入库）：`scrcpy-win64/`（tools/fetch_scrcpy.py 拉取）、`frida-server/`（离线 xz，不随 exe 打包，兜底触发时手动放置/源码运行自动下载）、`minitouch/`（minitouch 二进制，tools/fetch_minitouch.py 拉取，普通版/模拟器版都带上） |
| `tools/test_locator.py` | 测试 locator 的 xpath 在当前页面的命中稳定性（连设备连续多轮 dump，统计 live/snapshot 两种调用方式的命中率与 bounds 漂移，定位深层 xpath 时有时无/位置漂移问题） |
| `tools/capture_visit_jump.py` | 抓取 QQ 宠物"访问好友"跳转参数（doJumpAction URL + doAction attrs），真机/模拟器对比、QQ 更新后排查用；`-s` 设备、`-c` 自动点 好友->访问；frida-server 按 opener 的隐身方式自动部署（伪装名 + 随机端口，用完即杀） |

## 关键约定（改动时必须遵守）

- **定位方式**：新 UI 元素登记到 `src/locators.py` 的 `LOCATORS`（沿用 `xxx_in` 进行中标志、
  `xxx_end` 结束标志、`quit`、`back`、`main_sign` 命名）。优先 xpath / u2 选择器
  （`main_sign` 是 xpath `//*[@content-desc="金币胶囊"]` 检测主页面——只有自己主页面
  有该元素，好友宠物页没有；care 解析面板名称时另有"加好友"守卫，防止把好友昵称
  当自己宠物的名称写进状态缓存）；
  进行中状态文字（`xxx_in`）用整屏 OCR 关键词（状态区域 xpath  bounds 随页面
  结构漂移，裁剪 OCR 不可靠；同一张 screen 连续多次 `see()` 共享一次整屏 OCR，
  见 `_ocr_texts_cached`）；整屏 OCR 统一走 `src/ocr.py` 的 `ocr_fullscreen()`
  （先保持长宽比缩到接近 720×1280 再识别，坐标还原回原图）；游戏内按钮用整屏 OCR 文案（候选列表按优先序）。
  位置固定、只需点击的元素可加 `'cache': True`（如 `back`）：第一次命中后坐标
  记入 `_locate_cache`，之后 `see()` 直接返回缓存点不再识别。
  位置固定的裁剪区域（如宠物状态面板 `status_region`）登记 xpath + `'cache': True`
  后用 `see_bounds()` 取范围：第一次命中后 bounds 记入 `_bounds_cache`，之后直接复用。
- **洗澡搓洗**：搓洗点位按分辨率百分比（`care.py` 的 `SCRUB_TOP_PCT` / `SCRUB_BOTTOM_PCT`），
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
  **启动例外**（非模拟器模式）：`Runner.run()` 开头先做启动检查
  （`_ensure_pet_page_or_relaunch`）——识别不到 `main_sign` 说明多半根本不在游戏里，
  此时**不走 ensure_main_page 的 back 宽限**（乱按 back 无意义甚至误退别的 App），
  直接走"重启游戏"分支（`reenter_pet(adb, '重启游戏')`：强停 QQ → 启动 QQ → 点
  `Q宠-*` 入口，不受 `recover.method` 配置影响、不重启设备），成功后刷新各场景 `dev`；
  失败发告警退出调度器。
  **返回方式**（`schedule.back_method`，设置页下拉）：所有“回退”统一走基类
  `go_back()`——`系统返回`（默认）用 `dev.d.press('back')`；`返回图标` 定位
  `back` 按钮点击，找不到返回 False 由调用方决定（重试/放弃），避免误按系统返回退过头。
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
  - 例外（无需热加载）：`adb.*`（连接层，改完需重启 GUI）、`gui.*`（仅 GUI 用，如 theme 保存时即时 setTheme）、`runner.engine`
    （切换调度引擎需重启调度器）。
  - **统一失败重试间隔**：`tasks.failure_interval`（设置页"任务失败重试间隔"，`SETTING_FIELDS` + `DEFAULTS`/`validate_field`）是各任务 `failure_interval` 的**唯一入口**——`load_config` 构造完各 `TaskItemConfig` 后用全局值覆盖，改 config.yaml 里各任务单独的 `failure_interval` 无效（config 文件里已删除这些行）。
- **GUI 线程纪律**：调度器是子进程（`scenarios/runner.py`，打包后为 `exe --runner`，
  两种入口都走 `run_scheduler()` 按 `runner.engine` 配置选引擎，不要写死 Runner），
  日志经 stdout → 队列 → QTimer 上屏；worker 线程不直接碰 Qt 控件。
  界面是 PyQt6-Fluent-Widgets（`MSFluentWindow`）：新增页面用 `addSubInterface` 注册
  （页面必须 setObjectName），主题切换由 qfluentwidgets 自己处理，
  自绘/普通 Qt 控件要读 `isDarkTheme()` 或用 fluent 标签类。
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
- **体力/清洁不足提示条**：提示条（`enabled=false` 内联条，非模态弹窗）会**替换
  开始按钮**，因此检测要覆盖两类阶段：点 `*_start`（上课/打工/冒险/PK，含雇佣
  好友复用的 work_start）时识别 `_start` 的同时同帧检测，以及**等待 `*_start`
  出现的阶段**（`click_until_gone_or_see` 的 wait_name 以 `_start` 结尾时同样
  每轮检测——提示条顶掉开始按钮时可能还没点过 _start，如"前往冒险"阶段等
  adventure_start，只查 click_name 会漏判到导航超时）。检测目标：
  `//*[@content-desc="你的宠物体力不足，请回家补充体力"]` /
  `//*[@content-desc="你的宠物清洁值不足，请回家洗澡"]`（`src/locators.py` 的
  `pet_low_energy` / `pet_low_clean`）；命中则点 back 退出面板 -> 回主页面 ->
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
    **进度文件读写统一走 `src/progress_store.py`**（`src/progress.py` 只是兼容层）：
    跨天规整 + 原子写入 + 损坏兜底都在 store。跨天时旧日期当天次数归档进 history、
    清掉 `learned/study_secs/work_secs`；**`school/duration` 作为会话元数据跨天保留**
    （昨晚开始的打工/上课今天收尾要拿它累计今天的时长），由 `get_daily_field` 按
    `date==今天` 门控；`set_current_school`/`set_current_work_duration` 跨天**总是落盘**
    推进日期（即使值没变，否则同校/同时长第一天结算会拿不到元数据）。
    禁止整字典替换成 `{date}`（曾因此把 school_progress.json 的 history/learned 全丢）。
  **被雇佣时间段内主任务（冒险/学习/打工/雇佣好友）不触发**（队列 `_main_choice`
  返回 None，pending 收尾不受影响；legacy 跳过冒险/雇佣好友/学习打工段睡到下次检查）。
- 控制台中文乱码是 Windows GBK 终端显示问题，日志文件（UTF-8）里是正常的，不要当 bug 修。
- **模拟器模式**（`--emulator`）：模拟器里 QQ 搜索卡片的宠物入口默认是空的（点不到
  `Q宠-*`），由 `src/opener.py` 打开宠物主页。**当前方案零注入**（旧版全程常驻 frida
  注入被 QQ 风控"使用外挂插件"，详见 `src/opener.py` 模块 docstring），分级：
  0. **MuMu 机型伪装映射改写**（`ensure_device_spoof`，启动 QQ 前执行）：由配置项
     `emulator.device_spoof` 控制（设置页"MuMu 机型伪装"开关，**默认关闭**——门禁
     MMKV 补丁已翻转过的设备不需要伪装；opener 每次运行重新 `load_config`，保存即生效）：
     MuMu 的 `/system/etc/mumu-configs/app-device-prop-*.config`（按 Android 版本
     命名，多个匹配取实际含 QQ 行的）是"包名正则
     →机型 profile"映射，QQ（`com.tencent.mobileqq`）被故意喂 `yyb.config`（完整
     caas/Intel/AOSP/tablet 属性 dump），注入机制是 QQ 进程里的 MuMu hook lib
     （libnemuinitaidl.so/libjavahelper.so）；把 QQ 行改成目录里已有的真实手机
     profile（含 `ro.build.characteristics=default`，优先 `honor_magic4pro.config`），
     QQ 进程重启即按真机身份运行——门禁原生通过，搜索"QQ宠物"卡片出现"宠物"功能
     入口、双击即进宠物主页，与真机完全一致。/system 硬只读（USER 固件无
     disable-verity），只能 root `mount --bind` 覆盖（暂存
     `/data/local/tmp/qqpet_adp.config`），**重启模拟器后失效需重挂**（函数幂等，
     每次 opener 运行自动检查重挂）；非 MuMu 设备静默跳过；
  1. 无伪装时模拟器被 QQ 判成 TABLET（`ro.build.characteristics` 含 tablet），
     宠物设备门禁 `PetQQMC.e()` 读 UnitedConfig 107805 配置的 `enable_tablet`
     （默认 0）拦截一切官方跳转；opener 用 root 直接改 UnitedConfig 本地 MMKV 缓存
     （`files/mmkv/united_config_mmkv_<uin>`，append-only 追加一条 `enable_tablet:1`
     的 `107805_key_content` 记录 + 重算 CRC 元文件）本地翻转门禁；
  2. 然后官方 scheme `am start -a VIEW -d "mqqapi://qpet/open?version=1&src_type=app&source=1"`
     （JumpActivity，exported，普通 shell 可发）由 QQ 自己打开宠物主页并初始化宠物 SDK。
  调度三级：scheme 直开（伪装/补丁持久，二次运行零写盘）→ MMKV 补丁后 scheme →
  frida 一次性 SDK init + am start fragment 兜底（QQ 大改版 MMKV 格式变化时）。
  注意：QQ 服务端重新下发 107805 配置会覆盖补丁，opener 每次启动自检重补（幂等）；
  **Root 按需**（`_has_root()` 软检查）：伪装重挂、MMKV 补丁自检/重补、frida 兜底
  都只在有 Root 时执行；无 Root 直接 scheme 直开——伪装（bind-mount 每次开机由
  opener 重挂）与 MMKV 补丁（QQ 数据盘持久）都已在有 Root 时打过一次后，日常运行
  可永久关闭 Root（实测 MuMu/雷电9 关 Root 后 scheme 直开均正常）；scheme 失败且
  无 Root 才报错提示临时开一次 Root 补补丁。
  scheme 打开的宠物页宿主是 `AdelieFragmentActivity`（am start fragment 是
  `QPublicFragmentActivity`），渲染判定两种都认。
  好友访问（踩踩/PK/好友护理/好友雇佣）：门禁翻转后（`opener.GATE_OPEN=True`）
  游戏内 好友->访问 与真机一致直接可用；仅 frida 兜底模式（门禁仍关）才走
  `visit.py` 的 `_goto_first_friend_emulator`（am start 直开 + `runs/friend_entry.json`
  入口缓存）。
  frida-server xz 不随 exe 打包（省 ~32MB）：兜底触发时按提示把
  `frida-server-<版本>-android-<架构>.xz` 放到 exe 旁 `runs/resources/frida-server/`
  （`tools/fetch_frida_server.py` 下载，源码运行缺失时自动联网下载）；
  frida 客户端版本必须与它一致（`requirements.txt` 的 `frida` 锁定版本）。
  Frida 17 起 `Java` 桥不再内置在运行时里（脚本里没有全局 `Java`），注入前由
  `_wrap_java_bridge` 把 frida-tools 的 `frida-java-bridge` 暴露为全局 `Java` 再拼 init 脚本；
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
Release 工作流打包前会执行 `tools/write_version.py --tag <tag>` 把版本号写进 `src/version.py`
（exe 内"检查更新"按它比较版本）；本地手动打包发布前如需正确版本号，同样先跑这个命令。
