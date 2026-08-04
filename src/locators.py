"""UI 定位注册表：u2 控件选择器 + OCR 文字 + 相对坐标兜底（分辨率无关）。

取代原 templates/ 模板匹配。每个逻辑名（沿用原模板名，场景代码不用改）映射到
若干定位方式，按顺序尝试：

- xpath: u2 XPath 路径列表（d.xpath），命中返回元素中心，score 记 1.0
- xpath_ocr: [{'xpath': 路径, 'ocr': [候选文字]}]，先用 XPath 取元素范围，
       只对该范围裁剪的小图做 OCR（整屏 OCR 慢，状态文字类定位优先用这个），
       命中返回文字中心的屏幕坐标与置信度；xpath 未命中或区域内没有目标文字
       则继续尝试后续方式
- u2:  uiautomator2 控件选择器列表（原生控件树可及的范围，如系统/QQ 原生弹窗），
       命中返回控件中心，score 记 1.0
- ocr: 候选文字列表，对整屏截图做 RapidOCR，任一命中即返回文字中心与置信度
       （QQ 宠物大部分界面是 canvas 自绘渲染，游戏内按钮主要靠这个）
- rel: 720x1280 参考坐标，按当前分辨率等比换算；作为"必然命中"的点击兜底，
       只给无文字、纯图形且位置固定的元素用（如左上 back 箭头），
       不要给需要"检测是否存在"的场景用

OCR 文案是按界面推断的经验值，换游戏版本后如识别不到，
用 scenarios/runner.py --test <场景>.<方法> 真机逐屏校准本表即可。
"""
from __future__ import annotations

import numpy as np

from .ocr import find_all_text, find_text, ocr_texts
from .u2dev import U2Device

# OCR 命中的最低置信度
OCR_MIN_SCORE = 0.5

# 同一张 screen 连续查多个 OCR 定位时复用识别结果（一轮检测共享快照，
# 期间屏幕内容不会变；换下一张 screen 对象时自动失效）。强引用是刻意的，
# 防止只用 id(screen) 时对象释放后地址复用导致误命中缓存。
_ocr_cache: tuple[np.ndarray, tuple[int, int, int, int] | None,
                 list[tuple[str, int, int, float]]] | None = None

# 命中缓存：entry 标了 'cache': True 的定位，第一次命中后记住坐标，
# 之后 see() 直接返回缓存点、不再走任何识别（只适合位置固定的元素，如 back）。
_locate_cache: dict[str, tuple[int, int, float]] = {}


def _ocr_texts_cached(
    screen: np.ndarray, region: tuple[int, int, int, int] | None = None
) -> list[tuple[str, int, int, float]]:
    """对 screen 的指定区域 OCR；同一 screen 同一区域直接复用结果。"""
    global _ocr_cache
    if _ocr_cache is not None and _ocr_cache[0] is screen and _ocr_cache[1] == region:
        return _ocr_cache[2]
    if region is None:
        crop = screen
    else:
        x1, y1, x2, y2 = region
        crop = screen[y1:y2, x1:x2]
    results = ocr_texts(crop)
    _ocr_cache = (screen, region, results)
    return results


def ocr_screen(screen: np.ndarray) -> list[tuple[str, int, int, float]]:
    """整屏 OCR（同一 screen 复用结果）；给 see() 之外的自定义解析用。"""
    return _ocr_texts_cached(screen)


# 学习/工作选择框共用前缀：三条 xpath 仅最后一段 FrameLayout 序号不同
SELECT_BOX_XPATH = (
    '//*[@resource-id="com.tencent.mobileqq:id/ckj"]'
    '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
    '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
    '/android.widget.FrameLayout[2]/android.widget.FrameLayout[1]'
    '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
    '/android.widget.FrameLayout[1]/android.widget.FrameLayout[4]'
    '/androidx.recyclerview.widget.RecyclerView[1]'
    '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
    '/androidx.recyclerview.widget.RecyclerView[1]'
    '/android.widget.FrameLayout[1]'
)


LOCATORS: dict[str, dict] = {
    # 主页面标志：控件树里的"宠物状态"容器（仅用于检测是否在主页面，
    # 不要点它——点出门用 leave_home）；xpath 失效时回退 OCR "出门"
    'main_sign': {
        'xpath': ['//*[@content-desc="宠物状态"]/android.widget.FrameLayout[1]']
    },
    # 主页面"出门"按钮（点击用）：xpath 优先，OCR 文字 + 参考坐标兜底
    'leave_home': {
        'xpath': ['//*[@resource-id="com.tencent.mobileqq:id/ckj"]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[2]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[3]'
                  '/android.widget.FrameLayout[3]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[3]/android.widget.Button[1]'
                  '/android.widget.FrameLayout[1]'],
        'rel': (359, 1103),
    },
    # 宠物状态面板展开/收起按钮（点击用）：xpath 优先，参考坐标兜底
    'pet_status': {
        'xpath': ['//*[@content-desc="宠物状态"]/android.widget.FrameLayout[1]'],
        'rel': (520, 120),
    },
    # 左上返回箭头（位置固定，cache 命中一次后直接复用坐标）
    'back': {
        'cache': True,
        'xpath': [
            '//*[@content-desc="返回"]/android.widget.FrameLayout[1]/android.widget.ImageView[1]',
            '//*[@content-desc="返回"]/android.widget.ImageView[1]',
            '//*[@resource-id="com.tencent.mobileqq:id/ckj"]'
            '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
            '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
            '/android.widget.FrameLayout[2]/android.widget.FrameLayout[1]'
            '/android.widget.FrameLayout[1]/android.widget.ImageView[1]',
            '//*[@resource-id="com.tencent.mobileqq:id/ckj"]'
            '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
            '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
            '/android.widget.FrameLayout[3]/android.widget.FrameLayout[1]'
            '/android.widget.FrameLayout[1]/android.widget.ImageView[1]',
        ],
    },
    'quit': {'xpath': ['//*[@content-desc="返回"]/android.widget.FrameLayout[1]/android.widget.ImageView[1]','//*[@content-desc="返回"]/android.widget.ImageView[1]']},

    # ---- 学习/工作选择框（选课、选工作共用的三栏列表） ----
    # 三条 xpath 共用 SELECT_BOX_XPATH 前缀，仅最后一段序号不同；归位拖动和点选都用它们。
    # 注意：xpath 只匹配学习页（RecyclerView 结构）；打工页卡片是 H5 匿名 node，
    # xpath 不命中时 see() 返回 None，reset_select_boxes 会抛"未定位到选择框"。
    'select_box_1': {
        'xpath': [SELECT_BOX_XPATH + '/android.widget.FrameLayout[1]'],
    },
    'select_box_2': {
        'xpath': [SELECT_BOX_XPATH + '/android.widget.FrameLayout[2]'],
    },
    'select_box_3': {
        'xpath': [SELECT_BOX_XPATH + '/android.widget.FrameLayout[3]'],
    },

    # ---- 学习 ----
    'school': {'xpath': ['//*[@content-desc="map_blank"]/android.widget.FrameLayout[2]/android.widget.FrameLayout[1]']
               ,'ocr': ['宠物学园']},
    'school_start': {'xpath': ['//*[@content-desc="去上课"]/android.widget.FrameLayout[1]']
               ,'ocr': ['去上课']},
    'school_in': {'ocr': ['正在学习']},
    'school_end': {'xpath': ['//*[@content-desc="分享"]/android.widget.FrameLayout[1]']},

    # ---- 打工 ----
    'town': {'xpath': ['//*[@content-desc="map_blank"]/android.widget.FrameLayout[4]/android.widget.FrameLayout[1]']
               ,'ocr': ['职业小镇']},
    'work_start': {'xpath': ['//*[@content-desc="去打工"]/android.widget.FrameLayout[1]']},
    'work_in': {'ocr': ['正在工作']},
    'work_end': {'xpath': ['//*[@content-desc="分享"]/android.widget.FrameLayout[1]']},
    'work_outworker': {
        'xpath': ['//*[@resource-id="com.tencent.mobileqq:id/ckj"]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[2]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[4]'
                  '/androidx.recyclerview.widget.RecyclerView[1]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[3]'
                  '/android.widget.FrameLayout[4]'],
    },
    'work_employ_close': {
        'xpath': ['//*[@resource-id="com.tencent.mobileqq:id/ckj"]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[2]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[2]'
                  '/androidx.recyclerview.widget.RecyclerView[1]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[2]'],
    },
    'employ': {
        'xpath': ['//*[@resource-id="com.tencent.mobileqq:id/ckj"]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[2]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[2]'
                  '/androidx.recyclerview.widget.RecyclerView[1]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
                  '/androidx.recyclerview.widget.RecyclerView[1]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
                  '/android.widget.Button[1]'],
    },

    # ---- 冒险 ----
    'adventure': {'xpath': ['//*[@content-desc="map_blank"]/android.widget.FrameLayout[3]/android.widget.FrameLayout[1]']
               ,'ocr': ['冒险']},
    'adventure_start': {'xpath': ['//*[@content-desc="开始"]/android.widget.FrameLayout[1]']},
    'adventure_in': {'ocr': ['正在冒险', '冒险中']},
    'adventure_end': {'xpath': ['//*[@content-desc="分享"]/android.widget.FrameLayout[1]']},

    # ---- 被雇佣 ----
    # 整屏 OCR 关键词检测；不能加"雇佣规则"——它是按钮，
    # wait_employed_back 防休眠会点击命中点，点中会打开规则页；
    # "被雇佣中"标题和"剩余"标签都只是文字，点击安全。
    'employed_in': {'ocr': ['被雇佣中', '雇佣中', '剩余']},
    # 召回标志不在注册表：分成比例要解析具体数值（雇佣者<=25% 且被雇佣者>=75%
    # 才命中，方向不能反），见 scenario.see_employed_sign / ocr.parse_employed_ratio
    'employed_come_back': {'ocr': ['现在召回', '召回']},
    'employed_come_back_confirm': {
        'ocr': ['确认', '确定'], 'u2': [{'text': '确认'}, {'text': '确定'}],
    },
    'employed_end': {'xpath': ['//*[@content-desc="分享"]/android.widget.FrameLayout[1]']},

    # ---- 照顾 ----
    'feed': {'xpath': ['//*[@content-desc="feed"]/android.widget.FrameLayout[1]']},
    'feed_10': {
        'xpath': ['//androidx.recyclerview.widget.RecyclerView'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'],
    },
    'shower': {'xpath': ['//*[@content-desc="shower"]/android.widget.FrameLayout[1]']},
    'shower_10': {
        'xpath': ['//androidx.recyclerview.widget.RecyclerView'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'],
    },
}


def see(
    dev: U2Device, name: str, screen: np.ndarray | None = None, source=None
) -> tuple[int, int, float] | None:
    """定位名为 name 的 UI 元素，命中返回 (中心x, 中心y, score)，否则 None。

    screen: 已截好的屏幕（numpy RGB），传 None 则按需现截（仅 OCR 方式需要）。
    source: dev.hierarchy() 的控件树快照；一轮检测多个元素时共享，
        避免每个 xpath 都重新 dump 全树（dump 一次约 1-3 秒）。
    entry 标了 'cache': True 时，第一次命中后坐标记入 _locate_cache，
    之后直接返回缓存点，不再做任何识别。
    """
    entry = LOCATORS.get(name)
    if entry is None:
        raise KeyError(f'未定义的定位名: {name!r}（请在 src/locators.py 的 LOCATORS 中登记）')

    if entry.get('cache'):
        hit = _locate_cache.get(name)
        if hit:
            return hit
        result = _locate(dev, entry, screen, source)
        if result:
            _locate_cache[name] = result
        return result
    return _locate(dev, entry, screen, source)


def _locate(
    dev: U2Device, entry: dict, screen: np.ndarray | None = None, source=None
) -> tuple[int, int, float] | None:
    """按注册表 entry 的定位方式依次尝试（不含缓存逻辑）。"""
    for path in entry.get('xpath', []):
        hit = dev.find_xpath(path, source)
        if hit:
            return hit[0], hit[1], 1.0

    for spec in entry.get('xpath_ocr', []):
        bounds = dev.find_xpath_bounds(spec['xpath'], source)
        if not bounds:
            continue
        if screen is None:
            screen = dev.screenshot()
        x1, y1, x2, y2 = bounds
        results = _ocr_texts_cached(screen, bounds)
        for target in spec['ocr']:
            hit = find_text(results, target)
            if hit and hit[2] >= OCR_MIN_SCORE:
                # OCR 坐标是裁剪图内的，换算回屏幕坐标
                return x1 + hit[0], y1 + hit[1], hit[2]

    for selector in entry.get('u2', []):
        hit = dev.find_ui(selector)
        if hit:
            return hit[0], hit[1], 1.0

    if 'ocr' in entry:
        if screen is None:
            screen = dev.screenshot()
        results = _ocr_texts_cached(screen)
        for target in entry['ocr']:
            hit = find_text(results, target)
            if hit and hit[2] >= OCR_MIN_SCORE:
                return hit

    if 'rel' in entry:
        x, y = dev.rel(*entry['rel'])
        return x, y, 1.0
    return None


def see_all(
    dev: U2Device, name: str, screen: np.ndarray | None = None
) -> list[tuple[int, int, float]]:
    """定位 name 的所有命中（OCR 多点），按从上到下、从左到右排序。"""
    entry = LOCATORS.get(name)
    if entry is None:
        raise KeyError(f'未定义的定位名: {name!r}（请在 src/locators.py 的 LOCATORS 中登记）')
    if 'ocr' not in entry:
        raise ValueError(f'{name!r} 没有 ocr 定位方式，不支持多点查找')
    if screen is None:
        screen = dev.screenshot()
    results = _ocr_texts_cached(screen)
    matches: list[tuple[int, int, float]] = []
    for target in entry['ocr']:
        matches.extend(m for m in find_all_text(results, target) if m[2] >= OCR_MIN_SCORE)
    matches.sort(key=lambda m: (m[1], m[0]))
    return matches
