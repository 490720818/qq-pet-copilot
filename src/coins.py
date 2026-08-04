"""主页金币数量识别：OCR 整屏，取顶部状态栏最右侧的类金币数字。

主页顶部状态栏从左到右依次是 星星 / 爪印 / 金币 三个数值，
金币在最右侧；格式如 "1.6k"（=1600），也兼容纯数字和 w/万 单位。
（原实现靠 main_sign 模板定位后裁剪右侧区域，现改为全屏 OCR，分辨率无关。）
"""
from __future__ import annotations

import re

import numpy as np

from .ocr import ocr_texts

# 金币栏在屏幕顶部的纵向范围（相对高度比例）
COIN_BAR_TOP = 0.08
COIN_BAR_BOTTOM = 0.22

_COIN_RE = re.compile(r'^(\d+(?:\.\d+)?)([kKwW万]?)$')


def parse_coin(text: str) -> int | None:
    """解析金币文本为数值，如 '1.6k' -> 1600，'800' -> 800；无法解析返回 None。"""
    m = _COIN_RE.match(text.strip().replace(',', '').replace(' ', ''))
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2).lower()
    if unit == 'k':
        value *= 1000
    elif unit in ('w', '万'):
        value *= 10000
    return int(value)


def read_coins(screen: np.ndarray) -> int | None:
    """OCR 整屏，取顶部状态栏最右侧可解析为金币的数字，识别失败返回 None。"""
    h = screen.shape[0]
    y1, y2 = int(h * COIN_BAR_TOP), int(h * COIN_BAR_BOTTOM)
    candidates = []
    for text, x, y, _ in ocr_texts(screen):
        if not (y1 <= y <= y2):
            continue
        coins = parse_coin(text)
        if coins is not None:
            candidates.append((x, coins))
    if not candidates:
        return None
    return max(candidates)[1]
