"""RapidOCR 文字识别封装：整屏识别文字及中心坐标。

引擎懒加载（首次调用时加载模型，约需几秒）。
输入为 numpy RGB 图（u2dev.U2Device.screenshot 的输出）。
整屏识别统一走 ocr_fullscreen()：先保持长宽比缩放到接近 720x1280 级别
再 OCR（高分辨率截图识别慢，缩放后速度与设备分辨率无关），
返回坐标已还原回原图像素，可直接用于点击。

引擎：rapidocr>=3.9（onnxruntime，PP-OCRv6 tiny 模型；实测比 v5 mobile
快约 2.4 倍且关键场景准确率持平，CJK+数字间可能插入空格，
见 find_text 的空格归一化）。
模型目录 APP_ROOT/runs/models/rapidocr：只使用 PP-OCRv6 tiny（det/rec）+ v4 cls，
不打/不初始化 rapidocr 包自带的 v4/v5 模型；打包随附的模型首次运行时复制出来，
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

# PP-OCRv6 tiny（det/rec）与 v4 cls 模型：URL 与 SHA256 取自 rapidocr 3.9.2 default_models.yaml。
# 文件名必须与 rapidocr 自动下载时的落盘名一致（URL basename），否则引擎会重复下载。
V6_TINY_MODELS = [
    ('PP-OCRv6_det_tiny.onnx',
     'https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv6/det/PP-OCRv6_det_tiny.onnx',
     'f42c0fbd294d95eac1a550e131b277dac97462c8025fa4b6c3cec1b7894bd3d5'),
    ('PP-OCRv6_rec_tiny.onnx',
     'https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv6/rec/PP-OCRv6_rec_tiny.onnx',
     'e16e242de5937ad92609223f19bc2aff3727ee40b095f996907c24749bad251b'),
    ('ch_ppocr_mobile_v2.0_cls_mobile.onnx',
     'https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv4/cls/ch_ppocr_mobile_v2.0_cls_mobile.onnx',
     'e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c'),
]

# 旧 v5 模型文件名：切到 v6 后不再使用，ensure_models() 里顺手清掉（cls 两版共用保留）
_STALE_V5_FILES = ('ch_PP-OCRv5_det_mobile.onnx', 'ch_PP-OCRv5_rec_mobile.onnx',
                   'ppocrv5_dict.txt')


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _ensure_models() -> None:
    """保证模型目录存在，并把打包随附的模型复制出来（避免首次联网下载）。

    只处理 PP-OCRv6 tiny（RESOURCE_ROOT/models/rapidocr）；不复制 rapidocr 包自带的 v4/v5。
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


def ensure_models() -> bool:
    """确保模型目录就绪，并尝试补齐 PP-OCRv6 tiny 模型（缺失时联网下载）。

    顺带清理已废弃的 v5 模型文件。返回是否已具备完整模型；下载失败只记日志
    并返回 False（调用方抛错，不再回退 v4/v5）。
    """
    MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    _ensure_models()
    for name, url, expected in V6_TINY_MODELS:
        dst = MODEL_ROOT / name
        if dst.exists() and _sha256(dst) == expected:
            continue
        log(f'下载 OCR 模型 {name} ...')
        try:
            _download_model(name, url, expected)
        except Exception as e:
            log(f'下载 OCR 模型 {name} 失败: {e}，将无法使用 OCR')
            return False
    for stale in _STALE_V5_FILES:
        (MODEL_ROOT / stale).unlink(missing_ok=True)
    return True


def get_engine():
    """懒加载 RapidOCR 引擎（全局单例）。"""
    global _engine
    if _engine is None:
        from rapidocr import EngineType, LangDet, LangRec, ModelType, OCRVersion, RapidOCR

        if not ensure_models():
            # 没有 v6 tiny 且下载失败：明确报错，不再静默回退 v4/v5
            raise RuntimeError(
                'PP-OCRv6 tiny 模型缺失且下载失败，OCR 不可用'
                f'（模型目录: {MODEL_ROOT}，可运行 python tools/fetch_ocr_models.py）')
        _engine = RapidOCR(params={
            'Global.model_root_dir': str(MODEL_ROOT),
            'Global.log_level': 'WARN',  # 抑制引擎 INFO 噪音
            'Det.engine_type': EngineType.ONNXRUNTIME,
            'Det.lang_type': LangDet.CH,
            'Det.model_type': ModelType.TINY,
            'Det.ocr_version': OCRVersion.PPOCRV6,
            'Rec.engine_type': EngineType.ONNXRUNTIME,
            'Rec.lang_type': LangRec.CH,
            'Rec.model_type': ModelType.TINY,
            'Rec.ocr_version': OCRVersion.PPOCRV6,
            # PP-OCRv6 不自带方向分类器，复用 v4 mobile cls
            'Cls.engine_type': EngineType.ONNXRUNTIME,
            'Cls.lang_type': 'ch',
            'Cls.model_type': ModelType.MOBILE,
            'Cls.ocr_version': OCRVersion.PPOCRV4,
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
    results: list[tuple[str, int, int, float]], target: str, strip_space: bool = True
) -> tuple[int, int, float] | None:
    """在 OCR 结果中找包含 target 的文字，返回 (中心x, 中心y, 置信度) 或 None。

    单行碎片兜底：大字号标题常被 OCR 拆成单字/错字碎片
    （如"被雇佣中"识别成 被/皮雇/佣/中），逐条匹配必然落空，
    把同一行的碎片按 x 拼接后再找 target。

    空格归一化（strip_space=True，默认开）：PP-OCRv6 模型常在中文与数字之间
    插入空格（如"体力 80"/"心情 100"），去空格后匹配避免子串落空；
    "体力 80" 这类在同一文本块内的空格也能命中。
    """
    def norm(t: str) -> str:
        return t.replace(' ', '') if strip_space else t

    tgt = norm(target)
    for text, x, y, score in results:
        if tgt in norm(text):
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
        merged = ''.join(norm(text) for text, *_ in line)
        idx = merged.find(tgt)
        if idx < 0:
            continue
        # 命中片段覆盖的碎片：x 取首尾碎片中心的中点，置信度取最低
        pos, first, last = 0, 0, len(line) - 1
        for i, (text, *_rest) in enumerate(line):
            t = norm(text)
            if pos <= idx < pos + len(t):
                first = i
            if pos < idx + len(tgt) <= pos + len(t):
                last = i
            pos += len(t)
        x = (line[first][1] + line[last][1]) // 2
        score = min(r[3] for r in line[first:last + 1])
        return x, line[0][2], score
    return None


def find_all_text(
    results: list[tuple[str, int, int, float]], target: str, strip_space: bool = True
) -> list[tuple[int, int, float]]:
    """在 OCR 结果中找所有包含 target 的文字（同一屏幕可能有多个相同按钮）。

    返回 [(中心x, 中心y, 置信度)]，按从上到下、从左到右排序。
    与 find_text 一样默认做空格归一化。
    """
    def norm(t: str) -> str:
        return t.replace(' ', '') if strip_space else t

    tgt = norm(target)
    matches = [(x, y, score) for text, x, y, score in results if tgt in norm(text)]
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


def parse_panel_location(
    results: list[tuple[str, int, int, float]],
) -> str | None:
    """在 OCR 结果中解析当前面板的地点名。

    取"力量/智力/魅力"属性面板所在行正下方第一串字符——那是当前打工地点的名字，
    用于确认当前工作面板是不是配置的打工地点；同一行内多个 OCR 碎片时取
    离屏幕中心 x 最近的一个（地点名在面板上横向居中，右侧装饰等误识别
    碎片通常偏在一边）。小镇地图页下方第一行是地点卡片名（通常与配置不符），
    据此也能区分"在小镇地图 vs 在打工面板"。
    跳过不含中文的 OCR 误识别碎片（如属性面板下方的装饰元素）
    避免把这类短串当地点名导致打工面板确认失败。
    返回去空格后的文字；属性面板没识别到返回 None。
    """
    def norm(t: str) -> str:
        return t.replace(' ', '')

    def has_cjk(t: str, min_chars: int = 2) -> bool:
        return sum('\u4e00' <= ch <= '\u9fff' for ch in t) >= min_chars

    stats_y = None
    for text, x, y, score in results:
        t = norm(text)
        if '力量' in t or '智力' in t or '魅力' in t:
            stats_y = y if stats_y is None else max(stats_y, y)
    if stats_y is None:
        return None

    candidates: list[tuple[str, int, int, float]] = []
    for text, x, y, score in results:
        t = norm(text)
        if not t or y <= stats_y or not has_cjk(t):
            continue
        candidates.append((t, x, y, score))
    if not candidates:
        return None

    candidates.sort(key=lambda c: (c[2], c[1]))
    center_x = max(x for _, x, _, _ in results) / 2
    first_y = candidates[0][2]
    best = min(
        (c for c in candidates if abs(c[2] - first_y) <= 30),
        key=lambda c: (abs(c[1] - center_x), c[1]),
    )
    return best[0]


def parse_employed_remaining(
    results: list[tuple[str, int, int, float]],
) -> tuple[int, int, int, float] | None:
    """在 OCR 结果中解析被雇佣面板的剩余时间行"剩余 00:44:00"。

    返回 (剩余秒数, 中心x, 中心y, 置信度)；解析不到（面板未加载/格式变化）
    返回 None，由调用方回退到只按分成比例等（不误召回）。
    时间与"剩余"标签可能在同一个文本块，也可能是同行相邻的两个块，
    均做空格归一化后匹配 HH:MM:SS；"剩余"标签本身没识别到时，若整屏恰好只有
    一个 HH:MM:SS 时间块（被雇佣面板只有一个倒计时）也回退命中，多个则返回 None 防误配。
    """
    time_re = re.compile(r'(\d{1,2}):(\d{2}):(\d{2})')

    def norm(t: str) -> str:
        return t.replace(' ', '')

    # 收集所有时间块
    times: list[tuple[int, int, int, float, int]] = []
    for text, x, y, score in results:
        m = time_re.search(norm(text))
        if m:
            h, mm, s = (int(g) for g in m.groups())
            times.append((h * 3600 + mm * 60 + s, x, y, score, m.start()))
    if not times:
        return None

    # "剩余"标签：同一块带时间就直接命中
    label = None
    for text, x, y, score in results:
        t = norm(text)
        if '剩余' not in t:
            continue
        m = time_re.search(t)
        if m:
            h, mm, s = (int(g) for g in m.groups())
            return h * 3600 + mm * 60 + s, x, y, score
        label = (x, y, score)
        break
    if label is None:
        # "剩余"标签没识别到：整屏恰好只有一个 HH:MM:SS 时间块时回退用它
        # （被雇佣面板只有一个倒计时；多个说明还有其他时间文本，避免误配返回 None）
        if len(times) == 1:
            secs, x, y, score, _pos = times[0]
            return secs, x, y, score
        return None

    # 标签与时间分块：取同一行、标签右侧最近的时间块
    lx, ly, _ = label
    best: tuple[int, int, int, int, float] | None = None
    for secs, x, y, score, _pos in times:
        if not (-30 <= y - ly <= 30) or x < lx - 10:
            continue
        d = x - lx
        if best is None or d < best[0]:
            best = (d, secs, x, y, score)
    if best is None:
        return None
    _, secs, x, y, score = best
    return secs, x, y, score
