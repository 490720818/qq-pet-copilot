"""场景基类：设备连接、截图、模板点击、通用导航。"""
from __future__ import annotations

import time

from .adb.device import Device
from .config import find_adb, load_config
from .progress import count_cross, log
from .vision import find, png_to_bgr

# ---- 可调参数 ----
THRESHOLD = 0.8            # 模板匹配阈值
CLICK_INTERVAL = 1.0       # 连续点击/重试间隔（秒）
NAV_TIMEOUT = 60           # 单个阶段最多重试次数，超过认为卡死抛异常


# 主页面出门按钮的固定坐标
LEAVE_HOME_POS = (359, 1103)
# 连续多少次未识别到 main_sign 才允许点 back
# （主页面点 back 会直接退出游戏；状态面板动画等会导致 main_sign 暂时匹配不上）
BACK_GRACE_ATTEMPTS = 3


class DeviceScenario:
    """各场景共用：截图、模板识别、点击/拖动、回主页面。"""

    def __init__(self, dev: Device | None = None):
        self.cfg = load_config()
        if dev is None:
            dev = Device(find_adb(self.cfg.adb.path), self.cfg.adb.device_serial)
            log(f'设备在线: {dev.ensure_connected()}')
        self.dev = dev

    def screen(self):
        return png_to_bgr(self.dev.screenshot())

    def see(self, template: str):
        """当前屏幕是否能看到模板，返回 (x, y, score) 或 None。"""
        return find(self.screen(), template, THRESHOLD)

    def click(self, x: int, y: int) -> None:
        log(f'点击 ({x}, {y})')
        self.dev.tap(x, y)

    def swipe(self, x1: int, y1: int, x2: int, y2: int) -> None:
        log(f'拖动 ({x1}, {y1}) -> ({x2}, {y2})')
        self.dev.swipe(x1, y1, x2, y2)

    def click_until_gone_or_see(self, click_tpl: str, wait_tpl: str, stage: str) -> None:
        """每隔 CLICK_INTERVAL 秒点击 click_tpl，直到看见 wait_tpl。"""
        for attempt in range(1, NAV_TIMEOUT + 1):
            hit = self.see(wait_tpl)
            if hit:
                log(f'{stage}: 已出现 {wait_tpl} (score={hit[2]:.2f})')
                return
            target = self.see(click_tpl)
            if target:
                self.click(target[0], target[1])
            else:
                log(f'{stage}: 未找到 {click_tpl}，等待重试 ({attempt}/{NAV_TIMEOUT})')
            time.sleep(CLICK_INTERVAL)
        raise RuntimeError(f'{stage}: 重试 {NAV_TIMEOUT} 次仍未出现 {wait_tpl}')

    def ensure_main_page(self) -> None:
        """确认在主页面；不在则点 back 直到回来。

        主页面点 back 会直接退出游戏，所以先重试几轮确认真的不在主页面，
        连续 BACK_GRACE_ATTEMPTS 次都识别不到 main_sign 才点 back。
        """
        for attempt in range(1, NAV_TIMEOUT + 1):
            screen = self.screen()
            hit = find(screen, 'main_sign', THRESHOLD)
            if hit:
                log(f'已在主页面 (score={hit[2]:.2f})')
                return
            # 低阈值再测一次，打出实际匹配分辅助诊断
            near = find(screen, 'main_sign', 0.5)
            score_hint = f'（main_sign 最高匹配 {near[2]:.2f}）' if near else ''
            if attempt < BACK_GRACE_ATTEMPTS:
                log(f'未识别到主页面标志{score_hint}，等待重试 ({attempt}/{NAV_TIMEOUT})')
            else:
                back = find(screen, 'back', THRESHOLD)
                if back:
                    log(f'连续 {attempt} 次未识别到主页面{score_hint}，'
                        f'点击 back (score={back[2]:.2f})')
                    self.click(back[0], back[1])
                else:
                    log(f'未识别到主页面也找不到 back{score_hint}，'
                        f'等待重试 ({attempt}/{NAV_TIMEOUT})')
            time.sleep(CLICK_INTERVAL)
        raise RuntimeError('无法回到主页面')

    def leave_home(self) -> None:
        """主页面点击出门（固定坐标）。"""
        self.click(*LEAVE_HOME_POS)
        time.sleep(CLICK_INTERVAL)

    def wait_end(self, in_tpl: str, end_tpl: str, check_interval: float) -> None:
        """等待 end_tpl 出现并点 quit 退出，期间点 in_tpl 画面防设备休眠。"""
        while True:
            hit = self.see(end_tpl)
            if hit:
                log(f'检测到结束标志 {end_tpl} (score={hit[2]:.2f})')
                break
            cur = self.see(in_tpl)
            if cur:
                self.click(cur[0], cur[1])
            log('仍在进行中...')
            time.sleep(check_interval)
        quit_hit = self.see('quit')
        if quit_hit:
            self.click(quit_hit[0], quit_hit[1])
            time.sleep(CLICK_INTERVAL)
        else:
            log('未找到 quit 按钮，直接返回')

    def wait_employed_back(self, check_interval: float = 15.0) -> None:
        """被雇佣中：每 15 秒识别一次，出现 employed_sign 就点 employed_come_back
        提前召回，再点 employed_come_back_confirm 确认；
        确认后等待 employed_end 出现，点 quit 退出并计入被雇佣次数。"""
        while True:
            sign = self.see('employed_sign')
            if sign:
                log(f'检测到召回标志 employed_sign (score={sign[2]:.2f})')
                for attempt in range(1, NAV_TIMEOUT + 1):
                    # 先查确认按钮：召回点击后 confirm 可能延迟弹出，
                    # 此时 come_back 已消失，只查 come_back 会永远等不到
                    confirm = self.see('employed_come_back_confirm')
                    if confirm:
                        self.click(confirm[0], confirm[1])
                        time.sleep(CLICK_INTERVAL)
                        break
                    back = self.see('employed_come_back')
                    if back:
                        self.click(back[0], back[1])
                    else:
                        log(f'未找到召回/确认按钮，重试 ({attempt}/{NAV_TIMEOUT})')
                    time.sleep(CLICK_INTERVAL)
                else:
                    raise RuntimeError('出现 employed_sign 但召回/确认按钮未找到')
                # 确认后等待结束界面，点 quit 退出并计数
                for attempt in range(1, NAV_TIMEOUT + 1):
                    end = self.see('employed_end')
                    if end:
                        log(f'检测到被雇佣结束标志 employed_end (score={end[2]:.2f})')
                        break
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
            log('仍在被雇佣中...')
            time.sleep(check_interval)

    def wait_busy_end(self, check_interval: float = 30.0) -> str | None:
        """出门后检测是否正在上课/工作/冒险/被雇佣中，是则等待结束并退出。

        返回 'school' / 'work' / 'adventure' / 'employed' / None
        （等完的是哪种，用于计入对应计数）。
        """
        if self.see('school_in'):
            log('检测到正在上课，等待这节课结束...')
            self.wait_end('school_in', 'school_end', check_interval)
            return 'school'
        if self.see('work_in'):
            log('检测到正在工作，等待这次工作结束...')
            self.wait_end('work_in', 'work_end', check_interval)
            return 'work'
        if self.see('adventure_in'):
            log('检测到正在冒险，等待这次冒险结束...')
            self.wait_end('adventure_in', 'adventure_end', check_interval)
            return 'adventure'
        if self.see('employed_in'):
            log('检测到被雇佣中，等待召回...')
            self.wait_employed_back()
            return 'employed'
        return None
