"""场景公共工具：日志与每日次数持久化（含按日期的历史记录）。

进度文件固定为 runs/ 下的单文件（学习/打工/冒险/踩踩/PK/被雇佣/雇佣好友/
经验日常各一个）。曾经按账号重定向到 runs/accounts/<账号>/，但账号名靠
状态面板 OCR 识别不稳定（时钟会被误识别成账号），数据被拆散，已取消多账号区分。
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

from .config import PROJECT_ROOT

SCHOOL_PROGRESS_FILE = PROJECT_ROOT / 'runs' / 'school_progress.json'
WORK_PROGRESS_FILE = PROJECT_ROOT / 'runs' / 'work_progress.json'
ADVENTURE_PROGRESS_FILE = PROJECT_ROOT / 'runs' / 'adventure_progress.json'
EMPLOYED_PROGRESS_FILE = PROJECT_ROOT / 'runs' / 'employed_progress.json'
VISIT_PROGRESS_FILE = PROJECT_ROOT / 'runs' / 'visit_progress.json'
PK_PROGRESS_FILE = PROJECT_ROOT / 'runs' / 'pk_progress.json'
HIRE_FRIEND_PROGRESS_FILE = PROJECT_ROOT / 'runs' / 'hire_friend_progress.json'


def log(msg: str) -> None:
    line = f'[{time.strftime("%H:%M:%S")}] {msg}'
    try:  # 打包成 --windowed 后没有控制台，stdout 可能是无效流，不能让它拖垮日志
        print(line, flush=True)
    except (OSError, UnicodeError):
        pass
    try:  # 写入日志文件 runs/logs/YYYY-MM-DD.log
        log_dir = PROJECT_ROOT / 'runs' / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / f'{date.today().isoformat()}.log', 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass
    for listener in _log_listeners:
        try:
            listener(line)
        except Exception:
            pass


_log_listeners: list = []


def add_log_listener(fn) -> None:
    """注册日志监听器（GUI 实时日志用），每行日志都会回调 fn(line)。"""
    _log_listeners.append(fn)


def load_progress(progress_file: Path, quiet: bool = False) -> tuple[str, int, dict]:
    """读取持久化进度，返回 (今天日期, 当天已完成次数, 历史记录 {日期: 次数})。

    跨天时把旧日期的次数归档进 history。quiet=True 时不打印续跑日志。
    """
    today = date.today().isoformat()
    history: dict[str, int] = {}
    done = 0
    try:
        data = json.loads(progress_file.read_text(encoding='utf-8'))
        history = {
            str(day): int(cnt) for day, cnt in (data.get('history') or {}).items()
        }
        saved_date = data.get('date')
        saved_done = int(data.get('learned', 0))
        if saved_date == today:
            done = saved_done
        elif saved_date and saved_done:
            history[saved_date] = saved_done  # 跨天归档
    except (OSError, ValueError):
        pass
    history[today] = done
    if done and not quiet:
        log(f'读取到今天已完成 {done} 次，继续计数')
    return today, done, history


def save_progress(progress_file: Path, today: str, done: int, history: dict) -> None:
    """持久化当天次数和历史记录 {日期: 次数}。"""
    history[today] = done
    progress_file.parent.mkdir(parents=True, exist_ok=True)
    progress_file.write_text(
        json.dumps(
            {'date': today, 'learned': done, 'history': history},
            ensure_ascii=False, indent=2,
        ),
        encoding='utf-8',
    )


def log_history(history: dict, today: str) -> None:
    """启动时打印历史记录（不含今天）。"""
    past = {d: c for d, c in sorted(history.items()) if d != today and c}
    if past:
        log('历史记录: ' + '，'.join(f'{d} {c} 次' for d, c in past.items()))


def increment_progress(progress_file: Path) -> int:
    """给指定进度文件的当天次数 +1 并保存，返回新次数。"""
    today, done, history = load_progress(progress_file, quiet=True)
    done += 1
    save_progress(progress_file, today, done, history)
    return done


# ---- 经验日常（踩踩时顺带做：好友页照顾区域有 exp 就点一键护理） ----
EXP_DAILY_PROGRESS_FILE = PROJECT_ROOT / 'runs' / 'exp_daily_progress.json'


def load_exp_daily(quiet: bool = False) -> tuple[str, bool, dict]:
    """读取经验日常进度，返回 (今天日期, 当日是否完成, 历史记录 {日期: 是否完成})。"""
    progress_file = EXP_DAILY_PROGRESS_FILE
    today = date.today().isoformat()
    history: dict[str, bool] = {}
    done = False
    try:
        data = json.loads(progress_file.read_text(encoding='utf-8'))
        history = {str(d): bool(v) for d, v in (data.get('history') or {}).items()}
        saved_date = data.get('date')
        if saved_date == today:
            done = bool(data.get('done', False))
        elif saved_date:
            history[saved_date] = bool(data.get('done', False))
    except (OSError, ValueError):
        pass
    history[today] = done
    if not quiet:
        log(f'经验日常: ' + ('已完成' if done else '未完成'))
    return today, done, history


def save_exp_daily(done: bool, today: str | None = None, history: dict | None = None) -> None:
    """持久化当天经验日常是否完成和历史记录。"""
    progress_file = EXP_DAILY_PROGRESS_FILE
    if today is None or history is None:
        t, d, h = load_exp_daily(quiet=True)
        if today is None:
            today = t
        if history is None:
            history = h
    history[today] = done
    progress_file.parent.mkdir(parents=True, exist_ok=True)
    progress_file.write_text(
        json.dumps({'date': today, 'done': done, 'history': history},
                   ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def exp_daily_done() -> bool:
    """今天经验日常是否已完成（供调度判断是否仍需处理）。"""
    _, done, _ = load_exp_daily(quiet=True)
    return done


def log_exp_daily() -> None:
    """打印经验日常当天状态与历史（日志/GUI 显示用）。"""
    today, done, history = load_exp_daily(quiet=True)
    past = {d: v for d, v in sorted(history.items()) if d != today}
    line = f'经验日常: ' + ('已完成' if done else '未完成')
    if past:
        line += '（历史: ' + '，'.join(f'{d} ' + ('完成' if v else '未完成')
                                      for d, v in past.items()) + '）'
    log(line)


# 活动类型 -> (进度文件, 中文量词, 计数名)，用于出门时等完别的活动后的交叉计数
CROSS_PROGRESS = {
    'school': (SCHOOL_PROGRESS_FILE, '一节课', '学习'),
    'work': (WORK_PROGRESS_FILE, '一次打工', '打工'),
    'adventure': (ADVENTURE_PROGRESS_FILE, '一次冒险', '冒险'),
    'employed': (EMPLOYED_PROGRESS_FILE, '一次被雇佣', '被雇佣'),
}


def count_cross(finished: str) -> None:
    """出门时等完了别的活动，计入对应进度并打日志。"""
    file, unit, name = CROSS_PROGRESS[finished]
    n = increment_progress(file)
    log(f'出门时等完了{unit}，已计入{name}次数（{n} 次）')
