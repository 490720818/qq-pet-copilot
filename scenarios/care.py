"""宠物状态照顾：任务调度前检查体力/清洁，不足则喂食/洗澡。

流程（主页面操作）：
1. 点击 (520, 120) 展开宠物状态
2. OCR 区域 (200,185)-(320,430) 识别 体力/清洁/心情 三个数值
3. 体力低于阈值 -> 喂食：点 feed -> 反复点 feed_10 并复测体力，直到达标
4. 清洁低于阈值 -> 洗澡：点 shower -> 按住不松手（motionevent）从 (365,1073) 拖到
   (365,600)，再在 (365,750) 和 (365,600) 之间来回搓洗，直到清洁达标后抬手
5. 若仍在喂食/洗澡界面（feed_10 / shower_10 可见）-> 点 (365, 450) 退出
6. 点击 (520, 120) 收起宠物状态

运行：python scenarios/care.py   （执行一次检查/照顾）
"""

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2

from src.ocr import ocr_texts
from src.progress import log
from src.scenario import CLICK_INTERVAL, DeviceScenario

# 展开/收起宠物状态的点击位置
STATUS_TOGGLE_POS = (520, 120)
# 状态数值 OCR 区域 (x1, y1, x2, y2)
STATUS_REGION = (200, 185, 320, 430)
# 退出喂食/洗澡状态的点击位置
EXIT_MODE_POS = (365, 450)
# 洗澡：按住从 (365,1073) 拖到 (365,600)，之后在这两点之间来回搓洗
SHOWER_START = (365, 1073, 365, 600)
SHOWER_A = (365, 750)
SHOWER_B = (365, 600)
FEED_RESULT_WAIT = 1.5  # 喂食后等数值刷新的时间（秒）
MAX_FEED_ATTEMPTS = 10    # 喂食最多次数，超过认为异常
MAX_SHOWER_ATTEMPTS = 25  # 搓洗最多回合数，超过认为异常

STATUS_NAMES = ('体力', '清洁', '心情')


def parse_status(results: list[tuple[str, int, int, float]]) -> dict:
    """从状态区域 OCR 结果解析 体力/清洁/心情 数值（0-100）。

    兼容两种 OCR 输出：名字和数字在同一文本块（'体力85'），
    或被拆成两个块（'体力' + '85'，取名字右侧/下方最近的数字）。
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
        right = [n for n in nums if n[1] > ax and abs(n[2] - ay) < 40]
        below = [n for n in nums if n[2] > ay and abs(n[1] - ax) < 80]
        pick = right or below
        if pick:
            pick.sort(key=lambda n: (n[1] - ax) ** 2 + (n[2] - ay) ** 2)
            status[name] = pick[0][0]
    return status


class CareScenario(DeviceScenario):
    def __init__(self, dev=None):
        super().__init__(dev)
        self.energy_threshold = self.cfg.care.energy_threshold
        self.clean_threshold = self.cfg.care.clean_threshold
        log(f'体力阈值: {self.energy_threshold}，清洁阈值: {self.clean_threshold}')

    # ---- 状态识别 ----

    def read_status(self) -> dict:
        """OCR 状态区域，返回 {'体力': n, '清洁': n, '心情': n}（识别不到的缺省）。"""
        screen = self.screen()
        x1, y1, x2, y2 = STATUS_REGION
        crop = screen[y1:y2, x1:x2]
        crop = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        results = ocr_texts(crop)
        log('状态区域 OCR: '
            + (', '.join(f'{t!r}@({x},{y})' for t, x, y, _ in results) or '无'))
        return parse_status(results)

    # ---- 照顾动作 ----

    def feed(self) -> None:
        """喂食：点 feed -> 反复点 feed_10 并复测体力，直到达到阈值。"""
        hit = self.see('feed')
        if not hit:
            raise RuntimeError('未找到 feed 喂食按钮')
        self.click(hit[0], hit[1])
        time.sleep(CLICK_INTERVAL)
        for attempt in range(1, MAX_FEED_ATTEMPTS + 1):
            btn = self.see('feed_10')
            if not btn:
                raise RuntimeError('喂食界面未找到 feed_10 按钮')
            self.click(btn[0], btn[1])
            time.sleep(FEED_RESULT_WAIT)
            energy = self.read_status().get('体力')
            log(f'第 {attempt} 次喂食后体力: {energy}')
            if energy is not None and energy >= self.energy_threshold:
                log(f'体力已达标（>= {self.energy_threshold}）')
                return
        raise RuntimeError(f'喂食 {MAX_FEED_ATTEMPTS} 次后体力仍未达到 {self.energy_threshold}')

    def scrub_path(self, x: int, y_from: int, y_to: int,
                   steps: int = 5, step_sleep: float = 0.05) -> None:
        """按住状态下从 (x, y_from) 匀速拖 MOVE 到 (x, y_to)（一次 adb 调用）。"""
        points = [(x, y_from + (y_to - y_from) * i // steps) for i in range(1, steps + 1)]
        self.dev.motion_path(points, step_sleep)

    def shower(self) -> None:
        """洗澡：点 shower -> 按住不松手从 (365,1073) 拖到 (365,600)，
        再在 (365,750) 和 (365,600) 之间来回搓洗，直到清洁达到阈值后抬手。

        用 input motionevent 分步注入 DOWN/MOVE/UP，整个搓洗过程不抬手
        （普通 input swipe 每次都会抬手，游戏不累计清洁度）。
        """
        hit = self.see('shower')
        if not hit:
            raise RuntimeError('未找到 shower 洗澡按钮')
        self.click(hit[0], hit[1])
        time.sleep(CLICK_INTERVAL)
        log('按住开始搓洗')
        self.dev.motion_event('DOWN', *SHOWER_START[:2])
        try:
            # 从 (365,1073) 慢速拖到 (365,600) 进入搓洗
            self.scrub_path(365, SHOWER_START[1], SHOWER_START[3])
            for attempt in range(1, MAX_SHOWER_ATTEMPTS + 1):
                # 在 (365,750) 和 (365,600) 之间来回拖（截图复测不影响按压）
                self.scrub_path(365, SHOWER_A[1], SHOWER_B[1])
                self.scrub_path(365, SHOWER_B[1], SHOWER_A[1])
                clean = self.read_status().get('清洁')
                log(f'搓洗 {attempt} 回合后清洁: {clean}')
                if clean is not None and clean >= self.clean_threshold:
                    log(f'清洁已达标（>= {self.clean_threshold}）')
                    return
            raise RuntimeError(f'搓洗 {MAX_SHOWER_ATTEMPTS} 回合后清洁仍未达到 {self.clean_threshold}')
        finally:
            self.dev.motion_event('UP', *SHOWER_A)

    def exit_care_mode(self) -> None:
        """若仍在喂食/洗澡界面（feed_10 / shower_10 可见），点 (365,450) 退出。"""
        for _ in range(5):
            if self.see('feed_10') or self.see('shower_10'):
                self.click(*EXIT_MODE_POS)
                time.sleep(CLICK_INTERVAL)
            else:
                return
        log('警告: 多次点击后仍未退出喂食/洗澡状态')

    # ---- 主流程 ----

    def check_and_care(self) -> None:
        """检查一次体力/清洁，低于阈值则喂食/洗澡，最后收起状态面板。"""
        self.ensure_main_page()
        self.click(*STATUS_TOGGLE_POS)
        time.sleep(CLICK_INTERVAL)
        status = self.read_status()
        log(f'宠物状态: 体力={status.get("体力")} '
            f'清洁={status.get("清洁")} 心情={status.get("心情")}')
        energy = status.get('体力')
        if energy is not None and energy < self.energy_threshold:
            log(f'体力 {energy} < 阈值 {self.energy_threshold}，需要喂食')
            self.feed()
        clean = status.get('清洁')
        if clean is not None and clean < self.clean_threshold:
            log(f'清洁 {clean} < 阈值 {self.clean_threshold}，需要洗澡')
            self.shower()
        self.exit_care_mode()
        self.click(*STATUS_TOGGLE_POS)
        time.sleep(CLICK_INTERVAL)
        log('状态检查完成，已收起宠物状态')


if __name__ == '__main__':
    try:
        CareScenario().check_and_care()
    except KeyboardInterrupt:
        log('手动停止')
