"""场景公共工具：日志与每日次数持久化（含按日期的历史记录）。

多账号（同一设备换 QQ 账号轮流托管）：账号名唯一来源是 care 状态面板 OCR
（每轮调度最先执行，写进状态缓存的 last_account）。识别到账号后，
load_progress/save_progress 自动把进度文件重定向到 runs/accounts/<账号名>/ 下；
首次识别时把单账号时期 runs/ 下的旧进度文件迁移进账号目录（历史记录保留）。
未识别账号时仍用 runs/ 下的默认路径。
"""
from __future__ import annotations

import json
import re
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

# 按账号的进度文件目录（识别到账号后 load/save 自动重定向到这里）
ACCOUNTS_DIR = PROJECT_ROOT / 'runs' / 'accounts'

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows 保留设备名（不含非法字符但也不能做目录名）
_RESERVED_NAMES = {'con', 'prn', 'aux', 'nul',
                   *(f'com{i}' for i in range(1, 10)),
                   *(f'lpt{i}' for i in range(1, 10))}


def sanitize_account(name: str) -> str:
    """账号名转安全目录名：Windows 非法字符/控制字符替换为 _，
    去掉首尾空白和尾部点号（Windows 会静默剥掉尾部点，造成路径不一致），
    保留设备名（CON/PRN/NUL/COM1-9/LPT1-9）加下划线，空名回退 default。"""
    safe = _INVALID_FILENAME_CHARS.sub('_', name).strip().rstrip('.')
    if not safe:
        return 'default'
    if safe.lower() in _RESERVED_NAMES:
        safe += '_'
    return safe


def current_account() -> str | None:
    """当前账号名：状态缓存的 last_account（care 状态面板 OCR 每轮写入）。
    'default' 是匿名写入的过渡条目，视为未识别（进度仍走默认路径）。"""
    try:
        from .status_cache import STATUS_CACHE_FILE  # 延迟导入避免循环依赖
        data = json.loads(STATUS_CACHE_FILE.read_text(encoding='utf-8'))
        name = data.get('last_account')
    except (OSError, ValueError):
        return None
    if not isinstance(name, str) or not name or name == 'default':
        return None
    return name


def resolve_progress_file(progress_file: Path, account: str | None = None) -> Path:
    """把进度文件路径解析到账号目录。

    account=None 用当前账号；显式传账号名（GUI 按账号读）不做迁移；
    未识别账号/传空串返回原默认路径。
    用当前账号且目标不存在、默认路径有旧文件时，把旧文件迁移进账号目录
    （单账号时期的历史记录归到当前账号，每个进度文件只在首次解析时迁一次）。
    """
    is_current = account is None
    if is_current:
        account = current_account()
    if not account:
        return progress_file
    target = ACCOUNTS_DIR / sanitize_account(account) / progress_file.name
    if is_current and not target.exists() and progress_file.exists():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            progress_file.replace(target)
            log(f'进度文件已归入账号 {account}: {progress_file.name}')
        except OSError as e:
            log(f'进度文件迁移失败 {progress_file.name}: {e}')
    return target


def known_accounts() -> list[str]:
    """所有已知账号（状态缓存记录 ∪ runs/accounts/ 目录），供 GUI 按账号显示。
    当前账号排在最前（状态条/统计条/折线图默认都先展示当前账号）。"""
    names: list[str] = []
    try:
        from .status_cache import load_accounts  # 延迟导入避免循环依赖
        names.extend(n for n in load_accounts() if n != 'default')
    except Exception:
        pass
    if ACCOUNTS_DIR.is_dir():
        for d in sorted(ACCOUNTS_DIR.iterdir()):
            if d.is_dir() and d.name not in names:
                names.append(d.name)
    cur = current_account()
    if cur in names:
        names.remove(cur)
        names.insert(0, cur)
    return names


def log(msg: str) -> None:
    line = f'[{time.strftime("%H:%M:%S")}] {msg}'
    print(line, flush=True)
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


def load_progress(progress_file: Path, quiet: bool = False,
                  account: str | None = None) -> tuple[str, int, dict]:
    """读取持久化进度，返回 (今天日期, 当天已完成次数, 历史记录 {日期: 次数})。

    跨天时把旧日期的次数归档进 history。quiet=True 时不打印续跑日志。
    account=None 按当前账号解析路径（多账号）；显式传账号名读指定账号（GUI 用）。
    """
    progress_file = resolve_progress_file(progress_file, account)
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


def save_progress(progress_file: Path, today: str, done: int, history: dict,
                  account: str | None = None) -> None:
    """持久化当天次数和历史记录 {日期: 次数}（account 同 load_progress）。"""
    progress_file = resolve_progress_file(progress_file, account)
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
