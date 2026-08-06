"""冒险场景。

流程（u2 控件/OCR 文字定位，分辨率无关）：
1. 主页面（main_sign="出门"）-> 点击出门
2. 出门后若正在上课/工作/冒险/被雇佣中（school_in / work_in / adventure_in / employed_in）
   -> 等待结束并退出，回主页面结束本轮
3. 点击 adventure 进入准备页面，直到出现 adventure_start 按钮
4. 点击 adventure_start 开始冒险，直到出现 adventure_in 标志；
   配置 adventure.skip_bad_weather 开启时，开始 5 秒后 OCR 冒险详情框：
   含"天色不对"则点"召回"->"确认召回"，计入一次冒险，不再等冒险结束
5. 冒险中按配置的检查间隔（schedule.check_interval）检查，直到出现 adventure_end 标志
6. 退出并退回主页面，重新开始

运行：python scenarios/adventure.py            （Ctrl+C 停止）
      python scenarios/adventure.py --times 5  （冒满 5 次自动结束，0 为不限）
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.locators import see_bounds
from src.ocr import find_text, ocr_texts
from src.progress import (
    ADVENTURE_PROGRESS_FILE,
    count_cross,
    load_progress,
    log,
    log_history,
    save_progress,
)
from src.scenario import CLICK_INTERVAL, DeviceScenario, NAV_TIMEOUT

BAD_WEATHER_WAIT = 5.0    # 开始冒险后等冒险详情框加载的时间（秒）
BAD_WEATHER_KEYWORD = '天色不对'
RECALL_KEYWORD = '召回'

PROGRESS_FILE = ADVENTURE_PROGRESS_FILE


class AdventureScenario(DeviceScenario):
    def __init__(self, dev=None):
        super().__init__(dev)
        self.times_per_day = self.cfg.adventure.times_per_day
        self.skip_bad_weather = self.cfg.adventure.skip_bad_weather
        log(f'每天冒险次数: {self.times_per_day if self.times_per_day else "不冒险"}'
            f'，跳过"天色不对": {"开" if self.skip_bad_weather else "关"}')

    # ---- 各阶段 ----

    def goto_adventure(self) -> str | None:
        """主页面 -> 出门 -> 点 adventure 直到出现 adventure_start（准备页面）。

        出门后若正在上课/工作/冒险中（上次中途停止），等待结束并退出、回主页面，
        返回等完的是哪种（'school' / 'work' / 'adventure' / 'employed'）——此时不再继续，
        由调用方/执行器重新判断后再决定下一步；正常情况返回 None。
        """
        self.leave_home()
        finished = self.wait_busy_end()
        if finished:
            self.ensure_main_page()
            return finished
        try:
            self.click_until_gone_or_see('adventure', 'adventure_start', '前往冒险')
        except RuntimeError:
            # 正在打工/上课等时点冒险入口不会进入准备页，导航必然超时；
            # 屏幕早已稳定，重新检测一次进行中状态再下结论
            finished = self._recheck_busy_after_nav('前往冒险')
            if finished:
                return finished
            raise
        return None

    def do_adventure(self) -> None:
        """准备页面 -> 开始冒险 -> 等待 adventure_end -> 点 quit 退出。
        开关开启时开始后先检测"天色不对"：命中则召回并确认，不再等冒险结束
        （调用方照常计入一次冒险）。"""
        self.click_until_gone_or_see('adventure_start', 'adventure_in', '开始冒险')
        if self.skip_bad_weather and self.recall_bad_weather():
            return
        log('已开始冒险，等待结束...')
        self.wait_end('adventure_in', 'adventure_end')

    def recall_bad_weather(self) -> bool:
        """开始冒险 BAD_WEATHER_WAIT 秒后 OCR 冒险详情框：
        含"天色不对"则按区域内"召回"文字的坐标点击，再点"确认召回"，返回 True；
        不含返回 False（照常冒险）。"""
        time.sleep(BAD_WEATHER_WAIT)
        bounds = see_bounds(self.dev, 'adventure_detail')
        if not bounds:
            log('未定位到冒险详情框，跳过天色检测')
            return False
        x1, y1, x2, y2 = bounds
        screen = self.screen()
        results = ocr_texts(screen[y1:y2, x1:x2])
        log('冒险详情框 OCR: '
            + (', '.join(f'{t!r}@({x},{y})' for t, x, y, _ in results) or '无'))
        if not any(BAD_WEATHER_KEYWORD in text for text, *_ in results):
            return False
        # OCR 坐标是裁剪图内的，点击要加回区域左上角偏移
        recall = find_text(results, RECALL_KEYWORD)
        if not recall:
            raise RuntimeError('检测到"天色不对"但未找到召回按钮')
        log(f'检测到"天色不对"，点击召回 ({x1 + recall[0]}, {y1 + recall[1]})')
        self.click(x1 + recall[0], y1 + recall[1])
        time.sleep(CLICK_INTERVAL)
        for attempt in range(1, NAV_TIMEOUT + 1):
            confirm = self.see('adventure_recall_confirm')
            if confirm:
                self.click(confirm[0], confirm[1])
                time.sleep(CLICK_INTERVAL)
                return True
            log(f'未找到确认召回按钮，等待重试 ({attempt}/{NAV_TIMEOUT})')
            time.sleep(CLICK_INTERVAL)
        raise RuntimeError('点击召回后未出现"确认召回"按钮')

    def run(self, max_times: int | None = None, max_rounds: int = 0) -> bool:
        """max_times: 当天冒险次数上限，0 表示不限；None 表示用配置值。
        max_rounds: 最多跑多少轮后返回，0 为不限。
        一轮 = 一次冒险，结束后回主页面（供执行器逐次调度）。
        返回本次调用是否完成了至少一次冒险。
        """
        if max_times is None:
            max_times = self.times_per_day
        today, done, history = load_progress(PROGRESS_FILE)
        log_history(history, today)
        start_done = done
        if max_times and done >= max_times:
            log(f'今天已冒险满 {max_times} 次，无需再冒险')
            return False
        round_no = 0
        while True:
            round_no += 1
            log(f'===== 第 {round_no} 轮 =====')
            self.ensure_main_page()
            finished = self.goto_adventure()
            if finished:
                if finished == 'adventure':
                    # 出门时等完了一次上次未结束的冒险，计入当天次数
                    done += 1
                    save_progress(PROGRESS_FILE, today, done, history)
                    log(f'已完成第 {done} 次冒险' + (f' / 目标 {max_times} 次' if max_times else ''))
                    if max_times and done >= max_times:
                        log('达到当天冒险次数，结束')
                        return True
                elif finished != 'employed':
                    # 出门时等完的是别的活动（上课/打工），计入对应次数
                    # （被雇佣在召回点 quit 时已计数）
                    count_cross(finished)
                # 等完了一次活动，计数已变化，本轮结束，
                # 回主页面交由执行器重新判断限制条件
                if max_rounds and round_no >= max_rounds:
                    return True
                log('本轮结束，回主页面重新开始')
                continue
            self.do_adventure()
            # 检测到 adventure_end 点完 quit 就计数，然后再回主页面
            done += 1
            save_progress(PROGRESS_FILE, today, done, history)
            log(f'已完成第 {done} 次冒险' + (f' / 目标 {max_times} 次' if max_times else ''))
            self.ensure_main_page()
            if max_times and done >= max_times:
                log('达到当天冒险次数，结束')
                return True
            if max_rounds and round_no >= max_rounds:
                log(f'已跑完 {max_rounds} 轮，返回')
                return done > start_done
            log('本轮结束，回主页面重新开始')


if __name__ == '__main__':
    import argparse

    ap = argparse.ArgumentParser(description='冒险场景')
    ap.add_argument('--times', type=int, default=None,
                    help='当天冒险次数上限，0 为不限；不指定则读 config.yaml 的 adventure.times_per_day')
    args = ap.parse_args()

    try:
        AdventureScenario().run(max_times=args.times)
    except KeyboardInterrupt:
        log('手动停止')
