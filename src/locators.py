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

from .ocr import find_all_text, find_text, ocr_fullscreen, ocr_texts
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

# 区域 bounds 缓存：entry 标了 'cache': True 时，see_bounds() 第一次命中后
# 记住 (x1, y1, x2, y2)，之后直接复用（只适合位置固定的裁剪区域，如 status_region）。
_bounds_cache: dict[str, tuple[int, int, int, int]] = {}

def _ocr_texts_cached(
    screen: np.ndarray, region: tuple[int, int, int, int] | None = None
) -> list[tuple[str, int, int, float]]:
    """对 screen 的指定区域 OCR；同一 screen 同一区域直接复用结果。

    整屏（region=None）走 ocr_fullscreen：先等比缩放到接近 720x1280 再识别，
    坐标已还原回原图；裁剪区域直接用原图（调用方已自行缩放）。
    """
    global _ocr_cache
    if _ocr_cache is not None and _ocr_cache[0] is screen and _ocr_cache[1] == region:
        return _ocr_cache[2]
    if region is None:
        results = ocr_fullscreen(screen)
    else:
        x1, y1, x2, y2 = region
        results = ocr_texts(screen[y1:y2, x1:x2])
    _ocr_cache = (screen, region, results)
    return results

def ocr_screen(screen: np.ndarray) -> list[tuple[str, int, int, float]]:
    """整屏 OCR（同一 screen 复用结果）；给 see() 之外的自定义解析用。"""
    return _ocr_texts_cached(screen)

# 学习/工作选择框共用前缀：三条 xpath 仅最后一段 FrameLayout 序号不同。
# 锚定"嵌套双层 RecyclerView"（选课面板里的卡片轮播），真机验证命中；
# 之前从 ckj 出发的绝对路径层级深、随页面结构漂移，容易整链失效。
# 选课/选工作三栏容器：纯子节点步进（中间不再用 // 后代搜索），u2 引擎解析更快更稳。
# 外层 RecyclerView(首命中) -> FL[1] -> FL[1] -> 内层 RecyclerView[1] -> FL[1]（课程卡容器）
SELECT_BOX_XPATH = (
    '//androidx.recyclerview.widget.RecyclerView'
    '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
    '/androidx.recyclerview.widget.RecyclerView[1]'
    '/android.widget.FrameLayout[1]'
)

LOCATORS: dict[str, dict] = {
    # 主页面标志：金币胶囊（仅用于检测是否在主页面，不要点它——点出门用
    # leave_home）。只有自己主页面有这个元素，好友宠物页没有
    # （之前用"宠物状态"容器会被好友页误判成主页面）
    'main_sign': {
        'xpath': ['//*[@content-desc="金币胶囊"]']
    },
    # 主页面"出门"按钮（点击用）：xpath 优先，OCR 文字 + 参考坐标兜底
    'leave_home': {
        'cache': True,
        'xpath': ['//*[@content-desc="出门"]'],
        'rel': (359, 1103),
    },
    # 宠物状态面板展开/收起按钮（点击用）：xpath 优先，参考坐标兜底
    'pet_status': {
        'xpath': ['//*[@content-desc="宠物状态"]'],
        'rel': (520, 120),
    },
    # 左上返回箭头（位置固定，cache 命中一次后直接复用坐标）
    'back': {
        'cache': True,
        'xpath': [
            '//*[@content-desc="返回"]',
            '//*[@content-desc="职业树"]/preceding-sibling::android.widget.FrameLayout[2]',
            '//*[@content-desc="history_back"]',
        ],
    },
    'quit': {'xpath': ['//*[@content-desc="返回"]']},

    # 职业升级弹窗（出门页新弹窗）：点"查看"进职业树，再用系统返回键关闭
    'career_upgrade': {'ocr': ['你将进阶成为', '恭喜！']},
    'career_upgrade_view': {'ocr': ['查看']},
    # 职业树页面（查看后进入）：无原生返回键，靠系统返回键逐层退出
    'career_tree': {'ocr': ['职业树']},

    # 轮播选择框：大容器 bounds 按 2:2:1 分割成左/中/右三个可见槽位中心。
    # 容器 xpath 命中一次后 cache bounds，select_box_N 由它推导（免各自 dump）
    'select_box_container': {
        'cache': True,
        # 打工/学园两套层级（差一个 FL[2]/FL[1]），哪个命中用哪个；
        # 两者都指向同一容器 [65,924][1015,1307]，2:2:1 分割出三槽中心
        'xpath': [
            '//*[@resource-id="com.tencent.mobileqq:id/ckj"]/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]/android.widget.FrameLayout[2]/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]/android.widget.FrameLayout[4]/androidx.recyclerview.widget.RecyclerView[1]/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]',
            '//*[@resource-id="com.tencent.mobileqq:id/ckj"]/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]/android.widget.FrameLayout[4]/androidx.recyclerview.widget.RecyclerView[1]/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]',
        ],
    },
    # 2:2:1 分割：左 2/5 中心=1/5 宽，中 2/5 中心=3/5 宽，右 1/5 中心=9/10 宽
    'select_box_1': {'from_bounds': 'select_box_container', 'split': (1, 5)},
    'select_box_2': {'from_bounds': 'select_box_container', 'split': (3, 5)},
    'select_box_3': {'from_bounds': 'select_box_container', 'split': (9, 10)},

    # ---- 学习 ----
    'school': {
        'xpath': ['//*[@content-desc="map_blank"]/android.widget.FrameLayout[2]/android.widget.FrameLayout[1]']
               ,'ocr': ['宠物学园']},
    'school_start': {
        'xpath': ['//*[@content-desc="去上课"]/android.widget.FrameLayout[1]']
               ,'ocr': ['去上课']},
    'school_in': {'ocr': ['正在学习']},
    'school_end': {'xpath': ['//*[@content-desc="分享"]/android.widget.FrameLayout[1]']},
    # 毕业标志：当前学园毕业后学校面板没有"去上课"，而是"去找同学玩"；
    # 此时点"关闭"再点两次 back 回主页面，重新进学校选下一阶段课程
    'school_graduated': {'xpath': ['//*[@content-desc="去找同学玩"]']},
    'school_graduate_close': {'xpath': ['//*[@content-desc="关闭"]']},
    # 学习等待期间的"鼓励宠物"按钮（点击提升心情/互动收益，wait_end(encourage=True) 用）。
    # xpath 优先：复用 wait_end 已有的控件树快照，避免 u2 选择器再实时查一次
    'encourage_pet': {'xpath': ['//*[@content-desc="鼓励宠物"]'],
                      'u2': [{'description': '鼓励宠物'}]},

    # ---- 打工 ----
    'town': {'xpath': ['//*[@content-desc="map_blank"]/android.widget.FrameLayout[4]/android.widget.FrameLayout[1]']
               ,'ocr': ['职业小镇']},
    'work_start': {'xpath': ['//*[@content-desc="去打工"]']},
    # work_start 被"去照顾一下"弹窗挡住时：点它进护理，一键护理+back 后回工作面板（work.py _recover_work_start）
    'go_care': {'xpath': ['//*[@content-desc="去照顾一下"]']},
    'work_in': {'ocr': ['正在打工']},
    'work_end': {'xpath': ['//*[@content-desc="分享"]']},
    'work_outworker': {
        'cache': True,
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
    # 雇佣按钮：OCR 文字"雇佣"。面板标题"宠友雇佣加成排行榜（实时刷新）"也含"雇佣"
    # 但在左侧；work._find_employ_button 按 x>=屏宽一半排除标题后取右侧最上面一个。
    # 整屏 OCR 约 0.5s，比原深路径 xpath + dump 控件树（4s+）快得多。
    'employ': {'ocr': ['雇佣']},

    # ---- 冒险 ----
    'adventure': {'xpath': ['//*[@content-desc="map_blank"]/android.widget.FrameLayout[3]/android.widget.FrameLayout[1]']
               ,'ocr': ['冒险']},
    'adventure_start': {'xpath': ['//*[@content-desc="开始"]/android.widget.FrameLayout[1]']},
    'adventure_in': {'ocr': ['正在冒险', '冒险中']},
    'adventure_end': {'xpath': ['//*[@content-desc="分享"]/android.widget.FrameLayout[1]']},
    # 冒险详情框区域：开始冒险后检测"天色不对"用（see_bounds 取范围裁剪 OCR）；
    # 命中后按区域内"召回"文字的坐标点击，再点"确认召回"，计入一次冒险
    'adventure_detail': {
        'xpath': ['//*[@resource-id="com.tencent.mobileqq:id/ckj"]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[4]'],
    },
    'adventure_recall_confirm': {'xpath': ['//*[@content-desc="确认召回"]']},

    # ---- 被雇佣 ----
    # 整屏 OCR 关键词检测；不能加"雇佣规则"——它是按钮，
    # wait_employed_back 防休眠会点击命中点，点中会打开规则页；
    # "被雇佣中"标题和"剩余"标签都只是文字，点击安全。
    'employed_in': {'ocr': ['被雇佣中', '雇佣中']},
    # 召回标志不在注册表：分成比例要解析具体数值（雇佣者<=25% 且被雇佣者>=75%
    # 才命中，方向不能反），见 scenario.see_employed_sign / ocr.parse_employed_ratio
    # 召回按钮：OCR 定位——wait_employed_back 里 see_employed_sign 已对同一 screen 做整屏 OCR
    # （_ocr_texts_cached 缓存），这里直接复用，无需额外 dump/识别
    'employed_come_back': {'ocr': ['现在召回', '召回']},
    'employed_come_back_confirm': {
        'ocr': ['确认', '确定'], 'u2': [{'text': '确认'}, {'text': '确定'}],
    },
    'employed_end': {'xpath': ['//*[@content-desc="分享"]/android.widget.FrameLayout[1]']},

    # ---- 踩踩（访问好友） ----
    'visit_friends': {'xpath': ['//*[@content-desc="好友"]']},
    # 好友面板里每个好友行一个"访问"（自绘页面，无 clickable，按坐标点）；
    # see() 取第一个命中 = 最上方好友
    'visit': {'xpath': ['//*[@content-desc="访问"]', '//*[@content-desc="回访"]']},
    # 已踩标志：好友宠物页今天已踩过（踩踩按钮变成"已踩"），
    # 踩踩前检测到就跳过该好友直接切换下一个，不重复计数
    'visit_stepped': {'u2': [{'description': '已踩'}]},
    'visit_step': {
        'u2': [{'description': '踩踩'}],
    },
    # 好友列表项（content-desc 形如 "好友 <昵称>"，注意带空格前缀，
    # 和入口按钮"好友"区分）；切换逻辑见 visit.py（累积名单、按顺序切换）
    'visit_friend_item': {'xpath': ['//*[starts-with(@content-desc, "好友 ")]']},

    # ---- PK（好友对战） ----
    'pk': {'xpath': ['//*[@content-desc="PK"]']},
    'pk_in': {'ocr': ['正在PK']},  # 进行中状态：全屏 OCR 关键词（PK 页 canvas 自绘）
    'pk_start': {'xpath': ['//*[@content-desc="开始"]']},
    'pk_end': {'xpath': ['//*[@content-desc="分享"]']},
    'pk_again': {'xpath': ['//*[@content-desc="再来一局"]']},

    # ---- 照顾 ----
    # 宠物状态面板区域：care.read_status 只裁这块做 OCR（整屏/半屏太慢）；
    # 位置固定，cache 命中一次后 see_bounds() 直接复用 bounds
    'status_region': {
        'cache': True,
        'xpath': ['//*[@resource-id="com.tencent.mobileqq:id/ckj"]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[2]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[5]'],
    },
    'feed': {'xpath': ['//*[@content-desc="feed"]/android.widget.FrameLayout[1]']},
    # 一键护理按钮（content-desc 以 one_click_care 开头，后缀不固定，前缀匹配）；
    # 护理方式配置为"一键护理"时，照顾流程只点它——不读状态、不手动喂食/洗澡
    'one_click_care': {'xpath': ['//*[starts-with(@content-desc, "one_click_care")]']},
    # one_click_care 的父级"照顾区域"：踩踩时 OCR 判断是否有"exp"经验值（经验日常）
    'care_region': {'xpath': ['//*[starts-with(@content-desc, "one_click_care")]/parent::*']},
    # 一键护理后的支付确认按钮：点击护理按钮后若弹出"支付并护理"，必须点掉才完成护理
    'one_click_pay': {'xpath': ['//*[@content-desc="支付并护理"]']},
    'feed_10': {
        'xpath': ['//androidx.recyclerview.widget.RecyclerView'
                  '/android.widget.FrameLayout[1]/android.widget.FrameLayout[1]'],
    },
    'shower': {'xpath': ['//*[@content-desc="洗澡"]']},
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
    base = entry.get('from_bounds')
    if base:
        # 由另一个定位的 bounds 推导中心（如 select_box_N 从容器 2:2:1 分割）
        bounds = see_bounds(dev, base, source)
        if not bounds:
            return None
        x1, y1, x2, y2 = bounds
        num, den = entry['split']
        w = x2 - x1
        return x1 + round(w * num / den), (y1 + y2) // 2, 1.0
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

def locate_cached(name: str) -> tuple[int, int, float] | None:
    """只查命中缓存坐标，不触发任何识别/dump；未缓存返回 None。

    给需要"多个定位共享一次控件树 dump"的调用方判断缓存是否齐了用。
    """
    return _locate_cache.get(name)


def see_bounds(dev: U2Device, name: str, source=None) -> tuple[int, int, int, int] | None:
    """定位 name 的元素范围，返回 (x1, y1, x2, y2)，未命中返回 None。

    只支持 xpath 定位；用于位置固定的裁剪区域（如宠物状态面板 status_region）。
    entry 标了 'cache': True 时，第一次命中后 bounds 记入 _bounds_cache，
    之后直接返回缓存，不再查控件树。
    """
    entry = LOCATORS.get(name)
    if entry is None:
        raise KeyError(f'未定义的定位名: {name!r}（请在 src/locators.py 的 LOCATORS 中登记）')
    if entry.get('cache'):
        hit = _bounds_cache.get(name)
        if hit:
            return hit
    for path in entry.get('xpath', []):
        bounds = dev.find_xpath_bounds(path, source)
        if bounds:
            if entry.get('cache'):
                _bounds_cache[name] = bounds
            return bounds
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
