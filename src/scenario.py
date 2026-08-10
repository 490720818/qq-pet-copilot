"""场景基类：设备连接、截图、u2/OCR 定位点击、通用导航。"""
from __future__ import annotations

import time

from .config import find_adb, load_config
from .locators import LOCATORS, ocr_screen
from .locators import see as locate
from .locators import see_all as locate_all
from .ocr import parse_employed_ratio, parse_employed_remaining
from .progress import count_cross, log
from .u2dev import U2Device

# ---- 可调参数 ----
CLICK_INTERVAL = 1.0       # 连续点击/重试间隔（秒）
NAV_TIMEOUT = 10           # 单个阶段最多重试次数，超过认为卡死抛异常
MAIN_PAGE_ATTEMPTS = 10    # 回主页面最多尝试次数（识别 main_sign / 点 back）
BUSY_GATE_ATTEMPTS = 2     # 出门后进行中状态的检测次数（活动面板加载有几秒延迟）
WAIT_LOG_INTERVAL = 300.0  # 长等待期间的心跳日志间隔（秒），避免每轮检测刷屏
ENCOURAGE_LOG_INTERVAL = 300.0  # "鼓励宠物"点击日志节流间隔（秒）：按钮每 ~12s 出现，避免刷屏
EMPLOYED_MAX_WAIT_MINUTES = 45  # 被雇佣"等到25/75（小于45min）"：面板剩余时间超过该值立即召回


class DeviceScenario:
    """各场景共用：截图、u2 控件/OCR 文字定位、点击/拖动、回主页面。"""

    def __init__(self, dev: U2Device | None = None):
        self.cfg = load_config()
        if dev is None:
            dev = U2Device(find_adb(self.cfg.adb.path), self.cfg.adb.device_serial)
        self.dev = dev

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

    def click_until_gone_or_see(self, click_name: str, wait_name: str, stage: str,
                                max_attempts: int = NAV_TIMEOUT) -> None:
        """点击 click_name，直到看见 wait_name；点击后 click_name 消失也视为已跳转。

        max_attempts: 最多重试次数，默认 NAV_TIMEOUT；某些"开始"转换失败率高、
        拖时间，调用方可以传小一点（如打工 work_start 传 3）。

        wait_name 纯 OCR（adventure_in/school_in/work_in）时先点后查：先查会每轮
        现截图+整屏 OCR（~1 秒）拖慢点击，这类"开始"转换改为点击目标在就只管点、
        目标消失（页面已跳转）才查 wait。其余 wait（xpath/u2，如 visit）仍先查——
        便宜，且避免 click_name 过度匹配时漏判"已进入目标状态"而一直重复点击
        （如 visit_friends 会匹配到好友列表里的"好友"按钮）。
        """
        wait_ocr_only = not any(LOCATORS.get(wait_name, {}).get(k)
                                for k in ('xpath', 'xpath_ocr', 'u2'))
        clicked = False
        for attempt in range(1, max_attempts + 1):
            # 只抓控件树快照，截图按需懒加载（see 内部 OCR 需要时才截）
            source = self.dev.hierarchy()
            target = self.see(click_name, None, source)
            if target and wait_ocr_only:
                # OCR wait：点击目标在就只管点，避免每轮截图+OCR
                self.click(target[0], target[1])
                clicked = True
                time.sleep(CLICK_INTERVAL)
                continue
            hit = self.see(wait_name, None, source)
            if hit:
                log(f'{stage}: 已出现 {wait_name} (score={hit[2]:.2f})')
                return
            if target:
                self.click(target[0], target[1])
                clicked = True
            elif clicked:
                # 按钮点完消失但下一页标志还没识别到：说明页面已跳转，
                # 不能因为下一页 XPath/OCR 未命中而一直重复等待
                log(f'{stage}: {click_name} 已消失，进入下一阶段')
                return
            else:
                if attempt == 1 or attempt == max_attempts:
                    log(f'{stage}: 未找到 {click_name}，等待重试 ({attempt}/{max_attempts})')
            time.sleep(CLICK_INTERVAL)
        raise RuntimeError(f'{stage}: 重试 {max_attempts} 次仍未出现 {wait_name}')

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

    def reset_select_boxes(self, drags: int = 2, source=None) -> None:
        """学习/工作三栏选择框归位：从第一框中心拖到第三框中心 drags 次。

        轮播有多个选择框（打工 4 个、学园 7 个），xpath 只认可见 3 个；
        drags 是回到第一页所需的拖动次数（打工 2 次、学园 3 次）。
        刚进面板时选择框可能还在加载，先重试等第一/三框都出现再归位，
        避免只查一次没查到就抛异常（页面加载慢/点进去还在转场）。
        """
        first = third = None
        source = None
        for attempt in range(1, 4):
            # select_box_N 由容器 bounds 推导：容器 cache 后秒回，
            # 未缓存时第一个 see 会 dump 一次并把容器 bounds 缓存，后续秒回
            first = self.see('select_box_1', source=source)
            third = self.see('select_box_3', source=source)
            if first and third:
                break
            if source is None:
                source = self.dev.hierarchy()  # 容器未命中：抓一次快照供推导
            if attempt == 1 or attempt == 3:
                log(f'未定位到选择框，等待加载 ({attempt}/3)')
            time.sleep(CLICK_INTERVAL)
        if not (first and third):
            raise RuntimeError('未定位到选择框，无法归位')
        for i in range(drags):
            log(f'选择框归位拖动 ({first[0]}, {first[1]}) -> ({third[0]}, {third[1]})')
            self.dev.drag(first[0], first[1], third[0], third[1])
            if i < drags - 1:  # sleep 只在两次拖动之间，最后一次拖完不再多等
                time.sleep(0.3)

    def leave_home(self) -> None:
        """主页面点击出门（OCR 定位"出门"，识别不到由注册表用参考坐标兜底）。"""
        hit = self.see('leave_home')
        if hit:
            self.click(hit[0], hit[1])
        else:
            log('未定位到"出门"按钮')
        time.sleep(CLICK_INTERVAL)

    def wait_end(self, in_name: str, end_name: str, check_interval: float | None = None,
                 encourage: bool = False) -> None:
        """等待 end_name 出现并点 quit 退出，期间点 in_name 画面防设备休眠。

        encourage=True 时每轮顺带检测"鼓励宠物"按钮（d(description="鼓励宠物")），
        出现就点一下（学习等待期间提升心情/互动收益）。
        check_interval=None 时用统一配置 schedule.check_interval。
        """
        if check_interval is None:
            check_interval = self.check_interval
        last_log_at = 0.0
        last_encourage_log = 0.0
        while True:
            screen, source = self.snapshot()
            hit = self.see(end_name, screen, source)
            if hit:
                log(f'检测到结束标志 {end_name} (score={hit[2]:.2f})')
                break
            cur = self.see(in_name, screen, source)
            if cur:
                self.dev.click(cur[0], cur[1])  # 防休眠点击不记日志
            if encourage:
                enc = self.see('encourage_pet', screen, source)
                if enc:
                    # 静默点击；日志按 ENCOURAGE_LOG_INTERVAL 节流（按钮每 ~12s 出现一次）
                    self.dev.click(enc[0], enc[1])
                    now = time.monotonic()
                    if now - last_encourage_log >= ENCOURAGE_LOG_INTERVAL:
                        log(f'检测到"鼓励宠物"，点击 ({enc[0]}, {enc[1]})')
                        last_encourage_log = now
            now = time.monotonic()
            if now - last_log_at >= WAIT_LOG_INTERVAL:
                log('仍在进行中...')
                last_log_at = now
            time.sleep(check_interval)
        quit_hit = self.see('quit')
        if quit_hit:
            self.click(quit_hit[0], quit_hit[1])
            time.sleep(CLICK_INTERVAL)
        else:
            log('未找到 quit 按钮，直接返回')

    def see_employed_sign(self, screen):
        """被雇佣召回标志：面板分成比例行"雇佣者 x% 被雇佣者 y%"，
        仅 x<=25 且 y>=75（宠物分成最高的终态，方向不能反）时命中，
        返回 (x, y, score) 或 None。"""
        return parse_employed_ratio(ocr_screen(screen))

    def see_employed_remaining(self, screen):
        """被雇佣面板剩余时间"剩余 00:44:00"：OCR 解析返回
        (剩余秒数, x, y, score) 或 None（解析不到由调用方回退到只等分成比例）。"""
        return parse_employed_remaining(ocr_screen(screen))

    def wait_employed_back(self, check_interval: float | None = None) -> None:
        """被雇佣中：按配置 employed.action 决定召回时机。

        等到25/75：分成比例到"雇佣者<=25% 被雇佣者>=75%"（宠物分成最高终态）
        才点 employed_come_back 提前召回；
        等到25/75（小于45min）：同左，但面板剩余时间 > EMPLOYED_MAX_WAIT_MINUTES
        分钟时不等比例直接召回（剩余 <=45 分钟才继续等到25/75）；
        立刻召回：进面板直接点"现在召回"。
        召回后点 employed_come_back_confirm 确认，等 employed_end 出现点 quit 退出并计数。
        check_interval=None 时用统一配置 schedule.check_interval。
        """
        if check_interval is None:
            check_interval = self.check_interval
        action = getattr(self.cfg.employed, 'action', '等到25/75')
        immediate = action == '立刻召回'
        time_capped = action == '等到25/75（小于45min）'
        last_log_at = 0.0
        while True:
            # 本循环全是 OCR 定位（剩余时间/分成比例/被雇佣中），
            # 不需要控件树快照（dump 一次 1~4s），只截图即可
            screen = self.screen()
            if immediate:
                log('按配置"立刻召回"被雇佣宠物')
            else:
                recall_ready = False
                if time_capped:
                    rem = self.see_employed_remaining(screen)
                    if rem is not None:
                        secs, _x, _y, _score = rem
                        if secs > EMPLOYED_MAX_WAIT_MINUTES * 60:
                            h, m, ss = secs // 3600, (secs % 3600) // 60, secs % 60
                            log(f'剩余时间 {h:02d}:{m:02d}:{ss:02d}'
                                f'（>{EMPLOYED_MAX_WAIT_MINUTES} 分钟），立即召回')
                            recall_ready = True
                if not recall_ready:
                    sign = self.see_employed_sign(screen)
                    if not sign:
                        # 点击一次被雇佣画面防止设备休眠
                        cur = self.see('employed_in', screen)
                        if cur:
                            self.dev.click(cur[0], cur[1])  # 防休眠点击不记日志
                        now = time.monotonic()
                        if now - last_log_at >= WAIT_LOG_INTERVAL:
                            log('仍在被雇佣中...')
                            last_log_at = now
                        time.sleep(check_interval)
                        continue
                    log(f'检测到召回标志（分成比例终态） (score={sign[2]:.2f})')
            # 召回：先点"现在召回"，再处理可能弹出的确认按钮
            for attempt in range(1, NAV_TIMEOUT + 1):
                # 先查确认按钮：召回点击后 confirm 可能延迟弹出，
                # 此时 come_back 已消失，只查 come_back 会永远等不到
                # 两个按钮都是自绘 OCR 定位，不需要控件树快照，只截图
                screen = self.screen()
                confirm = self.see('employed_come_back_confirm', screen)
                if confirm:
                    self.click(confirm[0], confirm[1])
                    time.sleep(CLICK_INTERVAL)
                    break
                back = self.see('employed_come_back', screen)
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

    def wait_busy_end(self, check_interval: float | None = None,
                      attempts: int = BUSY_GATE_ATTEMPTS) -> str | None:
        """出门后检测是否正在上课/工作/冒险/被雇佣中，是则等待结束并退出。

        返回 'school' / 'work' / 'adventure' / 'employed' / None
        （等完的是哪种，用于计入对应计数）。
        check_interval=None 时用统一配置 schedule.check_interval；
        attempts: 最多检测次数，默认 BUSY_GATE_ATTEMPTS。
        """
        time.sleep(0.5)
        if check_interval is None:
            check_interval = self.check_interval
        # 整屏 OCR 匹配关键词判断进行中状态（能覆盖全部四种状态，含被雇佣；
        # 之前的 status_banner 区域方案对被雇佣面板不适用）。出门后活动面板
        # 有几秒加载延迟，未匹配到任何状态时重试几次再下结论。
        for attempt in range(1, attempts + 1):
            screen = self.screen()
            # 职业升级弹窗（"你将进阶成为..." + "查看"按钮，出门页新弹窗）：
            # 点查看进职业树（职业树无原生返回键），连续按系统返回键逐层退出，
            # 再回主页面重新开始本轮，避免挡住进行中状态检测
            if self.see('career_upgrade', screen):
                view = self.see('career_upgrade_view', screen)
                if view:
                    log(f'检测到职业升级弹窗，点击"查看" ({view[0]}, {view[1]})')
                    self.click(view[0], view[1])
                    time.sleep(CLICK_INTERVAL)
                for _back in range(4):
                    self.dev.d.press('back')
                    time.sleep(CLICK_INTERVAL)
                    if not self.see('career_tree'):
                        break
                # 回主页面重新开始本轮（重新出门，走正常流程）
                self.ensure_main_page()
                self.leave_home()
                continue
            if self.see('school_in', screen):
                log('检测到正在上课，等待这节课结束...')
                self.wait_end('school_in', 'school_end', check_interval, encourage=True)
                return 'school'
            if self.see('work_in', screen):
                log('检测到正在打工，等待这次工作结束...')
                self.wait_end('work_in', 'work_end', check_interval, encourage=True)
                return 'work'
            if self.see('adventure_in', screen):
                log('检测到正在冒险，等待这次冒险结束...')
                self.wait_end('adventure_in', 'adventure_end', check_interval)
                return 'adventure'
            if self.see('employed_in', screen):
                log('检测到被雇佣中，等待召回...')
                self.wait_employed_back()
                return 'employed'
            if attempt < attempts:
                time.sleep(0.5)  # 两轮检测间的等待（原 CLICK_INTERVAL=1s，缩短省时）
        return None

    def _recheck_busy_after_nav(self, stage: str) -> str | None:
        """出门后入口导航失败（RuntimeError）时重新检测进行中状态。

        出门后活动面板加载有几秒延迟，wait_busy_end 首轮检测窗口可能错过
        正在上课/打工/冒险/被雇佣的状态——此时点活动入口不会进入准备页，
        导航必然超时（见各场景 goto_*）。导航失败说明屏幕早已稳定，重新
        检测一次：命中则等待结束并回主页面，返回等完的类型（由调用方计数）；
        未命中返回 None，由调用方原样抛出导航异常。
        """
        log(f'{stage}: 导航失败，重新检测进行中状态...')
        finished = self.wait_busy_end()
        if finished:
            self.ensure_main_page()
            return finished
        return None
