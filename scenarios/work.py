"""打工场景。

流程（u2 控件/OCR 文字定位，分辨率无关）：
1. 主页面（main_sign）-> 点击 leave_home 出门
2. 出门后若正在上课/工作/冒险/被雇佣中（school_in / work_in / adventure_in / employed_in）
   -> 等待结束并退出，等完的课程/工作计入对应场景的当天次数，
      回主页面结束本轮，由执行器重新判断限制条件后再决定下一步
3. 点击 town 进入小镇
4. 点击 back 重置默认选择地点，OCR 整屏文字找到配置的打工地点并点击进入
5. 把第一框拖到第三框归位（两次），点击第二框选择第二个工作（最高收益）
6. 点击 work_outworker 进入雇佣好友界面：
   - 识别到 employ 按钮 -> 点击最上方的一个
   - 没有 -> 从 (365, 1200) 拖到 (365, 700) 边拖边找，找到点最上方一个
   - 拖动达到配置上限仍没有 -> 点击 work_employ_close 关闭
7. 点击 work_start 开始工作，直到出现 work_in
8. 工作中按配置的检查间隔（schedule.check_interval）检查，直到出现 work_end，点击 quit 退出
9. 当天次数 +1 并持久化到 runs/work_progress.json（含 history 历史记录），
   回主页面重新开始

运行：python scenarios/work.py            （Ctrl+C 停止）
      python scenarios/work.py --times 5  （覆盖配置的每天打工次数，0 为不限）
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.ocr import find_text, ocr_fullscreen
from src.progress import (
    WORK_PROGRESS_FILE,
    count_cross,
    load_progress,
    log,
    log_history,
    save_progress,
)
from src.scenario import CLICK_INTERVAL, DeviceScenario, NAV_TIMEOUT

# 以下坐标均为 720x1280 参考坐标，运行时按当前分辨率自动换算
# 雇佣界面找不到按钮时的下滑拖动：从 (365, 1200) 拖到 (365, 700)
EMPLOY_SCROLL = (365, 1200, 365, 700)

PROGRESS_FILE = WORK_PROGRESS_FILE


class WorkScenario(DeviceScenario):
    def __init__(self, dev=None):
        super().__init__(dev)
        self.location = self.cfg.work.location
        if not self.location:
            raise ValueError('config.yaml 中 work.location 未配置打工地点')
        self.times_per_day = self.cfg.work.times_per_day
        self.employ_scroll_limit = self.cfg.work.employ_scroll_limit
        log(f'打工地点: {self.location}，每天打工次数: '
            f'{self.times_per_day if self.times_per_day else "不限"}')

    # ---- 各阶段 ----

    def goto_town(self) -> str | None:
        """主页面 -> 出门 -> 进小镇。

        出门后若正在上课/工作中（上次中途停止），等待结束并退出、回主页面，
        返回等完的是哪种（'school' / 'work' / 'adventure' / 'employed'）——此时不再继续进小镇，
        由调用方/执行器重新判断限制条件后再决定下一步；正常情况返回 None。
        """
        self.leave_home()
        finished = self.wait_busy_end()
        if finished:
            self.ensure_main_page()
            return finished
        try:
            for attempt in range(1, NAV_TIMEOUT + 1):
                town = self.see('town')
                if town:
                    self.click(town[0], town[1])
                    time.sleep(CLICK_INTERVAL)
                    return None
                log(f'未找到 town 按钮，等待重试 ({attempt}/{NAV_TIMEOUT})')
                time.sleep(CLICK_INTERVAL)
            raise RuntimeError('出门后未找到 town 按钮')
        except RuntimeError:
            # 正在上课/冒险等时点小镇入口可能无响应，导航超时；
            # 屏幕早已稳定，重新检测一次进行中状态再下结论
            finished = self._recheck_busy_after_nav('前往小镇')
            if finished:
                return finished
            raise

    def select_place(self) -> None:
        """点 back 重置默认地点后 OCR 整屏，找到配置的打工地点并点击进入。"""
        for attempt in range(1, NAV_TIMEOUT + 1):
            # 复用一次控件树快照：back 有多个 XPath 候选，
            # 不传 source 每个候选都会单独 dump 全树（每次约 2-3 秒）
            back = self.see('back', source=self.dev.hierarchy())
            if not back:
                raise RuntimeError('未找到 back 按钮，无法重置打工地点')
            self.click(back[0], back[1])
            time.sleep(CLICK_INTERVAL)
            results = ocr_fullscreen(self.screen())
            hit = find_text(results, self.location)
            if hit:
                log(f'OCR 找到打工地点 {self.location} '
                    f'({hit[0]}, {hit[1]}, score={hit[2]:.2f})')
                self.click(hit[0], hit[1])
                time.sleep(CLICK_INTERVAL)
                return
            log(f'未识别到打工地点 {self.location}，重试 ({attempt}/{NAV_TIMEOUT})')
        raise RuntimeError(f'OCR 多次未识别到打工地点: {self.location}')

    def select_job(self) -> None:
        """先把第一框拖到第三框归位（两次），点第二个工作（最高收益），
        再点 work_outworker 进雇佣界面。"""
        self.reset_select_boxes()
        hit = self.see('select_box_2')
        if not hit:
            raise RuntimeError('未定位到工作选择框: select_box_2')
        self.click(hit[0], hit[1])
        time.sleep(CLICK_INTERVAL)
        self.click_until_gone_or_see('work_outworker', 'work_employ_close', '选择雇佣好友')

    def hire_friend(self) -> None:
        """雇佣好友：点 XPath 定位到的雇佣按钮；没有则边下滑边找；到上限就关闭。"""
        # 每次查找复用一次控件树快照：employ 是深层 XPath，
        # 不传 source 的实时查询每次要 dump 全树（约 2-4 秒）
        btn = self.see('employ', source=self.dev.hierarchy())
        if btn:
            log(f'找到雇佣按钮，点击 ({btn[0]}, {btn[1]})')
            self.click(btn[0], btn[1])
            time.sleep(CLICK_INTERVAL)
            return
        for i in range(1, self.employ_scroll_limit + 1):
            self.swipe(*EMPLOY_SCROLL)
            time.sleep(CLICK_INTERVAL)
            btn = self.see('employ', source=self.dev.hierarchy())
            if btn:
                log(f'下滑 {i} 次后找到雇佣按钮，点击 ({btn[0]}, {btn[1]})')
                self.click(btn[0], btn[1])
                time.sleep(CLICK_INTERVAL)
                return
            log(f'未找到雇佣按钮，继续下滑 ({i}/{self.employ_scroll_limit})')
        log(f'拖动 {self.employ_scroll_limit} 次仍未找到雇佣按钮，关闭雇佣界面')
        close = self.see('work_employ_close', source=self.dev.hierarchy())
        if not close:
            raise RuntimeError('雇佣界面未找到 work_employ_close 关闭按钮')
        self.click(close[0], close[1])
        time.sleep(CLICK_INTERVAL)

    def wait_work_end(self) -> None:
        """等待 work_end 出现并点击 quit 退出。"""
        self.wait_end('work_in', 'work_end')

    def run(self, max_times: int | None = None, max_rounds: int = 0) -> bool:
        """max_times: 当天打工次数上限，0 表示不限；None 表示用配置值。
        max_rounds: 最多跑多少轮后返回，0 为不限（供执行器逐轮调度）。
        返回本次调用是否完成了至少一次打工。
        """
        if max_times is None:
            max_times = self.times_per_day
        today, done, history = load_progress(PROGRESS_FILE)
        log_history(history, today)
        start_done = done
        if max_times and done >= max_times:
            log(f'今天已打工满 {max_times} 次，无需再打工')
            return False
        round_no = 0
        while True:
            round_no += 1
            log(f'===== 第 {round_no} 轮 =====')
            self.ensure_main_page()
            finished = self.goto_town()
            if finished:
                if finished == 'work':
                    # 出门时等完了一次上次未结束的工作，计入当天次数
                    done += 1
                    save_progress(PROGRESS_FILE, today, done, history)
                    log(f'已完成第 {done} 次打工' + (f' / 目标 {max_times} 次' if max_times else ''))
                    if max_times and done >= max_times:
                        log('达到当天打工次数，结束')
                        return True
                elif finished != 'employed':
                    # 出门时等完的是别的活动（上课/冒险），计入对应次数
                    # （被雇佣在召回点 quit 时已计数）
                    count_cross(finished)
                # 等完了一次活动，计数已变化，本轮结束，
                # 回主页面交由执行器重新判断限制条件
                if max_rounds and round_no >= max_rounds:
                    return True
                log('本轮结束，回主页面重新开始')
                continue
            self.select_place()
            self.select_job()
            self.hire_friend()
            self.click_until_gone_or_see('work_start', 'work_in', '开始工作')
            log('已开始工作，等待结束...')
            self.wait_work_end()
            done += 1
            save_progress(PROGRESS_FILE, today, done, history)
            log(f'已完成第 {done} 次打工' + (f' / 目标 {max_times} 次' if max_times else ''))
            if max_times and done >= max_times:
                log('达到当天打工次数，结束')
                return done > start_done
            if max_rounds and round_no >= max_rounds:
                log(f'已跑完 {max_rounds} 轮，返回')
                return done > start_done
            log('本轮结束，回主页面重新开始')


if __name__ == '__main__':
    import argparse

    ap = argparse.ArgumentParser(description='打工场景')
    ap.add_argument('--times', type=int, default=None,
                    help='当天打工次数上限，0 为不限；不指定则读 config.yaml 的 work.times_per_day')
    args = ap.parse_args()

    try:
        WorkScenario().run(max_times=args.times)
    except KeyboardInterrupt:
        log('手动停止')
