"""RapidOCR 文字识别封装：整屏识别文字及中心坐标。

引擎懒加载（首次调用时加载模型，约需几秒）。
输入为 numpy RGB 图（u2dev.U2Device.screenshot 的输出）。
"""
from __future__ import annotations

import re

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
    """在 OCR 结果中找包含 target 的文字，返回 (中心x, 中心y, 置信度) 或 None。

    单行碎片兜底：大字号标题常被 OCR 拆成单字/错字碎片
    （如"被雇佣中"识别成 被/皮雇/佣/中），逐条匹配必然落空，
    把同一行的碎片按 x 拼接后再找 target。
    """
    for text, x, y, score in results:
        if target in text:
            return x, y, score

    lines: list[list[tuple[str, int, int, float]]] = []
    for item in sorted(results, key=lambda r: (r[2], r[1])):
        for line in lines:
            if abs(line[0][2] - item[2]) <= 10:
                line.append(item)
                break
        else:
            lines.append([item])
    for line in lines:
        line.sort(key=lambda r: r[1])
        merged = ''.join(text for text, *_ in line)
        idx = merged.find(target)
        if idx < 0:
            continue
        # 命中片段覆盖的碎片：x 取首尾碎片中心的中点，置信度取最低
        pos, first, last = 0, 0, len(line) - 1
        for i, (text, *_rest) in enumerate(line):
            if pos <= idx < pos + len(text):
                first = i
            if pos < idx + len(target) <= pos + len(text):
                last = i
            pos += len(text)
        x = (line[first][1] + line[last][1]) // 2
        score = min(r[3] for r in line[first:last + 1])
        return x, line[0][2], score
    return None


def find_all_text(
    results: list[tuple[str, int, int, float]], target: str
) -> list[tuple[int, int, float]]:
    """在 OCR 结果中找所有包含 target 的文字（同一屏幕可能有多个相同按钮）。

    返回 [(中心x, 中心y, 置信度)]，按从上到下、从左到右排序。
    """
    matches = [(x, y, score) for text, x, y, score in results if target in text]
    matches.sort(key=lambda m: (m[1], m[0]))
    return matches


def parse_employed_ratio(
    results: list[tuple[str, int, int, float]],
    max_employer: int = 25,
    min_employed: int = 75,
) -> tuple[int, int, float] | None:
    """在 OCR 结果中解析被雇佣面板的分成比例行"雇佣者 x% / 被雇佣者 y%"。

    仅当 x <= max_employer 且 y >= min_employed（宠物分成最高的终态，
    方向不能反，75/25 不算）时命中，返回比例行中心 (x, y, 置信度)，否则 None。
    """
    employer = employed = None
    pcts = []
    for text, x, y, score in results:
        t = text.strip()
        if t == '雇佣者':
            employer = (x, y, score)
        elif '被雇佣者' in t:
            employed = (x, y, score)
        m = re.fullmatch(r'(\d+)%', t)
        if m:
            pcts.append((int(m.group(1)), x, y))
    if not (employer and employed):
        return None

    def pct_below(label: tuple[int, int, float]) -> int | None:
        lx, ly, _ = label
        for value, x, y in pcts:
            if abs(x - lx) <= 60 and 0 < y - ly <= 80:
                return value
        return None

    e_pct = pct_below(employer)
    d_pct = pct_below(employed)
    if e_pct is None or d_pct is None:
        return None
    if e_pct <= max_employer and d_pct >= min_employed:
        x = (employer[0] + employed[0]) // 2
        return x, employer[1], min(employer[2], employed[2])
    return None
