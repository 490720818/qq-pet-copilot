"""宠物状态缓存：体力/清洁/心情/金币/香皂/饼干，供 GUI 日志页顶部状态条显示。

runs/status_cache.json 单条目（曾经按账号名称组织兼容多账号，但账号名靠
状态面板 OCR 识别不稳定——时钟会被误识别成账号，状态条出现多行——已取消
多账号区分，固定写 default 条目）：
{
  "accounts": {"default": {"pet_name": ..., "energy": ..., "clean": ...,
               "mood": ..., "coins": ..., "soap": ..., "biscuit": ...,
               "updated": "..."}}
}

写入点：
- care.check_and_care：状态面板 OCR 后写 体力/清洁/心情/宠物名称
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
    return {'accounts': {}}


def load_accounts() -> dict[str, dict]:
    """读取状态缓存（{default: {字段: 值}}），供 GUI 状态条显示。"""
    return _load()['accounts']


def _save(data: dict) -> None:
    try:
        STATUS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATUS_CACHE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError as e:
        log(f'状态缓存写入失败: {e}')


def clear_status_fields(*keys: str) -> None:
    """删除缓存条目里的指定字段（GUI 显示回 '-'）。

    一键护理后不读状态面板，体力/清洁/饼干/香皂的缓存值不再可信，调用方清空。
    """
    data = _load()
    entry = data['accounts'].get(DEFAULT_ACCOUNT)
    if not entry:
        return
    for key in keys:
        entry.pop(key, None)
    _save(data)


def update_status(account: str | None = None, **fields) -> None:
    """合并写入状态字段（值为 None 的跳过）。

    account 参数保留仅为兼容旧调用（care 曾传状态面板 OCR 的账号名称），
    已取消多账号区分，一律写 default 条目；老缓存文件里的多账号条目
    在第一次写入时丢弃（自愈，避免状态条一直显示残留的旧账号行）。
    """
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return
    data = _load()
    entry = data['accounts'].get(DEFAULT_ACCOUNT, {})
    entry.update(fields)
    entry['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
    data['accounts'] = {DEFAULT_ACCOUNT: entry}
    _save(data)
