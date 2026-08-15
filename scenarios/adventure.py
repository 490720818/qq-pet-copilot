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
6. 一轮连跑 ADVENTURE_BATCH（默认 12）次冒险，期间不回主页面；
   跑满（或达当天上限）后回主页面，交由执行器重新判断

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
from src.scenario import CLICK_INTERVAL, DeviceScenario

BAD_WEATHER_WAIT = 5.0    # 开始冒险后等冒险详情框加载的时间（秒）
BAD_WEATHER_KEYWORD = '天色不对'
RECALL_KEYWORD = '召回'
ADVENTURE_BATCH = 12      # 一轮连跑的冒险次数（跑满后回主页面，供执行器重新判断）
RECALL_CONFIRM_TRIES = 3  # 点召回后等/重试确认弹窗的次数（不再傻等 10 次）

PROGRESS_FILE = ADVENTURE_PROGRESS_FILE


class AdventureScenario(DeviceScenario):
    def __init__(self, dev=None):
        super().__init__(dev)
        self.times_per_day = self.cfg.adventure.times_per_day
        self.skip_bad_weather = self.cfg.adventure.skip_bad_weather
        self.batch = self.cfg.adventure.batch
        log(f'每天冒险次数: {self.times_per_day if self.times_per_day else "不冒险"}'
            f'，跳过"天色不对": {"开" if self.skip_bad_weather else "关"}'
            f'，单轮冒险次数 {self.batch}')

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

    def do_adventure(self) -> bool:
        """准备页面 -> 开始冒险 -> 等待 adventure_end -> 点 quit 退出。
        开关开启时开始后先检测"天色不对"：命中则召回并确认，不再等冒险结束
        （调用方照常计入一次冒险）。
        延时收尾模式（defer_wait）：进行中登记 pending 后回主页面，到点由
        调度器 finish_pending 收尾计数；召回同步完成则立即 count_cross 计数。
        返回 True 表示走了"天色不对"召回（召回成功后回到主页面，不是"出门"页，
        调用方应回主页面重进而不是直接点冒险入口）。"""
        self.click_until_gone_or_see('adventure_start', 'adventure_in', '开始冒险')
        if self.skip_bad_weather and self.recall_bad_weather():
            if self.defer_wait:
                # 召回即同步完成：立即计数（计数统一走 count_cross，
                # 由 run() 刷新本地计数判断上限）
                count_cross('adventure')
            return True
        log('已开始冒险，等待结束...')
        if self.defer_wait:
            # 延时收尾：登记 pending（到点由调度器 finish_pending 收尾计数）后回主页面
            self.defer_busy_end('adventure_in', 'adventure_end',
                                lambda: count_cross('adventure'), '冒险')
            self.ensure_main_page()
            return False
        self.wait_end('adventure_in', 'adventure_end')
        return False

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
        # 点召回后确认弹窗可能延迟弹出，最多重试 RECALL_CONFIRM_TRIES 次；
        # 每轮同时看是否还在"正在冒险"页——若是说明上次召回没点中/没生效，
        # 直接重新点一次召回，不再傻等
        for attempt in range(1, RECALL_CONFIRM_TRIES + 1):
            confirm = self.see('adventure_recall_confirm')
            if confirm:
                self.click(confirm[0], confirm[1])
                time.sleep(CLICK_INTERVAL)
                # 点完验证：确认弹窗已关 且 不再"正在冒险"才算成功；
                # 弹窗还在/仍在冒险说明没点中或没生效，重新点召回再试
                if (not self.see('adventure_recall_confirm')
                        and not self.see('adventure_in')):
                    log('确认召回成功，已离开冒险')
                    return True
                log(f'点击确认召回后仍在冒险/弹窗未关，重新点击召回 '
                    f'({x1 + recall[0]}, {y1 + recall[1]}) '
                    f'({attempt}/{RECALL_CONFIRM_TRIES})')
                self.click(x1 + recall[0], y1 + recall[1])
                time.sleep(CLICK_INTERVAL)
                continue
            if self.see('adventure_in'):
                log(f'仍处于"正在冒险"，重新点击召回 '
                    f'({x1 + recall[0]}, {y1 + recall[1]})')
                self.click(x1 + recall[0], y1 + recall[1])
                time.sleep(CLICK_INTERVAL)
                continue
            log(f'未找到确认召回按钮，等待重试 ({attempt}/{RECALL_CONFIRM_TRIES})')
            time.sleep(CLICK_INTERVAL)
        raise RuntimeError('点击召回后未出现"确认召回"按钮')

    def run(self, max_times: int | None = None, max_rounds: int = 0,
            batch: int | None = None) -> bool:
        """max_times: 当天冒险次数上限，0 表示不限；None 表示用配置值。
        max_rounds: 最多跑多少轮后返回，0 为不限。一轮 = 连跑 batch 次冒险，
        跑满（或达当天上限）后回主页面（供执行器逐次调度）。
        batch: 一轮连跑的冒险次数，默认取配置 adventure.batch（默认 12）；
        连续冒险之间不回主页面——quit 后落在"出门"页面，直接点冒险入口进
        准备页开下一把；点不到（被弹回主页面/处于进行中状态）则回主页面
        重进再继续本批。
        返回本次调用是否完成了至少一次冒险。
        """
        if max_times is None:
            max_times = self.times_per_day
        if batch is None:
            batch = getattr(self, 'batch', ADVENTURE_BATCH)
        today, done, history = load_progress(PROGRESS_FILE)
        log_history(history, today)
        start_done = done
        if max_times and done >= max_times:
            log(f'今天已冒险满 {max_times} 次，无需再冒险')
            return False

        def count_finished(finished: str) -> bool:
            """等完的活动计数（被雇佣在召回点 quit 时已计数，不再重复）；
            返回 True 表示已达标、本轮应结束。"""
            nonlocal done
            if finished == 'adventure':
                done += 1
                save_progress(PROGRESS_FILE, today, done, history)
                log(f'已完成第 {done} 次冒险'
                    + (f' / 目标 {max_times} 次' if max_times else ''))
            elif finished != 'employed':
                count_cross(finished)
            return bool(max_times and done >= max_times)

        round_no = 0
        while True:
            round_no += 1
            log(f'===== 第 {round_no} 轮（连跑 {batch} 次冒险）=====')
            # 刚收尾完（finish_pending 点 quit 已落在"出门"页）且中间没跑过别的
            # 任务（时间窗内）：直接点冒险入口，省一次 back + 出门；
            # 标记只消费一次，过期（>10s，中间跑过其他任务已回主页面）则忽略
            skip_home = (getattr(self, '_after_pending_go_out_at', 0)
                         and time.monotonic() - self._after_pending_go_out_at < 10)
            self._after_pending_go_out_at = 0
            if skip_home:
                log('收尾点完 quit 已在出门页面，直接点冒险入口')
                try:
                    self.click_until_gone_or_see(
                        'adventure', 'adventure_start', '前往冒险')
                    finished = None
                except RuntimeError:
                    log('收尾后直接点冒险入口失败，回主页面重进')
                    self.ensure_main_page()
                    finished = self.goto_adventure()
            else:
                self.ensure_main_page()
                finished = self.goto_adventure()
            if finished:
                if self.pending is not None:
                    # 延时收尾模式：出门检测到的进行中活动已登记 pending，计数由
                    # finish_pending 收尾时统一进行，本轮直接结束
                    return True
                if count_finished(finished):
                    log('达到当天冒险次数，结束')
                    return True
                if max_rounds and round_no >= max_rounds:
                    return True
                log('本轮结束，回主页面重新开始')
                continue
            # 连跑 batch 次冒险，期间不回主页面
            for i in range(batch):
                recalled = self.do_adventure()
                if self.defer_wait:
                    if self.pending is not None:
                        # 冒险进行中已登记 pending，计数由 finish_pending 收尾时
                        # 统一进行，本轮（本批）直接结束
                        return True
                    # 召回同步完成（skip_bad_weather）：count_cross 已计数，
                    # 刷新本地计数判断上限
                    today, done, history = load_progress(PROGRESS_FILE)
                    log(f'已完成第 {done} 次冒险'
                        + (f' / 目标 {max_times} 次' if max_times else ''))
                else:
                    # 检测到 adventure_end 点完 quit 就计数
                    done += 1
                    save_progress(PROGRESS_FILE, today, done, history)
                    log(f'已完成第 {done} 次冒险'
                        + (f' / 目标 {max_times} 次' if max_times else ''))
                if max_times and done >= max_times:
                    self.ensure_main_page()
                    log('达到当天冒险次数，结束')
                    return True
                if i < batch - 1:
                    if recalled:
                        # 召回成功后实测停在"出门"页（main_sign 识别不到），不是主页面：
                        # 出门页直接点冒险入口，省一次 back + 出门；只有真的在主页面
                        # （家里）才回家重进
                        if self.see('main_sign'):
                            log('召回后已在主页面，回主页面重新进入冒险')
                            self.ensure_main_page()
                            finished = self.goto_adventure()
                        else:
                            log('召回后已在出门页面，直接点冒险入口')
                            try:
                                self.click_until_gone_or_see(
                                    'adventure', 'adventure_start', '前往冒险')
                                finished = None
                            except RuntimeError:
                                log('召回后直接点冒险入口失败，回主页面重进')
                                self.ensure_main_page()
                                finished = self.goto_adventure()
                        if finished and self.pending is not None:
                            return True  # 延时收尾：pending 已登记，本轮直接结束
                        if finished and count_finished(finished):
                            return True
                        if finished:
                            break  # 重进时又等完别的活动：本批提前结束，交给下一轮
                        continue
                    # 上一把 quit 后落在"出门"页面：直接点冒险入口进准备页开下一把。
                    # 点不到=被弹回主页面/处于进行中状态，回主页面重进
                    try:
                        self.click_until_gone_or_see(
                            'adventure', 'adventure_start', '前往冒险')
                    except RuntimeError:
                        log('冒险后未在出门页面，回主页面重新进入')
                        self.ensure_main_page()
                        finished = self.goto_adventure()
                        if finished and self.pending is not None:
                            return True  # 延时收尾：pending 已登记，本轮直接结束
                        if finished and count_finished(finished):
                            return True
                        if finished:
                            break  # 重进时又等完别的活动：本批提前结束，交给下一轮
            if max_rounds and round_no >= max_rounds:
                self.ensure_main_page()
                log(f'已跑完 {max_rounds} 轮，返回')
                return done > start_done
            log(f'本轮连跑 {batch} 次冒险完成，回主页面重新开始')


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
