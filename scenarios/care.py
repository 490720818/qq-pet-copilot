"""宠物状态照顾：任务调度前检查体力/清洁，不足则喂食/洗澡。

流程（主页面操作，u2 控件/OCR 文字定位，坐标为 720x1280 参考值自动换算）：
1. 点击 pet_status（宠物状态按钮，xpath 定位）展开宠物状态
2. OCR 状态面板区域（xpath 定范围）识别 体力/清洁/心情 三个数值及 账号名称/宠物名称
3. 体力低于阈值 -> 喂食：点 feed -> 反复点 feed_10 并复测体力，直到达标；
   没有 feed_10（饼干不足）时先点"兑换食物"金币兑换 99 个（弹窗数量改 99 -> 支付）再继续
4. 清洁低于阈值 -> 洗澡：点 shower -> 按住肥皂（shower_10 控件中心）不松手
   （d.touch down/move/up）拖到 (50%, 40%)，再在 (50%, 67%) 和 (50%, 40%)
   之间来回搓洗，直到清洁达标后抬手（点位按当前分辨率百分比换算）
5. 若仍在喂食/洗澡界面（feed_10 / shower_10 可见）-> 点 back 退出
6. 点击 pet_status 收起宠物状态

运行：python scenarios/care.py   （执行一次检查/照顾）
"""

import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from src.locators import see_bounds
from src.ocr import ocr_texts
from src.progress import log
from src.scenario import CLICK_INTERVAL, DeviceScenario
from src.status_cache import clear_status_fields, update_status
from src.u2dev import REF_SIZE

# 洗澡搓洗点位：起点 = shower_10 控件中心（肥皂），其余按当前分辨率百分比换算
SCRUB_TOP_PCT = (0.5, 0.40)     # 拖动终点 / 搓洗上端点
SCRUB_BOTTOM_PCT = (0.5, 0.67)  # 搓洗下端点 / 抬手点
# 状态数值配对的像素容差：按 720 宽参考分辨率的 OCR 结果调的，
# 运行时按当前屏宽等比缩放（见 read_status）
STATUS_ROW_TOL = 40  # 名字右侧同行数字的纵向容差
STATUS_COL_TOL = 80  # 名字下方同列数字的横向容差
FEED_RESULT_WAIT = 1.5  # 喂食后等数值刷新的时间（秒）
FEED_PANEL_RETRIES = 4  # 点 feed 后等喂食面板加载（feed_10 或"兑换食物"出现）的重试次数
MAX_FEED_ATTEMPTS = 10    # 喂食最多次数，超过认为异常
EXCHANGE_FOOD_COUNT = 99  # 饼干不足时金币兑换食物的数量（弹窗输入框默认 5）
EXCHANGE_POPUP_RETRIES = 3  # 等兑换食物弹窗（数量输入框）出现的重试次数
MAX_SHOWER_ATTEMPTS = 25  # 搓洗最多回合数，超过认为异常
# 清洁连续多少回合不提升判定按压失效（minitouch 会话静默中断，touch_move 全丢，
# 肥皂停在原地）：抬手重按肥皂自愈。正常上涨约每 2 回合 +10，取 4 留余量
SCRUB_STALL_REPRESS = 4
STATUS_READ_RETRIES = 4   # 状态面板数值是异步加载的，刚展开可能只有账号/宠物名，重试读
ONE_CLICK_PAY_RETRIES = 2 # 点击一键护理后确认"支付并护理"弹窗的重试次数（每次等 1 秒）

STATUS_NAMES = ('体力', '清洁', '心情')
# 护理方式（care.method 配置）：ocr检测 = 读状态手动喂食/洗澡；一键护理 = 直接点主页面一键护理按钮
CARE_METHODS = ('ocr检测', '一键护理')
# 喂食/洗澡面板的物品库存：库存数字没有文字标签，只有图标 + 数字角标，
# 取 OCR 下半屏里离 feed_10 / shower_10 控件中心最近的数字
# （feed_10 -> 饼干 biscuit，shower_10 -> 香皂 soap，见 cache_care_items）
ITEM_ANCHOR_KEYS = {'feed_10': 'biscuit', 'shower_10': 'soap'}
# 库存角标在图标附近：以控件 bounds 各边向外扩这么多像素裁剪 OCR 区域
ITEM_CROP_MARGIN = 80


def parse_panel_info(results: list[tuple[str, int, int, float]]) -> dict:
    """从状态面板 OCR 结果解析 账号名称/宠物名称。

    面板文案没有标签：第一行是账号昵称（用户自取，任意文本），第二行是宠物名称，
    后面跟体力/清洁/心情状态。规则：按从上到下跳过状态行和纯数字行，
    前两行有效文本依次是 账号名称/宠物名称
    （名称和状态在同一文本块时截掉状态部分）。
    """
    tokens = sorted(((text.replace(' ', ''), x, y) for text, x, y, _ in results),
                    key=lambda t: (t[2], t[1]))
    info: dict[str, str] = {}
    # 好友的宠物页（面板带"加好友"按钮）不是自己的状态面板：不解析名称，
    # 防止把好友昵称当成账号写进状态缓存、污染按账号的进度目录
    if any('加好友' in text for text, *_ in results):
        return info
    keys = ('账号名称', '宠物名称')
    idx = 0
    for text, _, _ in tokens:
        if idx >= len(keys):
            break
        # 名称行里可能粘着状态（'小可爱体力85'），截出状态前的部分
        head = re.split('|'.join(STATUS_NAMES), text, maxsplit=1)[0]
        if not head or re.fullmatch(r'\d+', head):
            continue
        info[keys[idx]] = head
        idx += 1
    return info


def parse_status(results: list[tuple[str, int, int, float]], scale: float = 1.0) -> dict:
    """从状态区域 OCR 结果解析 体力/清洁/心情 数值（0-100）与 账号名称/宠物名称。

    兼容两种 OCR 输出：名字和数字在同一文本块（'体力85'），
    或被拆成两个块（'体力' + '85'，取名字右侧/下方最近的数字）。
    scale：像素容差（STATUS_ROW_TOL/STATUS_COL_TOL）的缩放系数，按 OCR 图宽换算。
    """
    tokens = [(text.replace(' ', ''), x, y) for text, x, y, _ in results]
    status: dict[str, int] = {}
    for name in STATUS_NAMES:
        for text, _, _ in tokens:
            m = re.search(re.escape(name) + r'\D{0,3}(\d{1,3})', text)
            if m:
                status[name] = int(m.group(1))
                break
        if name in status:
            continue
        anchors = [(x, y) for text, x, y in tokens if name in text]
        nums = [(int(t), x, y) for t, x, y in tokens if re.fullmatch(r'\d{1,3}', t)]
        if not anchors or not nums:
            continue
        ax, ay = anchors[0]
        # 优先取名字右侧同行的数字，其次取名字下方同列的数字
        right = [n for n in nums if n[1] > ax and abs(n[2] - ay) < STATUS_ROW_TOL * scale]
        below = [n for n in nums if n[2] > ay and abs(n[1] - ax) < STATUS_COL_TOL * scale]
        pick = right or below
        if pick:
            pick.sort(key=lambda n: (n[1] - ax) ** 2 + (n[2] - ay) ** 2)
            status[name] = pick[0][0]
    status.update(parse_panel_info(results))
    return status


def nearest_item_count(results: list[tuple[str, int, int, float]],
                       px: float, py: float) -> int | None:
    """OCR 结果里离 (px, py) 最近的纯数字（物品库存角标，兼容 'x12' / '×12'）。

    库存角标没有文字标签，不能用"名字+邻近数字"：以 feed_10 / shower_10
    控件中心为锚点，取最近的数字；没有数字返回 None。
    """
    best: tuple[float, int] | None = None
    for text, x, y, _ in results:
        m = re.fullmatch(r'[xX×]?(\d{1,4})', text.replace(' ', ''))
        if not m:
            continue
        dist = (x - px) ** 2 + (y - py) ** 2
        if best is None or dist < best[0]:
            best = (dist, int(m.group(1)))
    return best[1] if best else None


class CareScenario(DeviceScenario):
    def __init__(self, dev=None):
        super().__init__(dev)
        self.last_care_at: datetime | None = None  # 上次护理检查时间（护理间隔用）
        self.energy_threshold = self.cfg.care.energy_threshold
        self.clean_threshold = self.cfg.care.clean_threshold
        self.method = self.cfg.care.method
        if self.method not in CARE_METHODS:
            raise ValueError(
                f'config.yaml 中 care.method 配置无效: {self.method!r}，'
                f'可选: {"/".join(CARE_METHODS)}')
        log(f'护理方式: {self.method}，体力阈值: {self.energy_threshold}，'
            f'清洁阈值: {self.clean_threshold}')

    # ---- 状态识别 ----

    def read_status(self, screen=None, source=None) -> dict:
        """OCR 屏幕上半部分识别宠物状态面板，返回体力/清洁/心情/账号名称/宠物名称
        （识别不到的缺省）。状态面板在上半屏，不再走 status_region 控件定位
        （深层 xpath 在好友宠物页会误命中底部好友列表栏，且多一次控件树采集）。
        调用方已截图时传入，避免重复采集。"""
        if screen is None:
            screen = self.screen()
        region = screen[: screen.shape[0] // 2]
        results = ocr_texts(region)
        log('状态区域 OCR: '
            + (', '.join(f'{t!r}@({x},{y})' for t, x, y, _ in results) or '无'))
        # 不做放大：容差按当前屏宽相对 720 参考分辨率等比缩放
        scale = screen.shape[1] / REF_SIZE[0]
        return parse_status(results, scale)

    def read_status_ready(self, attempts: int = STATUS_READ_RETRIES) -> dict:
        """读状态面板数值：刚展开时数值可能还没加载（OCR 只有账号/宠物名），
        体力/清洁都读到才返回，否则每次重新截图重试（最后一次原样返回）。"""
        status: dict = {}
        for attempt in range(1, attempts + 1):
            screen, source = self.snapshot()
            status = self.read_status(screen, source)
            if status.get('体力') is not None and status.get('清洁') is not None:
                return status
            log(f'状态数值未加载，等待重试 ({attempt}/{attempts})')
            time.sleep(CLICK_INTERVAL)
        return status

    def cache_care_items(self, anchor: str, **status_fields) -> None:
        """喂食/洗澡结束时把物品库存写进状态缓存（供 GUI 日志页状态条显示）。

        anchor：'feed_10'（喂食面板，库存记为饼干）/ 'shower_10'（洗澡面板，记为香皂）。
        库存数字没有文字标签，OCR 控件附近区域后取离 anchor 控件中心最近的数字。
        顺带刷新刚变化的体力/清洁；识别失败只记日志，不影响照顾流程。
        """
        try:
            fields = dict(status_fields)
            count = self._read_item_count(anchor)
            if count is not None:
                log(f'物品库存: {anchor} 库存={count}')
                fields[ITEM_ANCHOR_KEYS[anchor]] = count
            update_status(None, **fields)
        except Exception as e:
            log(f'物品库存识别失败: {e}')

    def _read_item_count(self, anchor: str) -> int | None:
        """OCR anchor 控件附近区域，取离控件中心最近的数字作为物品库存。

        库存数字是图标旁的小角标：不能 OCR 整个下半屏——图太大会被检测模型
        内部再缩小（det_limit_side_len），小角标识别不到；以控件 bounds 向外
        扩 ITEM_CROP_MARGIN 裁小图 OCR，并在日志里打印识别结果便于校准。
        """
        bounds = see_bounds(self.dev, anchor)
        if not bounds:
            log(f'未定位到 {anchor}，无法读取物品库存')
            return None
        screen = self.screen()
        h, w = screen.shape[:2]
        x1, y1, x2, y2 = bounds
        m = ITEM_CROP_MARGIN
        rx1, ry1 = max(0, x1 - m), max(0, y1 - m)
        rx2, ry2 = min(w, x2 + m), min(h, y2 + m)
        results = ocr_texts(screen[ry1:ry2, rx1:rx2])
        log(f'库存区域 OCR: '
            + (', '.join(f'{t!r}@({x},{y})' for t, x, y, _ in results) or '无'))
        # 控件中心换算到裁剪后的 OCR 图坐标
        cx = (x1 + x2) / 2 - rx1
        cy = (y1 + y2) / 2 - ry1
        count = nearest_item_count(results, cx, cy)
        if count is None:
            log('库存区域 OCR 未找到数字')
        return count

    # ---- 照顾动作 ----

    def _pay_buy_popup(self, amount_default: str, select_name: str | None = None) -> None:
        """购买弹窗（兑换食物/购买洗澡道具点开后的弹窗）通用流程：
        数量输入框（默认 amount_default）改为 EXCHANGE_FOOD_COUNT；select_name 传入时先点选商品，随后点输入框 ->
        点"支付 xx 金币"（两个弹窗的支付按钮相同，共用 exchange_pay），
        弹窗关闭后由调用方重新找物品按钮继续护理。"""
        # 数量输入框有默认值，改成 EXCHANGE_FOOD_COUNT。
        # set_text 需要控件句柄（LOCATORS 的 see() 只给坐标），直接用 u2 xpath
        amount = self.dev.d.xpath(f'//*[@text="{amount_default}"]')
        for attempt in range(1, EXCHANGE_POPUP_RETRIES + 1):
            if amount.exists:
                break
            log(f'等待购买弹窗 ({attempt}/{EXCHANGE_POPUP_RETRIES})')
            time.sleep(CLICK_INTERVAL)
        else:
            raise RuntimeError(f'购买弹窗未找到数量输入框（text="{amount_default}"）')
        if select_name:
            sel = self.see(select_name)
            if sel:
                log(f'选择商品 {select_name}')
                self.click(sel[0], sel[1])
                time.sleep(CLICK_INTERVAL)
            else:
                log(f'未找到待选择商品 {select_name}，继续改数量')
        bounds = self.dev.find_xpath_bounds(f'//*[@text="{amount_default}"]')
        if not bounds:
            raise RuntimeError(f'购买弹窗未找到数量输入框（text="{amount_default}"）')
        x1, y1, x2, y2 = bounds
        log('点击数量输入框')
        self.click((x1 + x2) // 2, (y1 + y2) // 2)
        time.sleep(CLICK_INTERVAL)
        amount.set_text(str(EXCHANGE_FOOD_COUNT))
        time.sleep(0.5)  # 等"支付 xx 金币"按钮金额随数量刷新
        pay = self.see('exchange_pay')
        if not pay:
            raise RuntimeError('购买弹窗未找到"支付 xx 金币"按钮')
        log(f'购买 {EXCHANGE_FOOD_COUNT} 个，点击支付 ({pay[0]}, {pay[1]})')
        self.click(pay[0], pay[1])
        time.sleep(CLICK_INTERVAL)

    def _exchange_food(self, source=None) -> None:
        """饼干不足（无 feed_10）时用金币兑换食物：同一控件树里点"兑换食物" ->
        兑换弹窗数量输入框（默认 5）改 99 -> 支付，回喂食面板后继续喂食。"""
        hit = self.see('exchange_food', source=source)
        if not hit:
            raise RuntimeError('喂食界面未找到 feed_10 按钮，也没有"兑换食物"')
        log('未找到 feed_10（饼干不足），点击"兑换食物"')
        self.click(hit[0], hit[1])
        time.sleep(CLICK_INTERVAL)
        self._pay_buy_popup('5', 'cookie_5')

    def _buy_soap(self, source=None) -> None:
        """香皂不足（无 shower_10）时用金币购买洗澡道具：同一控件树里点"购买洗澡道具" ->
        购买弹窗数量输入框（默认 10）改 99 -> 支付，回洗澡面板后继续洗澡。"""
        hit = self.see('buy_soap', source=source)
        if not hit:
            raise RuntimeError('洗澡界面未找到 shower_10 肥皂，也没有"购买洗澡道具"')
        log('未找到 shower_10（香皂不足），点击"购买洗澡道具"')
        self.click(hit[0], hit[1])
        time.sleep(CLICK_INTERVAL)
        self._pay_buy_popup('10', 'soap_2')

    def feed(self, source=None) -> None:
        """喂食：点 feed -> 反复点 feed_10 并复测体力，直到达到阈值。
        没有 feed_10（饼干不足）时先 _exchange_food() 金币兑换食物再继续；
        支付后面板若被收起则原地重新点 feed 打开（不回主页面重进）。"""
        hit = self.see('feed', source=source)
        if not hit:
            raise RuntimeError('未找到 feed 喂食按钮')
        self.click(hit[0], hit[1])
        time.sleep(CLICK_INTERVAL)
        source = self.dev.hierarchy()
        # 好友家页面偶发卡顿，喂食面板加载慢：等 feed_10 或"兑换食物"出现再进喂食循环
        for attempt in range(1, FEED_PANEL_RETRIES + 1):
            if self.see('feed_10', source=source) or self.see('exchange_food', source=source):
                break
            log(f'等待喂食面板加载 ({attempt}/{FEED_PANEL_RETRIES})')
            time.sleep(CLICK_INTERVAL)
            source = self.dev.hierarchy()
        exchanged = False  # 每次喂食最多兑换一次（99 个足够），防兑换后仍无 feed_10 死循环
        for attempt in range(1, MAX_FEED_ATTEMPTS + 1):
            btn = self.see('feed_10', source=source)
            if not btn:
                if exchanged:
                    raise RuntimeError('兑换食物后仍未找到 feed_10 按钮')
                self._exchange_food(source)
                exchanged = True
                source = self.dev.hierarchy()
                if not self.see('feed_10', source=source):
                    # 支付后喂食面板可能被收起（回宠物页）：原地重新点 feed 打开，
                    # 不抛异常——异常会让调度器回主页面重进好友家
                    feed = self.see('feed', source=source)
                    if feed:
                        log('兑换后喂食面板已关闭，原地重新打开')
                        self.click(feed[0], feed[1])
                        time.sleep(CLICK_INTERVAL)
                        source = self.dev.hierarchy()
                continue
            self.click(btn[0], btn[1])
            time.sleep(FEED_RESULT_WAIT)
            screen, source = self.snapshot()
            energy = self.read_status(screen, source).get('体力')
            log(f'第 {attempt} 次喂食后体力: {energy}')
            if energy is not None and energy >= self.energy_threshold:
                log(f'体力已达标（>= {self.energy_threshold}）')
                self.cache_care_items('feed_10', energy=energy)
                return
        # 达到上限仍不达阈值：跳过本次喂食，不再抛异常（游戏有概率显示 bug——
        # 实际体力已达标但界面/OCR 没刷新，抛异常会让调度器走重启恢复甚至告警退出）
        log(f'喂食 {MAX_FEED_ATTEMPTS} 次后体力仍未达到 {self.energy_threshold}，'
            f'跳过本次喂食（可能是游戏显示未刷新，实际已达标）')

    def scrub_path(self, x1: int, y1: int, x2: int, y2: int,
                   steps: int = 5, step_sleep: float = 0.05) -> None:
        """按住状态下把触摸点从 (x1, y1) 匀速移动到 (x2, y2)（实际像素坐标）。"""
        for i in range(1, steps + 1):
            self.dev.touch_move(x1 + (x2 - x1) * i // steps,
                                y1 + (y2 - y1) * i // steps)
            time.sleep(step_sleep)

    def shower(self, source=None) -> None:
        """洗澡：点 shower -> 按住肥皂（shower_10 控件中心）不松手拖到
        (50%, 40%)，再在 (50%, 67%) 和 (50%, 40%) 之间来回搓洗，直到清洁达到阈值后抬手。
        没有 shower_10（香皂不足）时先 _buy_soap() 金币购买洗澡道具再继续；
        支付后面板若被收起则原地重新点 shower 打开（不回主页面重进）。

        用 u2 的 d.touch.down/move/up 分步注入，整个搓洗过程不抬手
        （普通 swipe 每次都会抬手，游戏不累计清洁度）。
        搓洗点位按当前分辨率百分比换算（SCRUB_TOP_PCT / SCRUB_BOTTOM_PCT）。
        清洁连续 SCRUB_STALL_REPRESS 回合不提升判定按压失效（模拟器上 minitouch
        会话会静默中断，之后 touch_move 全丢、肥皂停在原地），自动抬手重按自愈。
        """
        hit = self.see('shower', source=source)
        if not hit:
            raise RuntimeError('未找到 shower 洗澡按钮')
        self.click(hit[0], hit[1])
        time.sleep(CLICK_INTERVAL)
        source = self.dev.hierarchy()
        soap = self.see('shower_10', source=source)
        if not soap:
            # 香皂不足：金币购买一次（99 个足够，只买一次防死循环）后重新找
            self._buy_soap(source)
            source = self.dev.hierarchy()
            soap = self.see('shower_10', source=source)
            if not soap:
                # 支付后洗澡面板可能被收起（回宠物页）：原地重新点 shower 打开，
                # 不抛异常——异常会让调度器回主页面重进好友家
                hit = self.see('shower', source=source)
                if hit:
                    log('购买后洗澡面板已关闭，原地重新打开')
                    self.click(hit[0], hit[1])
                    time.sleep(CLICK_INTERVAL)
                    source = self.dev.hierarchy()
                    soap = self.see('shower_10', source=source)
            if not soap:
                raise RuntimeError('购买洗澡道具后仍未找到 shower_10 肥皂')
        w, h = self.dev.window_size()
        top = (round(w * SCRUB_TOP_PCT[0]), round(h * SCRUB_TOP_PCT[1]))
        bottom = (round(w * SCRUB_BOTTOM_PCT[0]), round(h * SCRUB_BOTTOM_PCT[1]))
        log(f'按住肥皂 ({soap[0]}, {soap[1]}) 拖到 {top} 开始搓洗')
        self.dev.touch_down(soap[0], soap[1])
        try:
            # 从肥皂慢速拖到搓洗上端点进入搓洗
            self.scrub_path(soap[0], soap[1], *top)
            last_clean: int | None = None
            stall = 0  # 清洁连续不提升的回合数
            for attempt in range(1, MAX_SHOWER_ATTEMPTS + 1):
                # 在 (50%, 67%) 和 (50%, 40%) 之间来回拖（截图复测不影响按压）
                self.scrub_path(*bottom, *top)
                self.scrub_path(*top, *bottom)
                clean = self.read_status(self.screen(), source).get('清洁')
                log(f'搓洗 {attempt} 回合后清洁: {clean}')
                if clean is not None and clean >= self.clean_threshold:
                    log(f'清洁已达标（>= {self.clean_threshold}）')
                    self.cache_care_items('shower_10', clean=clean)
                    return
                # 按压失效检测：清洁连续 SCRUB_STALL_REPRESS 回合不提升，
                # 说明 minitouch 会话静默中断（touch_move 全丢，肥皂停在原地），
                # 抬手重按肥皂自愈；识别不到（None）不计入停滞
                if clean is not None and last_clean is not None and clean <= last_clean:
                    stall += 1
                elif clean is not None:
                    stall = 0
                if clean is not None:
                    last_clean = clean
                if stall >= SCRUB_STALL_REPRESS:
                    log(f'清洁连续 {stall} 回合未提升，按压可能失效，抬手重按肥皂')
                    self.dev.touch_up(*bottom)
                    time.sleep(CLICK_INTERVAL)
                    soap = self.see('shower_10')
                    if not soap:
                        raise RuntimeError('重按肥皂时未找到 shower_10')
                    self.dev.touch_down(soap[0], soap[1])
                    self.scrub_path(soap[0], soap[1], *top)
                    stall = 0
            # 达到上限仍不达阈值：跳过本次洗澡，不再抛异常（游戏有概率显示 bug——
            # 实际清洁已达标但界面/OCR 没刷新，抛异常会让调度器走重启恢复甚至告警退出）
            log(f'搓洗 {MAX_SHOWER_ATTEMPTS} 回合后清洁仍未达到 {self.clean_threshold}，'
                f'跳过本次洗澡（可能是游戏显示未刷新，实际已达标）')
        finally:
            self.dev.touch_up(*bottom)

    def exit_care_mode(self, source=None):
        """若仍在喂食/洗澡界面（feed_10 / shower_10 可见），点 back 退出。

        返回退出后的控件树快照，供收起状态面板复用。
        """
        for _ in range(5):
            if source is None:
                source = self.dev.hierarchy()
            if self.see('feed_10', source=source) or self.see('shower_10', source=source):
                if not self.go_back(source=source):
                    raise RuntimeError('退出喂食/洗澡状态失败：未找到 back 按钮')
                time.sleep(CLICK_INTERVAL)
                source = None
            else:
                return source
        log('警告: 多次点击后仍未退出喂食/洗澡状态')
        return self.dev.hierarchy()

    # ---- 主流程 ----

    def one_click_care(self) -> bool:
        """一键护理：主页面找 one_click_care* 按钮（content-desc 前缀匹配），
        有就点并结束照顾流程；不读状态、不手动喂食/洗澡。
        按钮只在体力/清洁不足时出现：没有按钮视为状态正常，跳过护理。
        点击后若有"支付并护理"确认弹窗则一并点掉。
        点完后体力/清洁/心情/饼干/香皂的缓存值不再可信，从状态缓存清空（GUI 显示回 -）。
        返回是否点击了。"""
        hit = self.see('one_click_care')
        if not hit:
            # 一键护理按钮只在体力/清洁不足时出现：没有按钮 = 状态正常，跳过护理
            log('未找到一键护理按钮（体力/清洁正常），跳过护理')
            return False
        log('使用一键护理')
        self.click(hit[0], hit[1])
        # 确认弹窗可能比护理按钮点击晚一拍出现，短等几次再判断
        for attempt in range(1, ONE_CLICK_PAY_RETRIES + 1):
            pay = self.see('one_click_pay')
            if pay:
                log('检测到"支付并护理"，点击确认')
                self.click(pay[0], pay[1])
                time.sleep(CLICK_INTERVAL)
                break
            if attempt < ONE_CLICK_PAY_RETRIES:
                time.sleep(CLICK_INTERVAL)
        clear_status_fields('energy', 'clean', 'mood', 'biscuit', 'soap')
        return True

    def toggle_status(self, source=None) -> None:
        """点击宠物状态按钮（xpath 定位）展开/收起状态面板。"""
        hit = self.see('pet_status', source=source)
        if not hit:
            raise RuntimeError('未找到宠物状态按钮')
        self.click(hit[0], hit[1])
        time.sleep(CLICK_INTERVAL)

    def check_and_care(self) -> None:
        """检查一次体力/清洁，低于阈值则喂食/洗澡，最后收起状态面板。
        护理方式为"一键护理"时不读状态：主页面有一键护理按钮就点，然后直接结束。"""
        if self.method == '一键护理':
            self.ensure_main_page()
            self.one_click_care()
            return
        source = self.ensure_main_page()
        self.toggle_status(source)
        # 状态面板展开后重新读状态；数值异步加载（刚展开可能只有账号/宠物名），
        # read_status_ready 内部重新截图重试；feed/shower 入口按钮重新抓控件树
        status = self.read_status_ready()
        source = self.dev.hierarchy()
        log(f'宠物状态: 体力={status.get("体力")} '
            f'清洁={status.get("清洁")} 心情={status.get("心情")} '
            f'账号名称={status.get("账号名称")} 宠物名称={status.get("宠物名称")}')
        # 写状态缓存（GUI 日志页顶部状态条显示；已取消多账号区分，固定 default 条目）
        update_status(None,
                      pet_name=status.get('宠物名称'),
                      energy=status.get('体力'),
                      clean=status.get('清洁'),
                      mood=status.get('心情'))
        cared = False
        energy = status.get('体力')
        if energy is not None and energy < self.energy_threshold:
            log(f'体力 {energy} < 阈值 {self.energy_threshold}，需要喂食')
            self.feed(source)
            cared = True
            source = self.dev.hierarchy()
        clean = status.get('清洁')
        if clean is not None and clean < self.clean_threshold:
            log(f'清洁 {clean} < 阈值 {self.clean_threshold}，需要洗澡')
            self.shower(source)
            cared = True
            source = self.dev.hierarchy()
        if cared:
            source = self.exit_care_mode(source)
        self.toggle_status(source)
        log('状态检查完成，已收起宠物状态')


if __name__ == '__main__':
    try:
        CareScenario().check_and_care()
    except KeyboardInterrupt:
        log('手动停止')
