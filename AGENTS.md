# AGENTS.md

给 AI 代理的项目说明：结构、约定、常用命令。

## 项目概述

QQ 宠物自动化托管脚本。技术栈：Python 3 + adb/scrcpy（画面与输入）+
OpenCV `matchTemplate`（UI 识别）+ RapidOCR（文字/数字识别）+ PyQt6（GUI）。
平台：Windows（Git Bash 环境），目标设备：Android 手机（竖屏 720×1280 物理像素）。

## 运行与测试命令

```bash
PY=.venv/Scripts/python          # 项目虚拟环境，所有命令用它

$PY -m py_compile <files>        # 改完代码最基本的验证，必做
$PY main.py                      # GUI（scrcpy 嵌入 + 调度控制）
$PY scenarios/runner.py          # 控制台调度器
$PY scenarios/runner.py --test <target>   # 单模块测试：coins / school.X / work.X / adventure.X / care.X
$PY build.py                     # PyInstaller 打包（onefile），--onedir 目录模式
```

- 无设备测试：用 `DeviceScenario.__new__(DeviceScenario)` 跳过 adb 连接，
  桩掉 `see()` / `click()` 后调用被测方法（见历史上对 `wait_end` 的测试方式）。
- 无头 GUI 测试：`QT_QPA_PLATFORM=offscreen $PY -c ...`，
  并补丁 `MainWindow._start_all = lambda self: None` 避免拉起 scrcpy/调度器。
- **测试时不要污染真实进度文件**（`runs/*.json`）：涉及计数时把进度文件
  重定向到临时目录（monkeypatch `src.progress` 里的文件常量），或事后回退。

## 目录与模块职责

| 路径 | 职责 |
| --- | --- |
| `main.py` | PyQt6 GUI：scrcpy 窗口嵌入（SetParent）、日志页/设置页切换、调度器子进程控制 |
| `scenarios/runner.py` | 统一调度器：每轮 = 状态检查 → 冒险（定时优先）→ 点数规则 → 金币 OCR → 学习/打工一轮 |
| `scenarios/school.py` `work.py` `adventure.py` `care.py` | 各场景，均继承 `DeviceScenario` |
| `src/scenario.py` | 场景基类：截图/模板点击/回主页面/等待结束/被雇佣召回/四种进行中状态检测 |
| `src/adb/device.py` | adb 封装；`motion_event`/`motion_path` 用于"按住不松手"拖动（洗澡搓洗） |
| `src/vision.py` | `find`（单点）/ `find_all`（多点，NMS 去重，按从上到下排序） |
| `src/ocr.py` / `src/coins.py` | RapidOCR 封装；主页金币区域 OCR（小图需放大再识别） |
| `src/progress.py` | `log()`（控制台+文件+监听器）、每日次数持久化（含 history，跨天归档）、`count_cross` 交叉计数 |
| `src/config.py` | dataclass 配置 + 路径规划：`APP_ROOT`（可写）/ `RESOURCE_ROOT`（包内资源），`resource_path()` APP_ROOT 优先 |
| `src/settings.py` | ruamel 往返读写 config.yaml（保留注释），GUI 设置页用 |
| `templates/` | 模板图；命名约定：`xxx_in`（进行中标志）、`xxx_end`（结束标志）、`quit`、`back`、`main_sign` |

## 关键约定（改动时必须遵守）

- **坐标系**：全部是手机物理像素（720×1280 竖屏），与 `Device.tap`、模板裁剪一致。
- **一轮语义**：场景的 `run(max_times, max_rounds)` 中一轮 = 一节课 / 一次打工 / 一次冒险，
  结束后回主页面；执行器以 `max_rounds=1` 调用，每轮后重新判断金币/点数。
- **出门处理**：`goto_*` 出门后必须调 `wait_busy_end()` 检测四种进行中状态
  （school/work/adventure/employed）；等完的活动计入对应进度后**本轮直接结束**，
  由执行器重新判断限制条件，不得继续原定任务。
- **计数时机**：检测到 `xxx_end` 点完 `quit` 就计数（`save_progress`），再回主页面；
  被雇佣在召回点 quit 时由基类 `count_cross('employed')` 计数，场景 `run()` 不得重复计。
- **主页面点 back 会退出游戏**：`ensure_main_page` 必须保留 `BACK_GRACE_ATTEMPTS`
  宽限（连续多次识别不到 `main_sign` 才允许点 back）。
- **阈值**：模板匹配默认 0.8；`main_sign` 诊断用 0.5 低阈值打日志。
- **配置改动**：新配置项加到 `config.yaml` + `src/config.py` 的 dataclass +
  `main.py` 的 `SETTING_FIELDS`（设置页表单）三处。
- **GUI 线程纪律**：调度器是子进程（`scenarios/runner.py`，打包后为 `exe --runner`），
  日志经 stdout → 队列 → QTimer 上屏；worker 线程不直接碰 Qt 控件。
- 控制台中文乱码是 Windows GBK 终端显示问题，日志文件（UTF-8）里是正常的，不要当 bug 修。

## 打包

`python build.py`（onefile）。路径约定：打包后 `APP_ROOT` = exe 所在目录
（config.yaml 首启复制、runs/ 生成于此），`RESOURCE_ROOT` = `sys._MEIPASS`。
exe 旁放同名 `templates/`、`scrcpy-win64/` 可覆盖包内资源。
