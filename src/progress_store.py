"""进度文件统一管理：跨天规整 + 原子写入 + 损坏兜底。

runs/*_progress.json（学习/打工/冒险/踩踩/PK/被雇佣/雇佣好友/经验日常）的读写
统一走本模块，保证：

- **跨天规整**：旧日期的当天次数归档进 history，并清掉旧日期的累计时长
  （study_secs/work_secs）与当天计数（learned）；school/duration 是"当前会话"
  元数据——昨晚开始的打工/上课今天收尾时，结算要靠它把时长累计到今天，所以
  不随跨天清零，由 get_daily_field 按 date==今天 门控。
- **原子写入**：先写 <文件>.tmp 再 os.replace 覆盖，进程被杀/崩溃不会留下
  写了一半的损坏文件（曾出现 school_progress.json 被截断成 .corrupted.bak）。
- **损坏兜底**：JSON 解析失败时把原文件备份成 <名字>.corrupted.bak（已存在则
  加时间戳）后按空档继续，不静默丢数据。

src/progress.py 是对外兼容层（保持各场景/runner/GUI 的旧函数签名），内部全部
转发到这里；测试重定向进度文件时仍按老习惯 monkeypatch src.progress 里的
文件常量即可（progress.py 在调用时才读模块全局常量）。
"""
from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable

_log: Callable[[str], None] = lambda msg: None


def set_logger(fn: Callable[[str], None]) -> None:
    """注册日志回调（src/progress.log）；未注册时静默。"""
    global _log
    _log = fn


def today_str() -> str:
    return date.today().isoformat()


def to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _backup_corrupted(path: Path) -> None:
    """把损坏的进度文件备份成 *.corrupted.bak（已存在则加时间戳）。"""
    try:
        if not path.is_file():
            return
        bak = path.with_name(path.stem + '.corrupted.bak')
        if bak.exists():
            bak = path.with_name(f'{path.stem}.corrupted.{int(time.time())}.bak')
        os.replace(str(path), str(bak))
        _log(f'进度文件损坏，已备份到 {bak.name}，按空档继续')
    except OSError as e:
        _log(f'备份损坏进度文件失败: {e}')


def read_raw(path: Path) -> dict:
    """读进度文件为 dict；文件不存在/JSON 损坏返回 {}（损坏先备份）。"""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        _backup_corrupted(path)
        return {}


def write_raw(path: Path, data: dict) -> None:
    """原子写入：先写 <文件>.tmp 再 os.replace 覆盖（避免写一半被杀留损坏文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(str(tmp), str(path))


def normalize(data: dict) -> tuple[dict, bool]:
    """把进度数据规整到"今天"，返回 (data, 是否跨天)。

    跨天时：旧日期当天次数归档进 history（不覆盖已有条目）、清零 learned、
    清掉旧日期的累计时长（study_secs/work_secs）。school/duration 保留
    （会话元数据，见模块 docstring）。同一天原样返回。
    """
    today = today_str()
    if data.get('date') == today:
        return data, False
    history = data.get('history')
    history = {str(k): v for k, v in history.items()} if isinstance(history, dict) else {}
    old_date = data.get('date')
    if old_date:
        old_done = to_int(data.get('learned', 0))
        if old_done:
            history.setdefault(str(old_date), old_done)
    data['date'] = today
    data['history'] = history
    data['learned'] = 0
    data.pop('study_secs', None)
    data.pop('work_secs', None)
    return data, True


# ---- 每日次数进度（learned + history + 可选扩展字段） ----

def load_daily(path: Path) -> tuple[str, int, dict]:
    """读取每日次数进度，返回 (今天, 当天次数, history含今天)。

    跨天时只做内存归档（done=0、旧日期进 history），不落盘。
    """
    today = today_str()
    data = read_raw(path)
    data, _ = normalize(data)
    done = to_int(data.get('learned', 0))
    history = data.get('history')
    history = ({str(k): to_int(v) for k, v in history.items()}
               if isinstance(history, dict) else {})
    history[today] = done
    return today, done, history


def save_daily(path: Path, today: str, done: int, history: dict | None) -> None:
    """持久化当天次数与历史记录。

    写盘前先 normalize：跨天时清掉旧日期的累计时长（这是"昨日时长漏进今天"
    的根治点——无论调用顺序如何，只要原文件日期不是今天，旧时长必被清掉）。
    """
    data = read_raw(path)
    data, _ = normalize(data)
    merged = data.get('history')
    merged = ({str(k): to_int(v) for k, v in merged.items()}
              if isinstance(merged, dict) else {})
    if isinstance(history, dict):
        for k, v in history.items():
            merged[str(k)] = v
    today = str(today)
    merged[today] = to_int(done)
    data['date'] = today
    data['learned'] = to_int(done)
    data['history'] = merged
    write_raw(path, data)


def increment_daily(path: Path) -> int:
    """当天次数 +1 并保存，返回新次数。"""
    today, done, history = load_daily(path)
    done += 1
    save_daily(path, today, done, history)
    return done


def get_daily_field(path: Path, key: str) -> Any | None:
    """读取当天才有效的扩展字段（如 school/duration）；文件日期不是今天返回 None。"""
    data = read_raw(path)
    if data.get('date') == today_str():
        return data.get(key)
    return None


def set_daily_field(path: Path, key: str, value) -> None:
    """写入扩展字段：跨天时先规整（清旧时长、归档）并**总是落盘**推进日期，
    否则只在值变化时写（减少落盘）。"""
    data = read_raw(path)
    data, day_changed = normalize(data)
    if day_changed or data.get(key) != value:
        data[key] = value
        write_raw(path, data)


def today_seconds(path: Path, key: str) -> int:
    """今天的累计秒数字段（study_secs/work_secs）；文件日期不是今天返回 0。"""
    data = read_raw(path)
    if data.get('date') == today_str():
        return to_int(data.get(key, 0))
    return 0


def add_seconds(path: Path, key: str, seconds: int) -> int:
    """当天累计秒数 +seconds 并保存，返回新累计值（跨天先规整清旧值）。"""
    data = read_raw(path)
    data, _ = normalize(data)
    total = to_int(data.get(key, 0)) + to_int(seconds)
    data[key] = total
    write_raw(path, data)
    return total


# ---- 经验日常（done 为 bool） ----

def load_exp_daily(path: Path) -> tuple[str, bool, dict]:
    """读取经验日常进度，返回 (今天, 当天是否完成, history含今天)。"""
    today = today_str()
    data = read_raw(path)
    history = data.get('history')
    history = {str(k): bool(v) for k, v in history.items()} if isinstance(history, dict) else {}
    saved_date = data.get('date')
    done = False
    if saved_date == today:
        done = bool(data.get('done', False))
    elif saved_date:
        history[str(saved_date)] = bool(data.get('done', False))
    history[today] = done
    return today, done, history


def save_exp_daily(path: Path, done: bool, today: str | None = None,
                   history: dict | None = None) -> None:
    """持久化经验日常是否完成（原子写入）。"""
    if today is None or history is None:
        t, d, h = load_exp_daily(path)
        if today is None:
            today = t
        if history is None:
            history = h
    history[str(today)] = bool(done)
    write_raw(path, {'date': str(today), 'done': bool(done), 'history': history})
