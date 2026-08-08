"""RapidOCR 文字识别封装：整屏识别文字及中心坐标。

引擎懒加载（首次调用时加载模型，约需几秒）。
输入为 numpy RGB 图（u2dev.U2Device.screenshot 的输出）。
整屏识别统一走 ocr_fullscreen()：先保持长宽比缩放到接近 720x1280 级别
再 OCR（高分辨率截图识别慢，缩放后速度与设备分辨率无关），
返回坐标已还原回原图像素，可直接用于点击。

引擎：rapidocr 3.8.x（onnxruntime，PP-OCRV5 mobile 模型）。
模型目录 APP_ROOT/runs/models/rapidocr：只使用 PP-OCRV5 mobile（v5），
不打/不初始化 v4 模型；打包随附的 v5 首次运行时复制出来，
缺失时用 tools/fetch_ocr_models.py 或首次联网自动下载。
"""
from __future__ import annotations

import hashlib
import re
import shutil
import time
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

from .config import APP_ROOT, RESOURCE_ROOT
from .progress import log

_engine = None

# 整屏 OCR 前把截图等比缩放到这个宽度（保持长宽比，接近 720x1280 级别）
OCR_FULLSCREEN_WIDTH = 720

# OCR 模型目录（可写、持久，与 runs/ 进度日志同目录）：rapidocr 的 Global.model_root_dir
MODEL_ROOT = APP_ROOT / 'runs' / 'models' / 'rapidocr'

# PP-OCRV5 mobile（det/rec）与 cls 模型：URL 与 SHA256 取自 rapidocr default_models.yaml
V5_MODELS = [
    ('ch_PP-OCRv5_det_mobile.onnx',
     'https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.8.0/onnx/PP-OCRv5/det/ch_PP-OCRv5_det_mobile.onnx',
     '4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae'),
    ('ch_PP-OCRv5_rec_mobile.onnx',
     'https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.8.0/onnx/PP-OCRv5/rec/ch_PP-OCRv5_rec_mobile.onnx',
     '5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5'),
    ('ch_ppocr_mobile_v2.0_cls_mobile.onnx',
     'https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.8.0/onnx/PP-OCRv4/cls/ch_ppocr_mobile_v2.0_cls_mobile.onnx',
     'e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c'),
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _ensure_models() -> None:
    """保证模型目录存在，并把打包随附的 v5 模型复制出来（避免首次联网下载）。

    只处理 PP-OCRV5 mobile（RESOURCE_ROOT/models/rapidocr）；不复制 rapidocr 包自带的 v4。
    """
    MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    bundled_res = RESOURCE_ROOT / 'models' / 'rapidocr'
    if not bundled_res.exists():
        return
    for f in bundled_res.glob('*'):
        if not f.is_file():
            continue
        dst = MODEL_ROOT / f.name
        if not dst.exists():
            shutil.copy2(f, dst)


def _download_model(name: str, url: str, expected: str, attempts: int = 3) -> None:
    """下载单个模型到 MODEL_ROOT，SHA256 校验后落盘。

    网络抖动/限流自动重试（同 tools/fetch_scrcpy.py）；重试耗尽后抛异常。
    """
    dst = MODEL_ROOT / name
    tmp = dst.with_suffix('.part')
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp, open(tmp, 'wb') as f:
                shutil.copyfileobj(resp, f)
            if _sha256(tmp) != expected:
                raise RuntimeError(f'{name} SHA256 校验失败')
            tmp.replace(dst)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            tmp.unlink(missing_ok=True)  # 清掉半截文件，避免下次校验坏包
            if i < attempts - 1:
                wait = 2 * (i + 1)
                log(f'下载 OCR 模型 {name} 失败（{e}），{wait}s 后重试（{i + 1}/{attempts}）')
                time.sleep(wait)
    assert last_err is not None
    raise last_err


def ensure_v5_models() -> bool:
    """确保模型目录就绪，并尝试补齐 PP-OCRV5 mobile 模型（缺失时联网下载）。

    返回是否已具备完整 v5 模型；下载失败只记日志并返回 False（调用方抛错，不再回退 v4）。
    """
    MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    _ensure_models()
    for name, url, expected in V5_MODELS:
        dst = MODEL_ROOT / name
        if dst.exists() and _sha256(dst) == expected:
            continue
        log(f'下载 OCR 模型 {name} ...')
        try:
            _download_model(name, url, expected)
        except Exception as e:
            log(f'下载 OCR 模型 {name} 失败: {e}，将无法使用 OCR')
            return False
    return True


def get_engine():
    """懒加载 RapidOCR 引擎（全局单例）。"""
    global _engine
    if _engine is None:
        from rapidocr import EngineType, LangDet, LangRec, ModelType, OCRVersion, RapidOCR

        if not ensure_v5_models():
            # 没有 v5 且下载失败：明确报错，不再静默回退 v4
            raise RuntimeError(
                'PP-OCRV5 mobile 模型缺失且下载失败，OCR 不可用'
                f'（模型目录: {MODEL_ROOT}，可运行 python tools/fetch_ocr_models.py）')
        _engine = RapidOCR(params={
            'Global.model_root_dir': str(MODEL_ROOT),
            'Global.log_level': 'WARN',  # 抑制引擎 INFO 噪音
            'Det.engine_type': EngineType.ONNXRUNTIME,
            'Det.lang_type': LangDet.CH,
            'Det.model_type': ModelType.MOBILE,
            'Det.ocr_version': OCRVersion.PPOCRV5,
            'Rec.engine_type': EngineType.ONNXRUNTIME,
            'Rec.lang_type': LangRec.CH,
            'Rec.model_type': ModelType.MOBILE,
            'Rec.ocr_version': OCRVersion.PPOCRV5,
        })
    return _engine


def ocr_texts(screen: np.ndarray) -> list[tuple[str, int, int, float]]:
    """识别整屏文字，返回 [(文字, 中心x, 中心y, 置信度)]。"""
    res = get_engine()(screen, use_det=True, use_cls=True, use_rec=True)
    boxes = getattr(res, 'boxes', None)
    txts = getattr(res, 'txts', None)
    scores = getattr(res, 'scores', None)
    out = []
    if boxes is None or txts is None or scores is None:
        return out
    for box, text, score in zip(boxes, txts, scores):
        xs = [pt[0] for pt in box]
        ys = [pt[1] for pt in box]
        out.append((text, int(sum(xs) / 4), int(sum(ys) / 4), float(score)))
    return out


def ocr_fullscreen(screen: np.ndarray) -> list[tuple[str, int, int, float]]:
    """整屏 OCR：先保持长宽比缩放到接近 720x1280 级别再识别。

    返回坐标已除以缩放系数、还原回原图（截图）像素，可直接用于点击。
    """
    h, w = screen.shape[:2]
    if w <= OCR_FULLSCREEN_WIDTH:
        return ocr_texts(screen)  # 不做放大：宽度小于等于 720 直接原样识别
    scale = OCR_FULLSCREEN_WIDTH / w
    img = Image.fromarray(screen)
    img = img.resize((OCR_FULLSCREEN_WIDTH, round(h * scale)), Image.LANCZOS)
    return [(text, round(x / scale), round(y / scale), score)
            for text, x, y, score in ocr_texts(np.asarray(img))]


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
        """取标签正下方一行的百分比中离标签 x 最近的那个（x 偏移随比例变化，
        如 被雇佣者 75% 会偏左较多，固定容差会漏判）。"""
        lx, ly, _ = label
        best: tuple[int, int] | None = None
        for value, x, y in pcts:
            if not (0 < y - ly <= 120):
                continue
            d = abs(x - lx)
            if best is None or d < best[0]:
                best = (d, value)
        return best[1] if best else None

    e_pct = pct_below(employer)
    d_pct = pct_below(employed)
    if e_pct is None or d_pct is None:
        return None
    if e_pct <= max_employer and d_pct >= min_employed:
        x = (employer[0] + employed[0]) // 2
        return x, employer[1], min(employer[2], employed[2])
    return None
