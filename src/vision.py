"""OpenCV 模板匹配识别。

模板放在 templates/ 目录，坐标系为手机物理像素，与 Device.tap 一致。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .config import resource_path

TEMPLATES_DIR = resource_path("templates")
DEFAULT_THRESHOLD = 0.8

_cache: dict[str, np.ndarray] = {}


def load_template(name: str) -> np.ndarray:
    """按名加载模板（可省略 .png 后缀），带缓存。"""
    key = name.removesuffix(".png")
    if key not in _cache:
        path = TEMPLATES_DIR / f"{key}.png"
        img = cv2.imread(str(path))
        if img is None:
            raise FileNotFoundError(f"模板不存在或无法读取: {path}")
        _cache[key] = img
    return _cache[key]


def png_to_bgr(png: bytes) -> np.ndarray:
    """PNG 字节 -> OpenCV BGR 图。"""
    data = np.frombuffer(png, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("截图解码失败")
    return image


def find(
    screen: np.ndarray, template_name: str, threshold: float = DEFAULT_THRESHOLD
) -> tuple[int, int, float] | None:
    """在截图中找模板，命中返回 (中心x, 中心y, 匹配度)，否则 None。"""
    tpl = load_template(template_name)
    sh, sw = screen.shape[:2]
    th, tw = tpl.shape[:2]
    if th > sh or tw > sw:
        return None
    result = cv2.matchTemplate(screen, tpl, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    if max_val < threshold:
        return None
    return max_loc[0] + tw // 2, max_loc[1] + th // 2, float(max_val)


def find_all(
    screen: np.ndarray, template_name: str, threshold: float = DEFAULT_THRESHOLD
) -> list[tuple[int, int, float]]:
    """在截图中找模板的所有命中位置（同一屏幕可能有多个相同按钮）。

    返回 [(中心x, 中心y, 匹配度)]，按从上到下、从左到右排序。
    """
    tpl = load_template(template_name)
    sh, sw = screen.shape[:2]
    th, tw = tpl.shape[:2]
    if th > sh or tw > sw:
        return []
    result = cv2.matchTemplate(screen, tpl, cv2.TM_CCOEFF_NORMED)
    ys, xs = np.where(result >= threshold)
    # 按匹配度从高到低做 NMS，抑制同一按钮的重叠命中
    order = np.argsort(result[ys, xs])[::-1]
    kept: list[tuple[int, int, float]] = []  # (左上x, 左上y, score)
    for i in order:
        x, y = int(xs[i]), int(ys[i])
        if any(abs(x - kx) < tw // 2 and abs(y - ky) < th // 2 for kx, ky, _ in kept):
            continue
        kept.append((x, y, float(result[y, x])))
    matches = [(x + tw // 2, y + th // 2, s) for x, y, s in kept]
    matches.sort(key=lambda m: (m[1], m[0]))
    return matches
