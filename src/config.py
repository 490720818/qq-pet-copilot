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
    """定位只读资源：APP_ROOT 下存在则用（用户自定义优先），否则用随包资源。"""
    rel = Path(rel)
    if rel.is_absolute():
        return rel
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
    auto_wifi_failover: bool = True
    wifi_port: int = 5555


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
    check_interval: int = 15


@dataclass
class AdventureConfig:
    # 每天冒险次数，0 为不冒险
    times_per_day: int = 1
    # 冒险调度时间（HH:MM），到达后优先冒险
    start_time: str = "08:00"
    # 开始冒险后检测冒险详情框：出现"天色不对"就点召回->确认召回，计入一次冒险
    skip_bad_weather: bool = False


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
class RecoverConfig:
    # 异常恢复方式：重启设备（adb reboot，彻底）/ 重启游戏（只强停并重开 QQ，快）
    method: str = "重启设备"


@dataclass
class EmployedConfig:
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
    school: SchoolConfig = field(default_factory=SchoolConfig)
    work: WorkConfig = field(default_factory=WorkConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    adventure: AdventureConfig = field(default_factory=AdventureConfig)
    care: CareConfig = field(default_factory=CareConfig)
    visit: VisitConfig = field(default_factory=VisitConfig)
    pk: PkConfig = field(default_factory=PkConfig)
    employed: EmployedConfig = field(default_factory=EmployedConfig)
    recover: RecoverConfig = field(default_factory=RecoverConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)


def find_adb(configured_path: str = "") -> str:
    """按 配置路径 -> PATH -> 常见目录 的顺序定位 adb。"""
    candidates = []
    if configured_path:
        # 相对路径优先取 APP_ROOT（用户自定义），其次随包资源
        p = Path(configured_path)
        candidates.append(str(p if p.is_absolute() else resource_path(p)))
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
    return Config(
        adb=AdbConfig(**raw.get("adb", {})),
        school=SchoolConfig(**raw.get("school", {})),
        work=WorkConfig(**raw.get("work", {})),
        schedule=ScheduleConfig(**raw.get("schedule", {})),
        adventure=AdventureConfig(**raw.get("adventure", {})),
        care=CareConfig(**raw.get("care", {})),
        visit=VisitConfig(**raw.get("visit", {})),
        pk=PkConfig(**raw.get("pk", {})),
        employed=EmployedConfig(**raw.get("employed", {})),
        recover=RecoverConfig(**raw.get("recover", {})),
        notify=NotifyConfig(**raw.get("notify", {})),
    )
