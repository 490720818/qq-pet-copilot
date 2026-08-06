"""场景基类：设备连接、截图、u2/OCR 定位点击、通用导航。"""
from __future__ import annotations

import time

from .config import find_adb, load_config
from .locators import ocr_screen
from .locators import see as locate
from .locators import see_all as locate_all
from .ocr import parse_employed_ratio
from .progress import count_cross, log
from .u2dev import U2Device

# ---- 可调参数 ----
CLICK_INTERVAL = 1.0       # 连续点击/重试间隔（秒）
NAV_TIMEOUT = 10           # 单个阶段最多重试次数，超过认为卡死抛异常
MAIN_PAGE_ATTEMPTS = 10    # 回主页面最多尝试次数（识别 main_sign / 点 back）
BUSY_GATE_ATTEMPTS = 2     # 出门后进行中状态的检测次数（活动面板加载有几秒延迟）
WAIT_LOG_INTERVAL = 300.0  # 长等待期间的心跳日志间隔（秒），避免每轮检测刷屏


class DeviceScenario:
    """各场景共用：截图、u2 控件/OCR 文字定位、点击/拖动、回主页面。"""

    def __init__(self, dev: U2Device | None = None):
        self.cfg = load_config()
        if dev is None:
            dev = U2Device(find_adb(self.cfg.adb.path), self.cfg.adb.device_serial)
        self.dev = dev
        # 等待间隙回调（执行器设置，用来在上课/打工等长等待中插空处理踩踩）
        self.wait_hook = None

    def screen(self):
        return self.dev.screenshot()

    @property
    def check_interval(self) -> float:
        """进行中状态（上课/打工/冒险/被雇佣）的统一检查间隔（秒），
        配置 schedule.check_interval。"""
        return float(self.cfg.schedule.check_interval)

    def see(self, name: str, screen=None, source=None):
        """当前屏幕是否能看到名为 name 的元素，返回 (x, y, score) 或 None。

        一轮检测多个元素时应共享同一个 screen / source（截图和控件树快照），
        避免每次调用都重新截图、重新 dump 控件树。
        """
        return locate(self.dev, name, screen, source)

    def snapshot(self):
        """抓一轮检测用的快照：(屏幕截图, 控件树快照)。"""
        return self.screen(), self.dev.hierarchy()

    def see_all(self, name: str, screen=None):
        """定位 name 的所有命中（OCR 多点），按从上到下排序。"""
        return locate_all(self.dev, name, screen)

    def click(self, x: int, y: int) -> None:
        log(f'点击 ({x}, {y})')
        self.dev.click(x, y)

    def swipe(self, x1: int, y1: int, x2: int, y2: int) -> None:
        """按 720x1280 参考坐标拖动（内部换算到当前分辨率）。"""
        ax1, ay1 = self.dev.rel(x1, y1)
        ax2, ay2 = self.dev.rel(x2, y2)
        log(f'拖动 ({ax1}, {ay1}) -> ({ax2}, {ay2})')
        self.dev.swipe(ax1, ay1, ax2, ay2)

    def click_rel(self, x: int, y: int) -> None:
        """点击 720x1280 参考坐标（内部换算到当前分辨率）。"""
        self.click(*self.dev.rel(x, y))

    def click_until_gone_or_see(self, click_name: str, wait_name: str, stage: str) -> None:
        """点击 click_name，直到看见 wait_name；点击后 click_name 消失也视为已跳转。"""
        clicked = False
        for attempt in range(1, NAV_TIMEOUT + 1):
            # 只抓控件树快照，截图按需懒加载（see 内部 OCR 需要时才截）：
            # 状态区域不存在时 xpath_ocr 不截图，每轮省 1-2 秒
            source = self.dev.hierarchy()
            hit = self.see(wait_name, None, source)
            if hit:
                log(f'{stage}: 已出现 {wait_name} (score={hit[2]:.2f})')
                return
            target = self.see(click_name, None, source)
            if target:
                self.click(target[0], target[1])
                clicked = True
            elif clicked:
                # 按钮点完消失但下一页标志还没识别到：说明页面已跳转，
                # 不能因为下一页 XPath/OCR 未命中而一直重复等待
                log(f'{stage}: {click_name} 已消失，进入下一阶段')
                return
            else:
                if attempt == 1 or attempt == NAV_TIMEOUT:
                    log(f'{stage}: 未找到 {click_name}，等待重试 ({attempt}/{NAV_TIMEOUT})')
            time.sleep(CLICK_INTERVAL)
        raise RuntimeError(f'{stage}: 重试 {NAV_TIMEOUT} 次仍未出现 {wait_name}')

    def ensure_main_page(self):
        """确认在主页面；不在则点 back 直到回来，返回主页面控件树快照。

        识别不到 main_sign（金币胶囊，只有自己主页面有，好友宠物页没有）
        就直接点 back，然后立即重新抓控件树判断。
        返回的 source 可直接给同一个页面上的后续 XPath 定位复用。
        """
        for attempt in range(1, MAIN_PAGE_ATTEMPTS + 1):
            screen, source = self.snapshot()
            hit = self.see('main_sign', screen, source)
            if hit:
                log(f'已在主页面 (score={hit[2]:.2f})')
                return source
            back = self.see('back', screen, source)
            if back:
                log(f'未识别到主页面，点击 back ({back[0]}, {back[1]})')
                self.click(back[0], back[1])
                continue
            if attempt == 1 or attempt == MAIN_PAGE_ATTEMPTS:
                log(f'未识别到主页面也找不到 back，等待重试 ({attempt}/{MAIN_PAGE_ATTEMPTS})')
            time.sleep(CLICK_INTERVAL)
        raise RuntimeError('无法回到主页面')

    def reset_select_boxes(self) -> None:
        """学习/工作三栏选择框归位：一次定位后，从第一框中心拖到第三框中心两次。"""
        source = self.dev.hierarchy()
        first = self.see('select_box_1', source=source)
        third = self.see('select_box_3', source=source)
        if not (first and third):
            raise RuntimeError('未定位到选择框，无法归位')
        for _ in range(2):
            log(f'选择框归位拖动 ({first[0]}, {first[1]}) -> ({third[0]}, {third[1]})')
            self.dev.drag(first[0], first[1], third[0], third[1])
            time.sleep(0.3)

    def leave_home(self) -> None:
        """主页面点击出门（OCR 定位"出门"，识别不到由注册表用参考坐标兜底）。"""
        hit = self.see('leave_home')
        if hit:
            self.click(hit[0], hit[1])
        else:
            log('未定位到"出门"按钮')
        time.sleep(CLICK_INTERVAL)

    def wait_end(self, in_name: str, end_name: str, check_interval: float | None = None) -> None:
        """等待 end_name 出现并点 quit 退出，期间点 in_name 画面防设备休眠。
        check_interval=None 时用统一配置 schedule.check_interval。"""
        if check_interval is None:
            check_interval = self.check_interval
        last_log_at = 0.0
        while True:
            screen, source = self.snapshot()
            hit = self.see(end_name, screen, source)
            if hit:
                log(f'检测到结束标志 {end_name} (score={hit[2]:.2f})')
                break
            cur = self.see(in_name, screen, source)
            if cur:
                self.dev.click(cur[0], cur[1])  # 防休眠点击不记日志
            now = time.monotonic()
            if now - last_log_at >= WAIT_LOG_INTERVAL:
                log('仍在进行中...')
                last_log_at = now
            self._run_wait_hook()
            time.sleep(check_interval)
        quit_hit = self.see('quit')
        if quit_hit:
            self.click(quit_hit[0], quit_hit[1])
            time.sleep(CLICK_INTERVAL)
        else:
            log('未找到 quit 按钮，直接返回')

    def _run_wait_hook(self) -> None:
        """等待间隙回调（执行器用来插空处理踩踩）；失败只记日志不打扰等待。"""
        if self.wait_hook:
            try:
                self.wait_hook()
            except Exception as e:
                log(f'等待间隙任务失败: {e}')

    def see_employed_sign(self, screen):
        """被雇佣召回标志：面板分成比例行"雇佣者 x% 被雇佣者 y%"，
        仅 x<=25 且 y>=75（宠物分成最高的终态，方向不能反）时命中，
        返回 (x, y, score) 或 None。"""
        return parse_employed_ratio(ocr_screen(screen))

    def wait_employed_back(self, check_interval: float | None = None) -> None:
        """被雇佣中：按检查间隔识别一次，出现召回标志（分成比例终态）就点
        employed_come_back 提前召回，再点 employed_come_back_confirm 确认；
        确认后等待 employed_end 出现，点 quit 退出并计入被雇佣次数。
        check_interval=None 时用统一配置 schedule.check_interval。"""
        if check_interval is None:
            check_interval = self.check_interval
        last_log_at = 0.0
        while True:
            screen, source = self.snapshot()
            sign = self.see_employed_sign(screen)
            if sign:
                log(f'检测到召回标志（分成比例终态） (score={sign[2]:.2f})')
                for attempt in range(1, NAV_TIMEOUT + 1):
                    # 先查确认按钮：召回点击后 confirm 可能延迟弹出，
                    # 此时 come_back 已消失，只查 come_back 会永远等不到
                    screen, source = self.snapshot()
                    confirm = self.see('employed_come_back_confirm', screen, source)
                    if confirm:
                        self.click(confirm[0], confirm[1])
                        time.sleep(CLICK_INTERVAL)
                        break
                    back = self.see('employed_come_back', screen, source)
                    if back:
                        self.click(back[0], back[1])
                    else:
                        if attempt == 1 or attempt == NAV_TIMEOUT:
                            log(f'未找到召回/确认按钮，重试 ({attempt}/{NAV_TIMEOUT})')
                    time.sleep(CLICK_INTERVAL)
                else:
                    raise RuntimeError('出现召回标志但召回/确认按钮未找到')
                # 确认后等待结束界面，点 quit 退出并计数
                for attempt in range(1, NAV_TIMEOUT + 1):
                    screen, source = self.snapshot()
                    end = self.see('employed_end', screen, source)
                    if end:
                        log(f'检测到被雇佣结束标志 employed_end (score={end[2]:.2f})')
                        break
                    if attempt == 1 or attempt == NAV_TIMEOUT:
                        log(f'等待 employed_end 出现 ({attempt}/{NAV_TIMEOUT})')
                    time.sleep(CLICK_INTERVAL)
                else:
                    raise RuntimeError('召回确认后未出现 employed_end')
                quit_hit = self.see('quit')
                if quit_hit:
                    self.click(quit_hit[0], quit_hit[1])
                    time.sleep(CLICK_INTERVAL)
                else:
                    log('未找到 quit 按钮，直接返回')
                count_cross('employed')  # 点完 quit 就计数
                return
            # 点击一次被雇佣画面防止设备休眠
            cur = self.see('employed_in', screen, source)
            if cur:
                self.dev.click(cur[0], cur[1])  # 防休眠点击不记日志
            now = time.monotonic()
            if now - last_log_at >= WAIT_LOG_INTERVAL:
                log('仍在被雇佣中...')
                last_log_at = now
            self._run_wait_hook()
            time.sleep(check_interval)

    def wait_busy_end(self, check_interval: float | None = None) -> str | None:
        """出门后检测是否正在上课/工作/冒险/被雇佣中，是则等待结束并退出。

        返回 'school' / 'work' / 'adventure' / 'employed' / None
        （等完的是哪种，用于计入对应计数）。
        check_interval=None 时用统一配置 schedule.check_interval。
        """
        if check_interval is None:
            check_interval = self.check_interval
        # 整屏截图匹配关键词判断进行中状态（同一屏幕四次 see 共享一次 OCR，
        # 见 locators._ocr_texts_cached）。出门后活动面板有几秒加载延迟，
        # 未匹配到任何状态时重试几次再下结论。
        for attempt in range(1, BUSY_GATE_ATTEMPTS + 1):
            screen = self.screen()
            if self.see('school_in', screen):
                log('检测到正在上课，等待这节课结束...')
                self.wait_end('school_in', 'school_end', check_interval)
                return 'school'
            if self.see('work_in', screen):
                log('检测到正在工作，等待这次工作结束...')
                self.wait_end('work_in', 'work_end', check_interval)
                return 'work'
            if self.see('adventure_in', screen):
                log('检测到正在冒险，等待这次冒险结束...')
                self.wait_end('adventure_in', 'adventure_end', check_interval)
                return 'adventure'
            if self.see('employed_in', screen):
                log('检测到被雇佣中，等待召回...')
                self.wait_employed_back()
                return 'employed'
            if attempt < BUSY_GATE_ATTEMPTS:
                time.sleep(CLICK_INTERVAL)
        return None
