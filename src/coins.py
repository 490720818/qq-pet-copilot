"""主页金币数量识别：模板定位 main_sign，OCR 其后方区域并解析数值。

金币显示格式如 "1.6k"（=1600），也兼容纯数字和 w/万 单位。
"""
from __future__ import annotations

import re

import cv2
import numpy as np

from .ocr import ocr_texts

# 金币文字相对 main_sign 中心的位置：后方（右）120 像素，上下 50 像素范围内
# （左右留宽一些防止切掉多位数字，识别前放大 2 倍提高小字识别率）
COIN_OFFSET_X = 120
COIN_HALF_W = 80
COIN_HALF_H = 50
COIN_SCALE = 2

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


def read_coins(screen: np.ndarray, main_sign_pos: tuple[int, int, float]) -> int | None:
    """在 main_sign 后方区域 OCR 金币数量，识别失败返回 None。

    main_sign_pos: see('main_sign') 的返回值 (中心x, 中心y, score)。
    """
    x, y = main_sign_pos[0], main_sign_pos[1]
    h, w = screen.shape[:2]
    x1 = max(0, x + COIN_OFFSET_X - COIN_HALF_W)
    x2 = min(w, x + COIN_OFFSET_X + COIN_HALF_W)
    y1 = max(0, y - COIN_HALF_H)
    y2 = min(h, y + COIN_HALF_H)
    crop = screen[y1:y2, x1:x2]
    crop = cv2.resize(crop, None, fx=COIN_SCALE, fy=COIN_SCALE,
                      interpolation=cv2.INTER_CUBIC)
    for text, _, _, _ in ocr_texts(crop):
        coins = parse_coin(text)
        if coins is not None:
            return coins
    return None
