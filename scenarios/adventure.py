"""冒险场景。

流程（u2 控件/OCR 文字定位，分辨率无关）：
1. 主页面（main_sign="出门"）-> 点击出门
2. 出门后若正在上课/工作/冒险/被雇佣中（school_in / work_in / adventure_in / employed_in）
   -> 等待结束并退出，回主页面结束本轮
3. 点击 adventure 进入准备页面，直到出现 adventure_start 按钮
4. 点击 adventure_start 开始冒险，直到出现 adventure_in 标志
5. 冒险中每 15 秒检查一次，直到出现 adventure_end 标志
6. 退出并退回主页面，重新开始

运行：python scenarios/adventure.py            （Ctrl+C 停止）
      python scenarios/adventure.py --times 5  （冒满 5 次自动结束，0 为不限）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.progress import (
    ADVENTURE_PROGRESS_FILE,
    count_cross,
    load_progress,
    log,
    log_history,
    save_progress,
)
from src.scenario import DeviceScenario

ADVENTURE_CHECK_INTERVAL = 15.0  # 冒险中检查 adventure_end 的间隔（秒）

PROGRESS_FILE = ADVENTURE_PROGRESS_FILE


class AdventureScenario(DeviceScenario):
    def __init__(self, dev=None):
        super().__init__(dev)
        self.times_per_day = self.cfg.adventure.times_per_day
        log(f'每天冒险次数: {self.times_per_day if self.times_per_day else "不冒险"}')

    # ---- 各阶段 ----

    def goto_adventure(self) -> str | None:
        """主页面 -> 出门 -> 点 adventure 直到出现 adventure_start（准备页面）。

        出门后若正在上课/工作/冒险中（上次中途停止），等待结束并退出、回主页面，
        返回等完的是哪种（'school' / 'work' / 'adventure' / 'employed'）——此时不再继续，
        由调用方/执行器重新判断后再决定下一步；正常情况返回 None。
        """
        self.leave_home()
        finished = self.wait_busy_end(ADVENTURE_CHECK_INTERVAL)
        if finished:
            self.ensure_main_page()
            return finished
        self.click_until_gone_or_see('adventure', 'adventure_start', '前往冒险')
        return None

    def do_adventure(self) -> None:
        """准备页面 -> 开始冒险 -> 等待 adventure_end -> 点 quit 退出。"""
        self.click_until_gone_or_see('adventure_start', 'adventure_in', '开始冒险')
        log('已开始冒险，等待结束...')
        self.wait_end('adventure_in', 'adventure_end', ADVENTURE_CHECK_INTERVAL)

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
