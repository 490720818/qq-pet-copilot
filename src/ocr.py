"""RapidOCR 文字识别封装：整屏识别文字及中心坐标。

引擎懒加载（首次调用时加载模型，约需几秒）。
输入为 OpenCV BGR 图（与 vision.png_to_bgr 输出一致）。
"""
from __future__ import annotations

import numpy as np

_engine = None


def get_engine():
    """懒加载 RapidOCR 引擎（全局单例）。"""
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR

        _engine = RapidOCR()
    return _engine


def ocr_texts(screen: np.ndarray) -> list[tuple[str, int, int, float]]:
    """识别整屏文字，返回 [(文字, 中心x, 中心y, 置信度)]。"""
    result, _ = get_engine()(screen)
    out = []
    for box, text, score in result or []:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append((text, int(sum(xs) / 4), int(sum(ys) / 4), float(score)))
    return out


def find_text(
    results: list[tuple[str, int, int, float]], target: str
) -> tuple[int, int, float] | None:
    """在 OCR 结果中找包含 target 的文字，返回 (中心x, 中心y, 置信度) 或 None。"""
    for text, x, y, score in results:
        if target in text:
            return x, y, score
    return None
