# QQ 宠物自动化助手（qq-pet-copilot）

基于 uiautomator2 控件定位 + RapidOCR 文字识别的 QQ 宠物自动托管工具（分辨率无关）。
PyQt6 图形界面内嵌 scrcpy 实时画面，按金币和每日点数规则自动调度
**学习、打工、冒险**，并自动处理**被雇佣召回**和**体力/清洁照顾**。

## 功能

- **统一调度器**：每轮先在主页面 OCR 金币数量，金币充足优先学习，不足先打工赚够再学；
  学习×20 + 打工×45 超过每日点数上限后当天只打工，第二天自动清零。
- **学习场景**：出门 → 学校 → 拖动归位选课（力量/智力/魅力）→ 上课 → 下课计数。
- **打工场景**：出门 → 小镇 → OCR 识别打工地点进入 → 选最高收益工作 → 雇佣好友 → 开工。
- **冒险场景**：每天到达配置时间后优先冒险，满次数后等第二天。
- **被雇佣召回**：出门时检测到被雇佣中，每 15 秒检查召回标志，出现后提前召回并计数。
- **状态照顾**：任务前检查体力/清洁，不足自动喂食 / 洗澡（持续按压搓洗）。
- **每日计数持久化**：各场景次数按天记录在 `runs/*.json`（含历史记录），
  中途停止重跑接着计数，跨天自动归档清零。
- **GUI**：左侧 scrcpy 画面嵌入（关屏镜像），右侧日志 + 开始/停止按钮 + 设置页面
  （可视化修改 config.yaml，保存后即时生效）。

## 快速开始

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
```

1. 手机开 USB 调试并连接电脑（可用 `scrcpy-win64/adb.exe devices` 确认）。
   首次运行时 uiautomator2 会自动往手机安装 atx-agent，需在手机弹窗上允许安装。
2. 编辑 `config.yaml`（或在 GUI 设置页里改）。
3. 启动：

```bash
.venv/Scripts/python main.py            # GUI：画面 + 日志 + 控制按钮
.venv/Scripts/python scenarios/runner.py  # 控制台模式（无 GUI）
```

GUI 打开后自动嵌入手机画面，点**开始**启动调度器（子进程），点**停止**立即结束。

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
.venv/Scripts/python scenarios/runner.py --test work.select_place  # 只跑某个阶段方法
.venv/Scripts/python scenarios/runner.py --test care.read_status   # 只测体力/清洁识别
```

场景脚本也可单独跑：`python scenarios/school.py --times 1`（work / adventure / care 同理）。

## 打包 exe

```bash
.venv/Scripts/python build.py            # 单文件：dist/QQPetCopilot.exe
.venv/Scripts/python build.py --onedir   # 目录模式
```

打包后 `config.yaml` 首次运行自动复制到 exe 旁，`runs/` 也生成在 exe 旁。冷启动需解压资源，会慢几秒。

## 目录结构

```
main.py               # PyQt6 GUI 入口（scrcpy 嵌入 + 日志 + 开始/停止/设置）
build.py              # PyInstaller 打包脚本
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
  adb/device.py       # adb 封装：设备在线管理、屏幕属性读取
  scenario.py         # 场景基类：定位导航、回主页面、等待结束、被雇佣召回
  ocr.py              # RapidOCR 封装
  coins.py            # 主页金币 OCR
  progress.py         # 日志 + 每日次数持久化（含历史）
  settings.py         # config.yaml 读写（保留注释）
  config.py           # 配置加载与路径规划（兼容 PyInstaller）
```

## 定位方式

界面元素定位登记在 `src/locators.py` 的 `LOCATORS` 表：优先 u2 控件选择器
（原生弹窗等），游戏内 canvas 自绘按钮靠 OCR 文字，少数无文字纯图形元素
用 720x1280 参考坐标按当前分辨率等比换算。游戏更新后如识别失败，
用 `--test` 单测真机逐屏校准该表即可，无需重新截图做模板。

## 致谢

- [scrcpy](https://github.com/Genymobile/scrcpy)
  Android 画面镜像与控制工具，本项目的实时画面嵌入和 adb 能力实现。

## 免责声明

本项目仅供学习研究自动化与 OCR 识别技术使用。自动化操作可能违反游戏服务条款，
由此产生的一切后果由使用者自行承担。

## 许可证

本项目采用 GNU General Public License v3.0 (GPLv3)，详见根目录 [LICENSE](LICENSE)。
