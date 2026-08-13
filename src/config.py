"""配置加载：config.yaml，以及 adb 路径定位。

路径规划（兼顾 PyInstaller 打包）：
- APP_ROOT：可写数据根目录。开发时是项目根目录；打包后是 exe 所在目录。
  config.yaml、runs/（进度、日志）都放在这里。
- RESOURCE_ROOT：只读资源根目录。打包后是 PyInstaller 解压目录（sys._MEIPASS），
  scrcpy-win64/ 等随包资源从这里读；APP_ROOT 下存在同名资源时优先使用，
  方便用户不重新打包直接替换。
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _app_root() -> Path:
    if getattr(sys, "frozen", False):  # PyInstaller 打包后
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


APP_ROOT = _app_root()
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", APP_ROOT))
PROJECT_ROOT = APP_ROOT  # 兼容旧引用

CONFIG_FILE = APP_ROOT / "config.yaml"


def resource_path(rel: str | Path) -> Path:
    """定位资源。

    源码运行：项目根下（resources/ 等）存在则用，否则随包资源；
    打包运行：exe 旁的 runs/（可写数据目录）下放同名资源即可覆盖随包资源
    （如 runs/resources/scrcpy-win64/、runs/resources/frida-server/），无需重新打包。
    """
    rel = Path(rel)
    if rel.is_absolute():
        return rel
    if getattr(sys, "frozen", False):
        local = APP_ROOT / "runs" / rel
        return local if local.exists() else RESOURCE_ROOT / rel
    local = APP_ROOT / rel
    return local if local.exists() else RESOURCE_ROOT / rel


# Windows 上 adb 的常见安装位置
_COMMON_ADB_PATHS = [
    r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe",
    r"%USERPROFILE%\AppData\Local\Android\Sdk\platform-tools\adb.exe",
    r"C:\platform-tools\adb.exe",
    r"D:\platform-tools\adb.exe",
    r"C:\Android\platform-tools\adb.exe",
    r"C:\Program Files (x86)\Android\android-sdk\platform-tools\adb.exe",
]


@dataclass
class AdbConfig:
    path: str = ""
    device_serial: str = ""


@dataclass
class EmulatorConfig:
    # 模拟器类型：auto 自动探测，或 src/emulator.py 的 EMULATOR_TYPES 之一
    # （serial 匹配到多个实例时用于消歧，ALAS EmulatorInfo 同）
    type: str = "auto"
    # 实例名称（留空自动探测）
    name: str = ""
    # 模拟器安装路径（留空自动探测）
    path: str = ""


@dataclass
class SchoolConfig:
    # 属性点课程：力量 / 智力 / 魅力
    attribute: str = "力量"
    # 每天学习次数上限，0 为不限
    times_per_day: int = 0


@dataclass
class WorkConfig:
    # 打工地点（OCR 文字匹配），如：风铃旅社 / 星尘魔法塔 / 彩虹画室
    location: str = "风铃旅社"
    # 每天打工次数上限，0 为不限
    times_per_day: int = 0
    # 已不再使用：旧流程"下滑找雇佣按钮"已移除（当前页没有雇佣按钮时直接关闭面板开工），
    # 保留字段仅为兼容已有 config.yaml
    employ_scroll_limit: int = 5


@dataclass
class ScheduleConfig:
    # 金币阈值：金币 >= 该值优先学习，低于则先打工赚够再学习
    coin_threshold: int = 2000
    # 每日点数规则：学习次数 x school_factor + 打工次数 x work_factor
    # 超过 daily_point_limit 后今天不再学习，只打工
    school_factor: int = 20
    work_factor: int = 45
    daily_point_limit: int = 480
    # 进行中状态（上课/打工/冒险/被雇佣）的统一检查间隔（秒）
    check_interval: int = 8
    # 鼓励次数：在学习/打工进行中页面快速点击"鼓励宠物"按钮的次数，0 为不鼓励
    # （非阻塞调度在登记 pending 离开进行中页面前就地点击；结算页没有该按钮）
    encourage_times: int = 10


@dataclass
class AdventureConfig:
    # 每天冒险次数，0 为不冒险
    times_per_day: int = 1
    # 冒险调度时间（HH:MM），到达后优先冒险
    start_time: str = "08:00"
    # 开始冒险后检测冒险详情框：出现"天色不对"就点召回->确认召回，计入一次冒险
    skip_bad_weather: bool = False
    # 一轮连跑的冒险次数（跑满后回主页面）
    batch: int = 12


@dataclass
class CareConfig:
    # 护理方式：ocr检测（读宠物状态，低于阈值手动喂食/洗澡）/ 一键护理（直接点主页面的一键护理按钮）
    method: str = "一键护理"
    # 体力阈值：低于则喂食到达标
    energy_threshold: int = 60
    # 清洁阈值：低于则洗澡到达标
    clean_threshold: int = 60


@dataclass
class VisitConfig:
    # 每天踩踩次数，0 为不踩
    times_per_day: int = 10
    # 踩踩调度时间（HH:MM），到达后开始处理
    start_time: str = "00:01"


@dataclass
class PkConfig:
    # 每天 PK 次数，0 为不 PK；每个好友可 PK 3 次，打完自动切换下一个好友
    times_per_day: int = 15
    # PK 调度时间（HH:MM），到达后开始处理
    start_time: str = "00:01"


@dataclass
class FriendCareConfig:
    # 启用好友护理开关
    enabled: bool = False
    # 好友护理时间段（HH:MM-HH:MM）：到达开始时间后访问指定好友家护理，到结束时间退出场景
    time_range: str = "14:00-19:30"
    # 护理好友名称（好友列表 content-desc "好友 xxx" 里匹配）
    friend_name: str = ""
    # 护理好友方式：ocr检测（读好友状态面板，体力/清洁护理到 90）/ 一键护理（点好友页的一键护理按钮）
    method: str = "ocr检测"
    # 调度间隔（秒）：每次调度只做一次护理巡检，距上次巡检至少间隔这么久才再次调度
    interval_seconds: int = 60


@dataclass
class HireFriendConfig:
    # 雇佣好友开关
    enabled: bool = False
    # 雇佣好友时间段（HH:MM-HH:MM，支持跨零点）：时间段内才调度雇佣
    time_range: str = "19:31-23:59"
    # 调度间隔（秒）：距上次调度雇佣至少间隔这么久才再次调度（收尾后快速接下一轮）
    interval_seconds: int = 5
    # 雇佣好友名称（好友列表 content-desc "好友 xxx" 里匹配）
    friend_name: str = ""
    # 每天雇佣好友次数，0 为不雇佣
    times_per_day: int = 8


# 任务队列调度的任务键（tasks.order 里可配置的任务名）
TASK_KEYS = ('care', 'adventure', 'visit', 'pk', 'hire_friend', 'friend_care',
             'school', 'work')
# 主任务组：冒险/学习/打工/雇佣好友互斥（共用"出门-进行中"一条线，不能同时做），
# 由 TaskQueueRunner 按 tasks.main_order 统一调度
MAIN_TASK_KEYS = ('adventure', 'school', 'hire_friend', 'work')


@dataclass
class TaskItemConfig:
    # 单个任务的调度设置（config.yaml 的 tasks.<任务名> 段）
    enabled: bool = True
    # trigger: interval=按间隔秒数 / daily=按每日时间点列表（到点打开一个执行窗口）
    trigger: str = "interval"
    interval_seconds: int = 60
    daily_times: list = field(default_factory=list)  # HH:MM 列表
    # 允许执行的时间窗（HH:MM:SS-HH:MM:SS）
    enabled_time_range: str = "00:00:00-23:59:59"
    # 成功/失败后距下次执行的最小间隔（秒）
    success_interval: int = 60
    failure_interval: int = 1800


@dataclass
class TasksConfig:
    # 执行顺序（> 分隔，越靠前越优先）；不在 order 里的任务不调度
    order: str = "care>school>friend_care>hire_friend>adventure>visit>pk>work"
    # 主任务组（冒险/学习/打工/雇佣好友，互斥）组内优先级（> 分隔，越靠前越优先）；
    # 没列出的主任务按默认顺序兜底排最后
    main_order: str = "school>hire_friend>adventure>work"
    care: TaskItemConfig = field(default_factory=TaskItemConfig)
    adventure: TaskItemConfig = field(default_factory=TaskItemConfig)
    visit: TaskItemConfig = field(default_factory=TaskItemConfig)
    pk: TaskItemConfig = field(default_factory=TaskItemConfig)
    hire_friend: TaskItemConfig = field(default_factory=TaskItemConfig)
    friend_care: TaskItemConfig = field(default_factory=TaskItemConfig)
    school: TaskItemConfig = field(default_factory=TaskItemConfig)
    work: TaskItemConfig = field(default_factory=TaskItemConfig)


@dataclass
class RunnerConfig:
    # 调度引擎：task_queue（任务队列调度，默认）/ legacy（老主循环调度）
    engine: str = "task_queue"


@dataclass
class RecoverConfig:
    # 异常恢复方式：重启设备（adb reboot，彻底）/ 重启游戏（只强停并重开 QQ，快）
    method: str = "重启设备"
    # 模拟器重启命令（模拟器模式"重启设备"用）：MuMu 等模拟器不支持 adb reboot
    # （会把 adb 服务卡死），配了该命令则改为执行它重启模拟器整机；留空自动探测
    # MuMu 实例分步停/启（src/emulator.py），探测不到才回退 adb reboot。
    # 例（MuMu 12）："D:/Netease/MuMu Player 12/shell/MuMuManager.exe" control -v 0 restart
    emulator_restart_cmd: str = ""


@dataclass
class EmployedConfig:
    # 被雇佣开关：开启后按时间段+检查间隔出门检查是否被雇佣中
    enabled: bool = False
    # 被雇佣时间段（HH:MM-HH:MM，支持跨零点）：时间段内定时检查，
    # 且主任务（冒险/学习/打工/雇佣好友）不触发
    time_range: str = "19:31-23:59"
    # 被雇佣检查间隔（秒）：距上次检查至少间隔这么久才再次出门检查
    interval_seconds: int = 60
    # 被雇佣后处理：等到25/75（分成比例到 雇佣者<=25% 被雇佣者>=75% 才召回，收益最高）/
    # 等到25/75（小于45min）（同左，但面板剩余时间 >45 分钟立即召回，<=45 分钟继续等到25/75）/
    # 立刻召回（进被雇佣面板直接点"现在召回"）
    action: str = "等到25/75（小于45min）"


@dataclass
class NotifyConfig:
    # 场景多次重试仍失败时的告警：Windows 桌面 Toast 通知
    win_toast: bool = True
    # OnePush 推送配置（YAML，支持多行），如 {provider: bark, key: xxx}；留空不推送
    onepush_config: str = ""


@dataclass
class Config:
    adb: AdbConfig = field(default_factory=AdbConfig)
    emulator: EmulatorConfig = field(default_factory=EmulatorConfig)
    school: SchoolConfig = field(default_factory=SchoolConfig)
    work: WorkConfig = field(default_factory=WorkConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    adventure: AdventureConfig = field(default_factory=AdventureConfig)
    care: CareConfig = field(default_factory=CareConfig)
    visit: VisitConfig = field(default_factory=VisitConfig)
    pk: PkConfig = field(default_factory=PkConfig)
    friend_care: FriendCareConfig = field(default_factory=FriendCareConfig)
    hire_friend: HireFriendConfig = field(default_factory=HireFriendConfig)
    employed: EmployedConfig = field(default_factory=EmployedConfig)
    runner: RunnerConfig = field(default_factory=RunnerConfig)
    tasks: TasksConfig = field(default_factory=TasksConfig)
    recover: RecoverConfig = field(default_factory=RecoverConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)


def find_adb(configured_path: str = "") -> str:
    """按 配置路径 -> 随包 adb -> PATH -> 常见目录 的顺序定位 adb。"""
    candidates = []
    if configured_path:
        # 相对路径优先取 APP_ROOT（用户自定义），其次随包资源
        p = Path(configured_path)
        candidates.append(str(p if p.is_absolute() else resource_path(p)))
    # 随包 scrcpy 自带的 adb 兜底：配置路径失效时仍可用（如 exe 连同旧 config.yaml
    # 拷到别的电脑——配置里是指向原机器的路径/旧相对路径，本机靠 PATH 兜住没暴露）
    bundled = str(resource_path('resources/scrcpy-win64/adb.exe'))
    if bundled not in candidates:
        candidates.append(bundled)
    which = shutil.which("adb")
    if which:
        candidates.append(which)
    for raw in _COMMON_ADB_PATHS:
        candidates.append(os.path.expandvars(raw))

    for path in candidates:
        if path and Path(path).is_file():
            return str(Path(path))
    raise FileNotFoundError(
        "找不到 adb。请安装 platform-tools，并在 config.yaml 的 adb.path 中填写 adb.exe 完整路径。"
    )


def load_config(config_path: str | Path | None = None) -> Config:
    path = Path(config_path) if config_path else CONFIG_FILE
    if not path.is_file():
        # 打包后首次运行：把随包默认配置复制到 exe 旁，便于用户修改
        for name in ('config.yaml', 'config.example.yaml'):
            bundled = RESOURCE_ROOT / name
            if bundled.is_file() and bundled.resolve() != path.resolve():
                shutil.copy(bundled, path)
                break
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    raw_tasks = raw.get("tasks", {}) or {}
    tasks = TasksConfig()
    if raw_tasks.get("order"):
        tasks.order = str(raw_tasks["order"])
    if raw_tasks.get("main_order"):
        tasks.main_order = str(raw_tasks["main_order"])
    for key in TASK_KEYS:
        item = raw_tasks.get(key)
        if isinstance(item, dict):
            # 过滤掉未知键，防止 YAML 里多写的键让 dataclass 构造报错
            valid = {k: v for k, v in item.items() if k in TaskItemConfig.__dataclass_fields__}
            setattr(tasks, key, TaskItemConfig(**valid))
    return Config(
        adb=AdbConfig(**raw.get("adb", {})),
        emulator=EmulatorConfig(**{k: v for k, v in (raw.get("emulator", {}) or {}).items()
                                   if k in EmulatorConfig.__dataclass_fields__}),
        school=SchoolConfig(**raw.get("school", {})),
        work=WorkConfig(**raw.get("work", {})),
        schedule=ScheduleConfig(**raw.get("schedule", {})),
        adventure=AdventureConfig(**raw.get("adventure", {})),
        care=CareConfig(**raw.get("care", {})),
        visit=VisitConfig(**raw.get("visit", {})),
        pk=PkConfig(**raw.get("pk", {})),
        friend_care=FriendCareConfig(**raw.get("friend_care", {})),
        hire_friend=HireFriendConfig(
            **{k: v for k, v in (raw.get("hire_friend", {}) or {}).items()
               if k in HireFriendConfig.__dataclass_fields__}),
        employed=EmployedConfig(**raw.get("employed", {})),
        runner=RunnerConfig(**{k: v for k, v in (raw.get("runner", {}) or {}).items()
                               if k in RunnerConfig.__dataclass_fields__}),
        tasks=tasks,
        recover=RecoverConfig(**raw.get("recover", {})),
        notify=NotifyConfig(**raw.get("notify", {})),
    )


def is_emulator_build() -> bool:
    """是否为打包的模拟器版（build.py --emulator 内置 emulator_mode.txt 标记）。

    模拟器版 exe 启动后默认开启模拟器模式（用 qqpet-module-opener 打开宠物主页）；
    普通版/源码运行时为 False，可用 --emulator / --no-emulator 命令行参数覆盖。
    """
    if not getattr(sys, "frozen", False):
        return False
    return (RESOURCE_ROOT / "emulator_mode.txt").is_file()
