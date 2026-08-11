"""config.yaml 读写（ruamel 往返模式，保留注释和格式），供设置页面使用。"""
from __future__ import annotations

from datetime import datetime

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, LiteralScalarString

from .config import CONFIG_FILE

_yaml = YAML()  # 默认 round-trip，保留注释

# 各配置项默认值：设置页校验不通过时恢复
DEFAULTS = {
    'adb.path': 'resources/scrcpy-win64/adb.exe',
    'adb.device_serial': '',
    'school.attribute': '力量',
    'school.times_per_day': 0,
    'work.location': '风铃旅社',
    'work.times_per_day': 0,
    'work.employ_scroll_limit': 5,
    'schedule.coin_threshold': 2000,
    'schedule.school_factor': 20,
    'schedule.work_factor': 45,
    'schedule.daily_point_limit': 480,
    'schedule.check_interval': 8,
    'adventure.times_per_day': 1,
    'adventure.start_time': '08:00',
    'adventure.skip_bad_weather': False,
    'adventure.batch': 12,
    'visit.times_per_day': 10,
    'visit.start_time': '00:01',
    'pk.times_per_day': 15,
    'pk.start_time': '00:01',
    'care.energy_threshold': 60,
    'care.clean_threshold': 60,
    'care.method': '一键护理',
    'employed.action': '等到25/75（小于45min）',
    'notify.win_toast': True,
    'notify.onepush_config': '',
}


def validate_field(key: str, value):
    """校验单个配置项，返回 (是否通过, 修正后的值)；不通过时给出默认值。"""
    default = DEFAULTS.get(key)
    if key in ('adventure.start_time', 'visit.start_time', 'pk.start_time'):
        try:
            datetime.strptime(str(value), '%H:%M')
            # 必须带引号写回：9:00 不带引号会被 YAML 1.1 解析成整数 540
            return True, DoubleQuotedScalarString(str(value))
        except ValueError:
            return False, DoubleQuotedScalarString(str(default))
    if key == 'work.location':
        return (True, value) if str(value).strip() else (False, default)
    if key == 'school.attribute':
        return (True, value) if value in ('力量', '智力', '魅力') else (False, default)
    if key == 'care.method':
        return (True, value) if value in ('ocr检测', '一键护理') else (False, default)
    if key in ('care.energy_threshold', 'care.clean_threshold'):
        return (True, value) if 0 <= int(value) <= 100 else (False, default)
    if key == 'schedule.check_interval':
        # 检查间隔至少 1 秒（0 会变成无间隔死循环）
        try:
            return (True, value) if int(value) >= 1 else (False, default)
        except (TypeError, ValueError):
            return False, default
    if key == 'notify.win_toast' or key == 'adventure.skip_bad_weather':
        return (True, value) if isinstance(value, bool) else (False, default)
    if key == 'notify.onepush_config':
        # OnePush 推送配置（YAML，支持多行）：留空，或能解析出含 provider 的字典
        text = str(value).strip()
        if not text:
            return True, ''
        try:
            cfg = yaml.safe_load(text)
        except yaml.YAMLError:
            return False, default
        if not (isinstance(cfg, dict) and cfg.get('provider')):
            return False, default
        # 多行用块样式写回（保留换行可读性），单行仍用带引号标量
        if '\n' in text:
            return True, LiteralScalarString(text)
        return True, DoubleQuotedScalarString(text)
    # 其余整数字段（次数/阈值/系数）非负即可，空字符串不允许
    if isinstance(DEFAULTS.get(key), int):
        try:
            return (True, value) if int(value) >= 0 else (False, default)
        except (TypeError, ValueError):
            return False, default
    return True, value


def load_raw():
    """读取 config.yaml 为可修改的映射对象（保留注释）。"""
    with open(CONFIG_FILE, encoding='utf-8') as f:
        return _yaml.load(f) or {}


def save_raw(data) -> None:
    """写回 config.yaml（保留原有注释和格式）。"""
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        _yaml.dump(data, f)


def get_value(data, dotted_key: str, default=None):
    """按 'school.attribute' 形式的点路径取值。"""
    cur = data
    for part in dotted_key.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_value(data, dotted_key: str, value) -> None:
    """按点路径写值，中间层级不存在则创建。"""
    parts = dotted_key.split('.')
    cur = data
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value
