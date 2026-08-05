"""宠物状态照顾：任务调度前检查体力/清洁，不足则喂食/洗澡。

流程（主页面操作，u2 控件/OCR 文字定位，坐标为 720x1280 参考值自动换算）：
1. 点击 pet_status（宠物状态按钮，xpath 定位）展开宠物状态
2. OCR 状态面板区域（xpath 定范围）识别 体力/清洁/心情 三个数值及 账号名称/宠物名称
3. 体力低于阈值 -> 喂食：点 feed -> 反复点 feed_10 并复测体力，直到达标
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PIL import Image

from src.locators import see_bounds
from src.ocr import ocr_texts
from src.progress import log
from src.scenario import CLICK_INTERVAL, DeviceScenario
from src.u2dev import REF_SIZE

# 洗澡搓洗点位：起点 = shower_10 控件中心（肥皂），其余按当前分辨率百分比换算
SCRUB_TOP_PCT = (0.5, 0.40)     # 拖动终点 / 搓洗上端点
SCRUB_BOTTOM_PCT = (0.5, 0.67)  # 搓洗下端点 / 抬手点
# 状态数值 OCR 放大倍数（上半屏截图放大，提高小字识别率）
STATUS_OCR_SCALE = 2
# 状态数值配对的像素容差：按 720 宽参考分辨率的 OCR 结果调的，
# 运行时按当前屏宽等比缩放（见 read_status）
STATUS_ROW_TOL = 40  # 名字右侧同行数字的纵向容差
STATUS_COL_TOL = 80  # 名字下方同列数字的横向容差
FEED_RESULT_WAIT = 1.5  # 喂食后等数值刷新的时间（秒）
MAX_FEED_ATTEMPTS = 10    # 喂食最多次数，超过认为异常
MAX_SHOWER_ATTEMPTS = 25  # 搓洗最多回合数，超过认为异常

STATUS_NAMES = ('体力', '清洁', '心情')


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


class CareScenario(DeviceScenario):
    def __init__(self, dev=None):
        super().__init__(dev)
        self.energy_threshold = self.cfg.care.energy_threshold
        self.clean_threshold = self.cfg.care.clean_threshold
        log(f'体力阈值: {self.energy_threshold}，清洁阈值: {self.clean_threshold}')

    # ---- 状态识别 ----

    def read_status(self, screen=None, source=None) -> dict:
        """OCR 宠物状态面板区域（locators 的 status_region 定范围，bounds 有缓存），
        返回体力/清洁/心情/账号名称/宠物名称（识别不到的缺省）。
        调用方已截图/抓控件树时传入，避免重复采集。"""
        if screen is None:
            screen = self.screen()
        bounds = see_bounds(self.dev, 'status_region', source)
        if bounds:
            x1, y1, x2, y2 = bounds
            region = screen[y1:y2, x1:x2]
        else:
            log('未定位到宠物状态区域，回退上半屏 OCR')
            region = screen[: screen.shape[0] // 2]
        img = Image.fromarray(region)
        # 放大提高小字识别率
        img = img.resize((img.width * STATUS_OCR_SCALE, img.height * STATUS_OCR_SCALE),
                         Image.LANCZOS)
        results = ocr_texts(np.asarray(img))
        log('状态区域 OCR: '
            + (', '.join(f'{t!r}@({x},{y})' for t, x, y, _ in results) or '无'))
        # 容差随分辨率等比缩放：OCR 图宽 = 区域宽 * SCALE，区域宽正比于屏宽
        scale = screen.shape[1] / REF_SIZE[0]
        return parse_status(results, scale)

    # ---- 照顾动作 ----

    def feed(self, source=None) -> None:
        """喂食：点 feed -> 反复点 feed_10 并复测体力，直到达到阈值。"""
        hit = self.see('feed', source=source)
        if not hit:
            raise RuntimeError('未找到 feed 喂食按钮')
        self.click(hit[0], hit[1])
        time.sleep(CLICK_INTERVAL)
        source = self.dev.hierarchy()
        for attempt in range(1, MAX_FEED_ATTEMPTS + 1):
            btn = self.see('feed_10', source=source)
            if not btn:
                raise RuntimeError('喂食界面未找到 feed_10 按钮')
            self.click(btn[0], btn[1])
            time.sleep(FEED_RESULT_WAIT)
            screen, source = self.snapshot()
            energy = self.read_status(screen, source).get('体力')
            log(f'第 {attempt} 次喂食后体力: {energy}')
            if energy is not None and energy >= self.energy_threshold:
                log(f'体力已达标（>= {self.energy_threshold}）')
                return
        raise RuntimeError(f'喂食 {MAX_FEED_ATTEMPTS} 次后体力仍未达到 {self.energy_threshold}')

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

        用 u2 的 d.touch.down/move/up 分步注入，整个搓洗过程不抬手
        （普通 swipe 每次都会抬手，游戏不累计清洁度）。
        搓洗点位按当前分辨率百分比换算（SCRUB_TOP_PCT / SCRUB_BOTTOM_PCT）。
        """
        hit = self.see('shower', source=source)
        if not hit:
            raise RuntimeError('未找到 shower 洗澡按钮')
        self.click(hit[0], hit[1])
        time.sleep(CLICK_INTERVAL)
        source = self.dev.hierarchy()
        soap = self.see('shower_10', source=source)
        if not soap:
            raise RuntimeError('洗澡界面未找到 shower_10 肥皂')
        w, h = self.dev.window_size()
        top = (round(w * SCRUB_TOP_PCT[0]), round(h * SCRUB_TOP_PCT[1]))
        bottom = (round(w * SCRUB_BOTTOM_PCT[0]), round(h * SCRUB_BOTTOM_PCT[1]))
        log(f'按住肥皂 ({soap[0]}, {soap[1]}) 拖到 {top} 开始搓洗')
        self.dev.touch_down(soap[0], soap[1])
        try:
            # 从肥皂慢速拖到搓洗上端点进入搓洗
            self.scrub_path(soap[0], soap[1], *top)
            for attempt in range(1, MAX_SHOWER_ATTEMPTS + 1):
                # 在 (50%, 67%) 和 (50%, 40%) 之间来回拖（截图复测不影响按压）
                self.scrub_path(*bottom, *top)
                self.scrub_path(*top, *bottom)
                clean = self.read_status(self.screen(), source).get('清洁')
                log(f'搓洗 {attempt} 回合后清洁: {clean}')
                if clean is not None and clean >= self.clean_threshold:
                    log(f'清洁已达标（>= {self.clean_threshold}）')
                    return
            raise RuntimeError(f'搓洗 {MAX_SHOWER_ATTEMPTS} 回合后清洁仍未达到 {self.clean_threshold}')
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
                back = self.see('back', source=source)
                if not back:
                    raise RuntimeError('退出喂食/洗澡状态失败：未找到 back 按钮')
                self.click(back[0], back[1])
                time.sleep(CLICK_INTERVAL)
                source = None
            else:
                return source
        log('警告: 多次点击后仍未退出喂食/洗澡状态')
        return self.dev.hierarchy()

    # ---- 主流程 ----

    def toggle_status(self, source=None) -> None:
        """点击宠物状态按钮（xpath 定位）展开/收起状态面板。"""
        hit = self.see('pet_status', source=source)
        if not hit:
            raise RuntimeError('未找到宠物状态按钮')
        self.click(hit[0], hit[1])
        time.sleep(CLICK_INTERVAL)

    def check_and_care(self) -> None:
        """检查一次体力/清洁，低于阈值则喂食/洗澡，最后收起状态面板。"""
        source = self.ensure_main_page()
        self.toggle_status(source)
        # 状态面板展开后必须重新截图/抓控件树，后续状态区域和 feed/shower 入口按钮共用这份快照
        # （feed_10/shower_10 只有点了对应入口才会出现，之后在对应流程里重新抓控件树）
        screen, source = self.snapshot()
        status = self.read_status(screen, source)
        log(f'宠物状态: 体力={status.get("体力")} '
            f'清洁={status.get("清洁")} 心情={status.get("心情")} '
            f'账号名称={status.get("账号名称")} 宠物名称={status.get("宠物名称")}')
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
