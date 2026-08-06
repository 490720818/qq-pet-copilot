"""账号状态缓存：体力/清洁/心情/金币/香皂/饼干，供 GUI 日志页顶部状态条显示。

runs/status_cache.json 按账号名称组织（设计上兼容以后多账号）：
{
  "accounts": {"<账号名称>": {"pet_name": ..., "energy": ..., "clean": ...,
               "mood": ..., "coins": ..., "soap": ..., "biscuit": ...,
               "updated": "..."}},
  "last_account": "<账号名称>"
}

写入点：
- care.check_and_care：状态面板 OCR 后写 体力/清洁/心情/账号名称/宠物名称
- care 喂食/洗澡结束：OCR 控件附近区域写 香皂/饼干 库存（同时刷新刚喂完/洗完的数值）
- runner.read_main_coins：主页金币 OCR 后写 金币
GUI 每 5 秒读一次刷新状态条。缓存只是展示用途，读写失败都不影响调度。
"""
from __future__ import annotations

import json
import time

from .config import PROJECT_ROOT
from .progress import log

STATUS_CACHE_FILE = PROJECT_ROOT / 'runs' / 'status_cache.json'

# 缓存字段 -> 状态条显示名（GUI 按这个顺序展示；pet_name 存了但不显示）
FIELDS = (
    ('energy', '体力'),
    ('clean', '清洁'),
    ('mood', '心情'),
    ('coins', '金币'),
    ('biscuit', '饼干'),
    ('soap', '香皂'),
)

DEFAULT_ACCOUNT = 'default'


def _load() -> dict:
    try:
        data = json.loads(STATUS_CACHE_FILE.read_text(encoding='utf-8'))
        if isinstance(data, dict) and isinstance(data.get('accounts'), dict):
            return data
    except (OSError, ValueError):
        pass
    return {'accounts': {}, 'last_account': None}


def load_accounts() -> dict[str, dict]:
    """读取全部账号的状态缓存（{账号名称: {字段: 值}}），供 GUI 状态条显示。"""
    return _load()['accounts']


def _save(data: dict) -> None:
    try:
        STATUS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATUS_CACHE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError as e:
        log(f'状态缓存写入失败: {e}')


def clear_status_fields(*keys: str, account: str | None = None) -> None:
    """删除账号条目里的指定字段（GUI 显示回 '-'）。

    一键护理后不读状态面板，体力/清洁/饼干/香皂的缓存值不再可信，调用方清空。
    account=None 时操作最近使用的账号。
    """
    data = _load()
    account = account or data.get('last_account')
    entry = data['accounts'].get(account or '')
    if not entry:
        return
    for key in keys:
        entry.pop(key, None)
    _save(data)


def _normalize_account(account: str) -> str:
    """账号名归一化：状态面板 OCR 可能把长昵称截断成 'Hydrogeniu...'，
    去掉尾部省略号/空白，避免同一账号因截断形式不同产生多个条目。"""
    return account.strip().rstrip('.…')


def update_status(account: str | None, **fields) -> None:
    """合并写入某账号的状态字段（值为 None 的跳过）。

    account 为 None 时写入最近使用的账号（金币等不知道账号名的写入点
    靠这个归到当前账号；还没有任何账号时记到 default 过渡条目——
    第一次拿到真实账号名时 default 条目会被合并过来并删除，
    避免状态条出现 default + 真实账号两行）。
    """
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return
    data = _load()
    account = account or data.get('last_account') or DEFAULT_ACCOUNT
    account = _normalize_account(account) or DEFAULT_ACCOUNT
    accounts = data['accounts']
    if account != DEFAULT_ACCOUNT and DEFAULT_ACCOUNT in accounts:
        stale = accounts.pop(DEFAULT_ACCOUNT)
        target = accounts.setdefault(account, {})
        for key, value in stale.items():
            if key != 'updated' and key not in target:
                target[key] = value
    entry = accounts.setdefault(account, {})
    entry.update(fields)
    entry['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
    data['last_account'] = account
    _save(data)
