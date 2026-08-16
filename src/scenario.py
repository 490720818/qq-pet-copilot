"""场景基类：设备连接、截图、u2/OCR 定位点击、通用导航。"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from .config import find_adb, load_config
from .locators import LOCATORS, ocr_screen
from .locators import see as locate
from .locators import see_all as locate_all
from .ocr import parse_employed_ratio, parse_employed_remaining
from .progress import (
    HIRE_FRIEND_PROGRESS_FILE,
    count_cross,
    increment_progress,
    log,
)
from .u2dev import U2Device

# ---- 可调参数 ----
CLICK_INTERVAL = 1.0       # 连续点击/重试间隔（秒）
NAV_TIMEOUT = 10           # 单个阶段最多重试次数，超过认为卡死抛异常
MAIN_PAGE_ATTEMPTS = 10    # 回主页面最多尝试次数（识别 main_sign / 点 back）
BUSY_GATE_ATTEMPTS = 2     # 出门后进行中状态的检测次数（活动面板加载有几秒延迟）
LEAVE_HOME_ATTEMPTS = 3    # 点击出门失败（点完 main_sign 仍在主页面）时的重试次数
WAIT_LOG_INTERVAL = 300.0  # 长等待期间的心跳日志间隔（秒），避免每轮检测刷屏
ENCOURAGE_LOG_INTERVAL = 300.0  # "鼓励宠物"点击日志节流间隔（秒）：按钮每 ~12s 出现，避免刷屏
EMPLOYED_MAX_WAIT_MINUTES = 45  # 被雇佣"等到25/75（小于45min）"：面板剩余时间超过该值立即召回
DEFER_DETECTION_ATTEMPTS = 3    # 延时收尾模式判定进行中/结束状态的检测次数
DEFER_FALLBACK_SECONDS = 15     # OCR 识别不到剩余时间时的兜底重估间隔（秒）：
# 原本 60s 会让一开始的收尾预估多等近 1 分钟（用户实测收尾偏晚），
# 缩短后尽早复查，OCR 通常下一轮就能读到真实剩余并收敛到准确收尾时间
DEFER_END_MARGIN_SECONDS = 1    # 剩余时间换算收尾时间点时加的余量（秒）
FINISH_DETECTION_ATTEMPTS = 5   # finish_pending 出门后检测结算页/进行中状态的重试次数


class TaskDeferred(Exception):
    """任务主动要求延后调度：到 until 时间再执行（如雇佣好友时发现宠物
    正在打工/学习/冒险/被雇佣中，按剩余时间延后）。与 ScenarioFailed 的
    失败退避语义不同，调度层需单独捕获。"""

    def __init__(self, until: datetime, reason: str = ''):
        super().__init__(reason or f'延后到 {until:%H:%M:%S}')
        self.until = until


class StatBlocked(Exception):
    """开始任务（点 *_start）时体力/清洁不足弹窗：handle_low_stat_dialog 已回
    主页面护理一次，调度器应重试当前任务一次（不算失败、不重启）。与
    TaskDeferred（延后）、ScenarioFailed（失败退避）语义不同：护理已完成，
    立即重试当前任务。"""


# 点 *_start 开始上课/打工/冒险/PK 时，体力/清洁不足弹窗（content-desc 整句，u2 定位）
LOW_STAT_DIALOGS = {
    'pet_low_energy': '你的宠物体力不足，请回家补充体力',
    'pet_low_clean': '你的宠物清洁值不足，请回家洗澡',
}


class DeviceScenario:
    """各场景共用：截图、u2 控件/OCR 文字定位、点击/拖动、回主页面。"""

    def __init__(self, dev: U2Device | None = None):
        self.cfg = load_config()
        if dev is None:
            dev = U2Device(find_adb(self.cfg.adb.path), self.cfg.adb.device_serial)
        self.dev = dev
        # 延时收尾模式（任务队列调度器对主任务开启）：进行中不阻塞等待，
        # OCR 剩余时间登记 self.pending 后先回主页面调度其他任务，到点由
        # finish_pending() 收尾计数。legacy 引擎保持 False（原阻塞等待）。
        self.defer_wait = False
        self.pending = None  # dict: in_name/end_name/until(datetime)/on_finish/desc/encourage

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
            if click_name.endswith('_start'):
                # 点开始按钮可能触发"体力/清洁不足"弹窗：识别 _start 的同时同帧
                # 检测弹窗，命中则回主页面护理一次并抛 StatBlocked，由调度器
                # 重试当前任务（见 handle_low_stat_dialog）
                self.handle_low_stat_dialog(source)
            target = self.see(click_name, None, source)
            if target and wait_ocr_only:
                # OCR wait：点击目标在就只管点，避免每轮截图+OCR 拖慢点击。
                # 但点击目标可能一直残留/被误匹配（如"开始"按钮在下一页仍在
                # 控件树里）：从第二次起先查 OCR wait，已进入状态就直接返回，
                # 否则会一直重复点击直到超时（见冒险"开始冒险"反复点开始、
                # adventure_in 却早已出现）
                if attempt >= 2:
                    hit = self.see(wait_name, None, source)
                    if hit:
                        log(f'{stage}: 已出现 {wait_name} (score={hit[2]:.2f})')
                        return
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

    def go_back(self, screen=None, source=None) -> bool:
        """按配置的返回方式（schedule.back_method）回退一页，返回是否执行了返回。

        - 系统返回：直接按 Android 返回键（dev.d.press('back')），始终返回 True；
        - 返回图标（默认）：定位游戏内 back 按钮点击；找不到返回 False，
          由调用方决定重试/放弃（避免页面不在预期时误按系统返回退过头）。
        """
        method = getattr(getattr(self.cfg, 'schedule', None), 'back_method', '系统返回')
        if method == '系统返回':
            self.dev.d.press('back')
            return True
        hit = self.see('back', screen, source)
        if not hit:
            return False
        self.click(hit[0], hit[1])
        return True

    def ensure_main_page(self):
        """确认在主页面；不在则点 back 直到回来，返回主页面控件树快照。

        识别不到 main_sign（金币胶囊，只有自己主页面有，好友宠物页没有）时，
        连续 schedule.main_page_checks 次（默认 1 = 立即点 back）识别都失败
        才允许点 back（主页面点 back 会退出游戏，需要宽限防识别抖动误退）。
        返回的 source 可直接给同一个页面上的后续 XPath 定位复用。
        """
        checks = max(1, int(getattr(self.cfg.schedule, 'main_page_checks', 1) or 1))
        max_attempts = MAIN_PAGE_ATTEMPTS * checks
        misses = 0
        for attempt in range(1, max_attempts + 1):
            screen, source = self.snapshot()
            hit = self.see('main_sign', screen, source)
            if hit:
                log(f'已在主页面 (score={hit[2]:.2f})')
                return source
            misses += 1
            if misses < checks:
                # 未达检测次数：等一下重新识别（页面可能还在加载/识别抖动）
                time.sleep(CLICK_INTERVAL)
                continue
            misses = 0
            if self.go_back(screen, source):
                log('未识别到主页面'
                    + (f'（连续 {checks} 次）' if checks > 1 else '')
                    + '，执行返回')
                continue
            if attempt == 1 or attempt == max_attempts:
                log(f'未识别到主页面也找不到 back，等待重试 ({attempt}/{max_attempts})')
            time.sleep(CLICK_INTERVAL)
        raise RuntimeError('无法回到主页面')

    def handle_low_stat_dialog(self, source=None) -> None:
        """点 *_start 开始任务时，同时检测"体力/清洁不足"弹窗（content-desc 整句）。

        命中：点 back 关掉弹窗 -> 回主页面 -> 护理一次（同调度器的护理检查）-> 抛
        StatBlocked，由调度器 run_one 捕获后立即重试当前任务一次（不算失败、不重启）。
        未命中返回 None，调用方继续原流程。
        """
        if source is None:
            source = self.dev.hierarchy()
        for key, desc in LOW_STAT_DIALOGS.items():
            hit = self.see(key, None, source)
            if not hit:
                continue
            log(f'检测到"{desc}"弹窗，回主页面护理一次后重试当前任务')
            # 弹窗可能盖住主页面元素（main_sign 在弹窗下层仍可能被 xpath 命中，
            # 直接 ensure_main_page 会误判已在主页面），先点一次 back 关掉弹窗
            self.go_back(None, source)
            time.sleep(CLICK_INTERVAL)
            self.ensure_main_page()
            self.care_once()
            raise StatBlocked()

    def care_once(self) -> None:
        """主页面执行一次护理检查（同调度器的护理：体力/清洁不足则喂食/洗澡，
        或一键护理），护理完成停在主页面。"""
        from scenarios.care import CareScenario

        CareScenario(self.dev).check_and_care()

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
        """主页面点击出门（OCR 定位"出门"，识别不到由注册表用参考坐标兜底）。

        点完检测一次 main_sign（金币胶囊，只有主页面有）：仍识别到 = 还在主页面 =
        点击失败/没生效，重试点击；最多 LEAVE_HOME_ATTEMPTS 次仍失败抛异常，
        由调用方走回主页面重试/恢复链路。"""
        for attempt in range(1, LEAVE_HOME_ATTEMPTS + 1):
            hit = self.see('leave_home')
            if hit:
                self.click(hit[0], hit[1])
            else:
                log(f'未定位到"出门"按钮（{attempt}/{LEAVE_HOME_ATTEMPTS}）')
            time.sleep(CLICK_INTERVAL)
            # 出门成功 = 主页面标志消失；还在主页面说明没点中/没生效
            if not self.see('main_sign'):
                return
            # 页面切换动画可能让 main_sign 短暂残留，多等一轮再判定
            time.sleep(CLICK_INTERVAL)
            if not self.see('main_sign'):
                return
            log(f'点击出门后仍在主页面，重试点击出门（{attempt}/{LEAVE_HOME_ATTEMPTS}）')
        raise RuntimeError(f'点击出门 {LEAVE_HOME_ATTEMPTS} 次后仍停留在主页面')

    def wait_end(self, in_name: str, end_name: str, check_interval: float | None = None,
                 encourage: bool = False) -> None:
        """等待 end_name 出现并点 quit 退出，期间点 in_name 画面防设备休眠。

        encourage=True 时每轮顺带检测"鼓励宠物"按钮（d(description="鼓励宠物")），
        出现就点一下（学习等待期间提升心情/互动收益）；检测到结束标志后、点 quit 前
        再按 schedule.encourage_times 快速点够鼓励次数（非阻塞调度的鼓励主要靠
        defer_busy_end/_defer_busy 登记 pending 时在进行中页面就地点击，结算页没有该按钮）。
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
        if encourage:
            # 结束前再快速点够鼓励次数（等待期间已逐次点过，补一轮无妨；
            # 非阻塞调度的鼓励主要靠 defer_busy_end/_defer_busy 登记 pending 时
            # 在进行中页面就地点击）
            self._encourage_burst()
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

    def employed_recall_ready(self, screen) -> bool:
        """单次判定被雇佣是否到召回时机（不等待）：
        立刻召回 总是召回；等到25/75（小于45min）剩余时间 >45 分钟直接召回；
        三种方式都在分成比例到"雇佣者<=25% 被雇佣者>=75%"终态时召回。"""
        action = getattr(self.cfg.employed, 'action', '等到25/75')
        if action == '立刻召回':
            log('按配置"立刻召回"被雇佣宠物')
            return True
        if action == '等到25/75（小于45min）':
            rem = self.see_employed_remaining(screen)
            if rem is not None:
                secs, _x, _y, _score = rem
                if secs > EMPLOYED_MAX_WAIT_MINUTES * 60:
                    h, m, ss = secs // 3600, (secs % 3600) // 60, secs % 60
                    log(f'剩余时间 {h:02d}:{m:02d}:{ss:02d}'
                        f'（>{EMPLOYED_MAX_WAIT_MINUTES} 分钟），立即召回')
                    return True
        sign = self.see_employed_sign(screen)
        if sign:
            log(f'检测到召回标志（分成比例终态） (score={sign[2]:.2f})')
            return True
        return False

    def _recall_employed(self) -> None:
        """召回被雇佣宠物：点"现在召回"，处理确认按钮，等 employed_end 点 quit 并计数。"""
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

    def wait_employed_back(self, check_interval: float | None = None) -> None:
        """被雇佣中：按配置 employed.action 决定召回时机（阻塞等到召回条件满足）。

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
        last_log_at = 0.0
        while True:
            # 本循环全是 OCR 定位（剩余时间/分成比例/被雇佣中），
            # 不需要控件树快照（dump 一次 1~4s），只截图即可
            screen = self.screen()
            if self.employed_recall_ready(screen):
                break
            # 点击一次被雇佣画面防止设备休眠
            cur = self.see('employed_in', screen)
            if cur:
                self.dev.click(cur[0], cur[1])  # 防休眠点击不记日志
            now = time.monotonic()
            if now - last_log_at >= WAIT_LOG_INTERVAL:
                log('仍在被雇佣中...')
                last_log_at = now
            time.sleep(check_interval)
        self._recall_employed()

    def read_remaining_seconds(self, screen=None) -> int | None:
        """OCR 整屏"剩余 HH:MM:SS"倒计时（学习/打工/冒险/被雇佣面板通用），
        返回剩余秒数或 None。"""
        if screen is None:
            screen = self.screen()
        rem = parse_employed_remaining(ocr_screen(screen))
        return rem[0] if rem else None

    def detect_busy_remaining(self, attempts: int = BUSY_GATE_ATTEMPTS) -> tuple[str, int] | None:
        """出门检测宠物是否正在打工/学习/冒险/被雇佣中（不等待、不收尾）：
        命中进行中状态则 OCR 剩余时间，返回 (kind, 剩余秒数)；OCR 不到
        剩余时间按 DEFER_FALLBACK_SECONDS 兜底。四种状态都未命中返回 None。
        用于雇佣好友等场景在出发前主动延后，与 wait_busy_end 的阻塞等待互补。
        调用前需已在主页面（内部直接 leave_home）。
        """
        self.leave_home()
        time.sleep(0.5)  # 出门后活动面板有几秒加载延迟
        signs = {'school_in': 'school', 'work_in': 'work',
                 'adventure_in': 'adventure', 'employed_in': 'employed'}
        for attempt in range(1, attempts + 1):
            screen = self.screen()
            for sign, kind in signs.items():
                if self.see(sign, screen):
                    secs = self.read_remaining_seconds(screen)
                    if secs is None:
                        secs = DEFER_FALLBACK_SECONDS
                        log(f'检测到{kind}进行中，未识别到剩余时间，按兜底 {secs} 秒延后')
                    return kind, secs
            if attempt < attempts:
                time.sleep(0.5)
        return None

    def defer_busy_end(self, in_name: str, end_name: str, on_finish, desc: str,
                       encourage: bool = False) -> bool:
        """延时收尾（非阻塞等待）：检测到进行中就 OCR 剩余时间登记 pending，
        返回 True（调用方回主页面，到点由 finish_pending() 收尾计数）；
        检测到已结束则原地走 wait_end 收尾并 on_finish() 计数，返回 False；
        两种状态都检测不到抛 RuntimeError（页面异常，走场景重试）。

        OCR 识别不到剩余时间按 DEFER_FALLBACK_SECONDS 兜底，避免卡死。
        """
        for attempt in range(1, DEFER_DETECTION_ATTEMPTS + 1):
            screen, source = self.snapshot()
            if self.see(end_name, screen, source):
                log(f'{desc}: 已出现结束标志 {end_name}，直接收尾')
                self.wait_end(in_name, end_name, self.check_interval, encourage)
                on_finish()
                return False
            if self.see(in_name, screen, source):
                if encourage:
                    # 鼓励按钮只在进行中页面常驻（结算页没有），登记 pending 离开前就地点击
                    self._encourage_burst()
                secs = self.read_remaining_seconds(screen)
                if secs is None:
                    secs = DEFER_FALLBACK_SECONDS
                    log(f'{desc}: 未识别到剩余时间，按兜底 {secs} 秒后再来收尾')
                until = datetime.now() + timedelta(seconds=secs + DEFER_END_MARGIN_SECONDS)
                self.pending = {'in_name': in_name, 'end_name': end_name, 'until': until,
                                'on_finish': on_finish, 'desc': desc, 'encourage': encourage}
                log(f'{desc}: 进行中，预计 {secs} 秒后结束（{until:%H:%M:%S} 收尾），先调度其他任务')
                return True
            if attempt < DEFER_DETECTION_ATTEMPTS:
                time.sleep(CLICK_INTERVAL)
        raise RuntimeError(f'{desc}: 未检测到进行中或结束状态')

    def _encourage_burst(self) -> None:
        """在"鼓励宠物"按钮可见的页面上快速点击 schedule.encourage_times 次。

        按钮在学习/打工进行中页面常驻，结算页实测没有（日志连续 0/50），
        所以非阻塞调度（defer_wait）在登记 pending（正停在进行中页面）时就地点击；
        结算页路径保留调用兜底。按钮不是一直渲染，每轮 see 不到就短等重试，
        最多 times*3 轮防卡死。
        """
        times = int(getattr(self.cfg.schedule, 'encourage_times', 0) or 0)
        if times <= 0:
            return
        clicked = 0
        # 先找一次按钮位置（see 是全树 dump ~0.8s，按钮位置固定；找到后单轮连点
        # 不再重查。找不到短等重试几次仍没有就放弃——按钮可能不在当前页/未渲染）
        hit = None
        for _ in range(3):
            hit = self.see('encourage_pet')
            if hit:
                break
            time.sleep(0.1)
        if not hit:
            log(f'鼓励宠物: 未找到按钮，跳过（0/{times} 次）')
            return
        x, y = hit[0], hit[1]
        for _ in range(times):
            self.click(x, y)
            clicked += 1
            time.sleep(0.1)  # 点击间隔（快速连点）
        log(f'鼓励宠物: 快速点击 {clicked}/{times} 次')

    def finish_pending(self) -> bool:
        """pending 到点收尾：回主页面出门，结算页（学习"教师评语"/打工"打工总结"
        （含雇佣好友名称），即 end_name 的"分享"按钮）在出门页面出现——见结算页点 quit
        （落在出门页面，由后续任务继续）、on_finish() 计数，返回 True；
        活动还在进行中（计时误差）时 pend['encourage'] 则先在进行中页面就地鼓励
        （结算页没有鼓励按钮，鼓励主要靠登记 pending 时的就地点击），重新 OCR 剩余时间
        更新 until 并回主页面，返回 False；
        多轮检测既没结算页也没进行中状态时直接丢弃该 pending（不计数）并返回 True——
        不重估时间（识别不到剩余时间会按 60 秒兜底永远卡在重估循环）；结算页若稍后
        真出现，后续主任务出门时会被 wait_busy_end 的结算检测兜底计数。
        无 pending 时直接返回 True。"""
        pend = self.pending
        if pend is None:
            return True
        # 学习/打工/雇佣好友不停留在进行中页面，下一次主任务调度点出门后
        # 才会出现 end 结算页面：先回主页面再出门
        self.ensure_main_page()
        self.leave_home()
        in_progress = False
        for _attempt in range(FINISH_DETECTION_ATTEMPTS):
            screen, source = self.snapshot()
            if self.see(pend['end_name'], screen, source):
                log(f"{pend['desc']}: 检测到结算页 {pend['end_name']}，收尾")
                if pend.get('encourage'):
                    # 结算页实测没有鼓励按钮（快速 3 轮不中即放弃），仅作兜底
                    self._encourage_burst()
                quit_hit = self.see('quit', screen, source)
                if quit_hit:
                    self.click(quit_hit[0], quit_hit[1])
                    time.sleep(CLICK_INTERVAL)
                    # 点完 quit 已落在"出门"页面：打时间戳标记，同场景下一轮
                    # 可以直接点活动入口，省一次 back + 出门（见 adventure.run）
                    self._after_pending_go_out_at = time.monotonic()
                else:
                    log(f"{pend['desc']}: 未找到 quit 按钮，直接返回")
                self.pending = None
                pend['on_finish']()
                return True
            if self.see(pend['in_name'], screen, source):
                in_progress = True
                break  # 还在进行中（计时误差）：跳出检测，重估收尾时间
            time.sleep(CLICK_INTERVAL)
        if not in_progress:
            # 结算页和进行中状态都没检测到：pending 已陈旧（结算页可能已被其他任务
            # 的出门检测 wait_busy_end/_detect_settlement 顺手收尾计数，或状态丢失）。
            # 丢弃该收尾任务（不计数，避免重复计），继续正常调度
            log(f"{pend['desc']}: 出门后未检测到结算页或进行中状态，"
                f'丢弃该收尾任务（不计数），继续正常调度')
            self.pending = None
            self.ensure_main_page()
            return True
        if pend.get('encourage'):
            # 还在进行中：鼓励按钮在进行中页面常驻，离开前就地点击
            self._encourage_burst()
        secs = self.read_remaining_seconds()
        if secs is None:
            secs = DEFER_FALLBACK_SECONDS
        pend['until'] = datetime.now() + timedelta(seconds=secs + DEFER_END_MARGIN_SECONDS)
        log(f"{pend['desc']}: 尚未结束，重估剩余 {secs} 秒（{pend['until']:%H:%M:%S} 再收尾）")
        self.ensure_main_page()
        return False

    def _defer_busy(self, kind: str, screen) -> bool:
        """出门检测到进行中且 defer_wait 开启：OCR 剩余时间登记 pending 返回 True
        （on_finish 按 kind 交叉计数）；识别不到剩余时间返回 False，回退阻塞等待。
        被雇佣按召回策略处理，不做延时收尾。"""
        if kind == 'employed':
            return False
        secs = self.read_remaining_seconds(screen)
        if secs is None:
            return False
        # 记录 OCR 读取剩余时间的时刻：**必须在鼓励宠物之前**——鼓励点击耗时
        # 不能算进收尾余量（否则估算偏晚：冒险无鼓励所以准，上课/打工有鼓励就晚
        # 鼓励耗时那么多秒）
        read_at = datetime.now()
        names = {'school': ('school_in', 'school_end', '上课'),
                 'work': ('work_in', 'work_end', '打工'),
                 'adventure': ('adventure_in', 'adventure_end', '冒险')}
        in_name, end_name, desc = names[kind]
        if kind in ('school', 'work'):
            # 鼓励按钮只在进行中页面常驻（结算页没有），登记 pending 离开前就地点击
            self._encourage_burst()
        until = read_at + timedelta(seconds=secs + DEFER_END_MARGIN_SECONDS)
        self.pending = {'in_name': in_name, 'end_name': end_name, 'until': until,
                        'on_finish': lambda k=kind: count_cross(k), 'desc': desc,
                        'encourage': kind in ('school', 'work')}
        log(f'检测到正在{desc}，预计 {secs} 秒后结束（{until:%H:%M:%S} 收尾），先调度其他任务')
        return True

    def _detect_settlement(self, screen, source) -> str | None:
        """出门后的结算页检测（上次活动已结束未收尾，如调度器重启丢失 pending 后，
        点出门直接出现结算页）：OCR 文案区分——"教师评语"=学习、"打工总结"=打工
        （文案含配置的雇佣好友名称时再计一次雇佣好友）；都没有但有"分享"按钮
        （adventure_end）=冒险结算。命中返回 'school'/'work'/'adventure'，否则 None。"""
        results = ocr_screen(screen)
        texts = [t for t, *_ in results]
        if any('教师评语' in t for t in texts):
            return 'school'
        if any('打工总结' in t for t in texts):
            hf_name = getattr(getattr(self.cfg, 'hire_friend', None), 'friend_name', '') or ''
            hf_name = hf_name.strip()
            if hf_name and any(hf_name in t for t in texts):
                # 打工总结含雇佣好友名称：同时计一次雇佣好友（打工次数由调用方计）
                n = increment_progress(HIRE_FRIEND_PROGRESS_FILE)
                log(f'打工总结含雇佣好友 {hf_name}，已计入雇佣好友次数（{n} 次）')
            return 'work'
        if self.see('adventure_end', screen, source):
            return 'adventure'
        return None

    def dismiss_career_popup(self, screen=None) -> bool:
        """检测并处理"职业升级/获得新职业"弹窗（出门页/打工面板加载时可能弹出，
        会挡住底层页面）：先连点三次弹窗识别到的坐标（展开/交互），
        再重新截图找对应按钮点击进职业树（职业树无原生返回键），
        连续按系统返回键逐层退出。处理过返回 True（后续导航由调用方决定），
        没有弹窗返回 False。screen 为 None 时重新截图。
        """
        if screen is None:
            screen = self.screen()
        # "你将进阶成为..."（职业升级）或 "神秘人/快去职业树里看看吧"（获得新职业）
        career_upgrading = self.see('career_upgrade', screen)
        career_new_hit = self.see('career_new', screen)
        if not (career_upgrading or career_new_hit):
            return False
        popup_hit = career_upgrading or career_new_hit
        for _tap in range(3):
            self.click(popup_hit[0], popup_hit[1])
            time.sleep(CLICK_INTERVAL)
        # 点三次后界面可能已变化：找按钮必须重新截图（不能复用旧 screen 的 OCR 缓存）。
        # 职业升级按钮是"查看"，获得新职业按钮是"去看看"
        if career_upgrading:
            view = self.see('career_upgrade_view')
            btn_name = '查看'
        else:
            view = self.see('career_new_view')
            btn_name = '去看看'
        if view:
            log(f'检测到{"职业升级" if career_upgrading else "获得新职业"}弹窗，点击"{btn_name}" ({view[0]}, {view[1]})')
            self.click(view[0], view[1])
            time.sleep(CLICK_INTERVAL)
        for _back in range(4):
            self.dev.d.press('back')
            time.sleep(CLICK_INTERVAL)
            if not self.see('career_tree'):
                break
        return True

    def wait_busy_end(self, check_interval: float | None = None,
                      attempts: int = BUSY_GATE_ATTEMPTS) -> str | None:
        """出门后检测是否正在上课/工作/冒险/被雇佣中，是则等待结束并退出；
        若直接出现结算页（上次活动已结束未收尾，如调度器重启丢失 pending 后），
        点 quit 收尾（_detect_settlement：教师评语=学习/打工总结=打工/分享=冒险）。

        返回 'school' / 'work' / 'adventure' / 'employed' / None
        （等完/收尾的是哪种，用于计入对应计数）。
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
            # 职业升级 / 获得新职业弹窗：处理后回主页面重新出门，
            # 重新开始本轮检测，避免挡住进行中状态检测
            if self.dismiss_career_popup(screen):
                self.ensure_main_page()
                self.leave_home()
                continue
            # 结算页（上次活动已结束未收尾）：点 quit 收尾并返回对应类型（计数同
            # 等完活动的语义；"打工总结"含雇佣好友名称时 _detect_settlement 已计雇佣）。
            # xpath 定位（分享/quit）source 传 None 按需 dump（结算页是低频路径）
            settle = self._detect_settlement(screen, None)
            if settle:
                settle_names = {'school': '学习', 'work': '打工', 'adventure': '冒险'}
                log(f'出门后检测到{settle_names[settle]}结算页，点 quit 收尾')
                if settle in ('school', 'work'):
                    # 结算页实测没有鼓励按钮（快速 3 轮不中即放弃），仅作兜底
                    self._encourage_burst()
                quit_hit = self.see('quit', screen)
                if quit_hit:
                    self.click(quit_hit[0], quit_hit[1])
                    time.sleep(CLICK_INTERVAL)
                else:
                    log('结算页未找到 quit 按钮，直接返回')
                return settle
            if self.see('school_in', screen):
                if self.defer_wait and self._defer_busy('school', screen):
                    return 'school'
                log('检测到正在上课，等待这节课结束...')
                self.wait_end('school_in', 'school_end', check_interval, encourage=True)
                return 'school'
            if self.see('work_in', screen):
                if self.defer_wait and self._defer_busy('work', screen):
                    return 'work'
                log('检测到正在打工，等待这次工作结束...')
                self.wait_end('work_in', 'work_end', check_interval, encourage=True)
                return 'work'
            if self.see('adventure_in', screen):
                if self.defer_wait and self._defer_busy('adventure', screen):
                    return 'adventure'
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
