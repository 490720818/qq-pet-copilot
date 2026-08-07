"""PK 场景：访问好友宠物页发起 PK 对战。

流程（u2 控件定位，分辨率无关）：
1. 点击 好友（visit_friends）-> 点击 访问（visit）进入第一个好友宠物页
   （好友导航复用 visit.py：累积名单、按顺序切换）
2. 点击 PK（pk）-> 点击 开始（pk_start）
3. 等 11 秒 PK 结束，出现 分享（pk_end）即计一次，持久化到 runs/pk_progress.json
4. 点击 再来一局（pk_again）-> 再点 开始；每个好友最多 PK 3 次
5. 切换下一个好友继续 PK，直到次数满或没有更多好友
6. 结束：点 back 回主页面

分轮与状态检查：每局消耗 体力/清洁 各 5 点；一次 run() 最多 PK 16 局
（配置次数 > 16 时本轮先做 16 局，剩余由执行器下一轮接着处理）。
开始前检查：体力/清洁 都 >= 本轮计划局数 x 5 才开跑，
不足则先喂食/洗澡补充到所需值（计划局数 x 5）。

运行方式（同 visit.py）：run() 独立运行回主页面。

运行：python scenarios/pk.py            （Ctrl+C 停止）
      python scenarios/pk.py --times 5 （覆盖配置的每天 PK 次数，0 为不限）
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.progress import (
    PK_PROGRESS_FILE,
    load_progress,
    log,
    log_history,
    save_progress,
)
from src.scenario import CLICK_INTERVAL, DeviceScenario, NAV_TIMEOUT
from scenarios.care import CareScenario
from scenarios.visit import FRIEND_ITEM_XPATH, VisitScenario

PK_PER_FRIEND = 3     # 每个好友可 PK 次数
PK_DURATION = 11.0    # 一局 PK 时长（秒）
PK_END_TIMEOUT = 13.0  # 等 PK 结果（分享按钮）的超时（秒），超时点 quit 换好友
PK_ENTER_TIMEOUT = 3.0  # 点 PK 后等开始按钮的短超时：上限只弹 toast 不跳页，不用长等
PK_ROUND_CAP = 16     # 一次 run() 最多 PK 局数（超出由执行器下一轮接着处理）
PK_STAT_COST = 5      # 每局消耗体力/清洁
PK_TIMEOUT_STREAK_LIMIT = 2  # 连续几次"PK 结果超时"就临时推迟 PK 任务

PROGRESS_FILE = PK_PROGRESS_FILE


class PKDeferred(Exception):
    """PK 结果连续超时：临时推迟 PK 任务（调度器延后重试，不做重启恢复）。"""


class PKScenario(VisitScenario):
    def __init__(self, dev=None):
        DeviceScenario.__init__(self, dev)  # 跳过 VisitScenario 的踩踩字段/日志
        self.times_per_day = self.cfg.pk.times_per_day
        log(f'每天 PK 次数: {self.times_per_day if self.times_per_day else "不限"}')

    # ---- 各阶段 ----

    def wait_pk_end(self) -> bool:
        """等 PK 结果页（分享按钮）出现；超时返回 False（调用方点 quit 换好友）。"""
        deadline = time.monotonic() + PK_END_TIMEOUT
        while time.monotonic() < deadline:
            hit = self.see('pk_end', source=self.dev.hierarchy())
            if hit:
                return True
            time.sleep(CLICK_INTERVAL)
        return False

    def leave_result_page(self) -> None:
        """PK 结束页/准备页点 back 回好友宠物页。

        PK 准备页（开始按钮）和结果页（分享/再来一局）都没有好友列表，
        必须回到好友宠物页（底部好友横排出现）才能切下一个好友。
        """
        for _ in range(5):
            source = self.dev.hierarchy()
            if self.dev.find_xpath_all(FRIEND_ITEM_XPATH, source=source):
                return
            back = self.see('back', source=source)
            if not back:
                return
            self.click(back[0], back[1])
            time.sleep(CLICK_INTERVAL)

    def _wait_pk_start(self, timeout: float = PK_END_TIMEOUT) -> tuple[int, int, float] | None:
        """等开始按钮出现（再来一局/进 PK 页后有几秒加载延迟，只查一次会误判）。

        超时返回 None，由调用方区分"没跳页（好友已满）"和"跳页了但加载慢"。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hit = self.see('pk_start', source=self.dev.hierarchy())
            if hit:
                return hit
            time.sleep(CLICK_INTERVAL)
        return None

    def pk_friend(self, max_times: int, today: str, done: int, history: dict) -> int:
        """对当前好友 PK 至多 PK_PER_FRIEND 局，返回新的当天次数。

        点 PK 后 3 秒内没出现开始按钮且 PK 按钮还在（没跳页，只是上限
        toast）= 该好友 PK 次数已达上限，直接返回换下一个好友；
        页面已跳转但开始按钮加载慢的，继续走长等待。
        """
        hit = None
        for attempt in range(1, 4):
            # 访问后好友页有几秒加载延迟，PK 按钮重试 3 次再下结论
            hit = self.see('pk', source=self.dev.hierarchy())
            if hit:
                break
            log(f'未找到 PK 按钮，等待重试 ({attempt}/3)')
            time.sleep(CLICK_INTERVAL)
        if not hit:
            raise RuntimeError('好友页未找到 PK 按钮')
        self.click(hit[0], hit[1])
        time.sleep(CLICK_INTERVAL)
        start = self._wait_pk_start(PK_ENTER_TIMEOUT)
        if start is None and self.see('pk', source=self.dev.hierarchy()):
            # 已达上限时只弹 toast 不跳页（还在好友宠物页），不要点 back，
            # 页面导航统一交给外层 leave_result_page
            log('该好友 PK 次数已达上限，切换下一个好友')
            return done
        for i in range(1, PK_PER_FRIEND + 1):
            if max_times and done >= max_times:
                break
            hit = self._wait_pk_start()
            if not hit:
                raise RuntimeError('PK 页未找到开始按钮')
            self.click(hit[0], hit[1])
            log(f'第 {i}/{PK_PER_FRIEND} 局 PK 中...')
            time.sleep(PK_DURATION)
            if not self.wait_pk_end():
                # 超时没出结果：连续出现多次说明 PK 环境异常（网络/加载卡住），
                # 点 quit 退出该局（不计数）换下一个好友；连续达到上限则临时推迟整个 PK 任务
                self._pk_timeout_streak += 1
                log(f'{PK_END_TIMEOUT:.0f}s 未出 PK 结果，点 quit 切换下一个好友'
                    f'（连续第 {self._pk_timeout_streak}/{PK_TIMEOUT_STREAK_LIMIT} 次）')
                if self._pk_timeout_streak >= PK_TIMEOUT_STREAK_LIMIT:
                    raise PKDeferred('PK 结果连续超时，临时推迟 PK 任务')
                quit_hit = self.see('quit')
                if quit_hit:
                    self.click(quit_hit[0], quit_hit[1])
                    time.sleep(CLICK_INTERVAL)
                return done
            done += 1
            self._pk_timeout_streak = 0  # PK 成功，清零连续超时计数
            save_progress(PROGRESS_FILE, today, done, history)
            log(f'已完成 {done} 次 PK' + (f' / 目标 {max_times} 次' if max_times else ''))
            if i < PK_PER_FRIEND and (not max_times or done < max_times):
                self.click_until_gone_or_see('pk_again', 'pk_start', '再来一局')
        return done

    def _pk_all(self, max_times: int, today: str, done: int, history: dict) -> int:
        """从好友面板开始逐好友 PK，直到次数满或没有更多好友，返回新的当天次数。"""
        self._friends = []        # 累积好友名单（只增不减），见 visit.py
        self._friend_index = 0    # 访问进入时默认第一个好友
        self._pk_timeout_streak = 0  # 连续 PK 结果超时计数（本轮内连续，成功清零）
        self.goto_first_friend()
        while not max_times or done < max_times:
            done = self.pk_friend(max_times, today, done, history)
            if max_times and done >= max_times:
                break
            self.leave_result_page()
            if not self.next_friend():
                log('没有更多好友了')
                break
        return done

    # ---- 入口 ----

    def _prepare_stats(self, planned: int) -> None:
        """PK 前检查体力/清洁：每局各消耗 PK_STAT_COST，
        不足 planned*5 则喂食/洗澡补充到所需值 planned*5（流程同 care.check_and_care）。
        护理方式为"一键护理"时不读状态：有一键护理按钮就点，然后直接开跑。
        """
        care = CareScenario(self.dev)
        if care.method == '一键护理':
            self.ensure_main_page()
            care.one_click_care()
            return
        need = planned * PK_STAT_COST
        source = self.ensure_main_page()
        care.toggle_status(source)
        # 数值异步加载（刚展开可能只有账号/宠物名），重试读到体力/清洁为止
        status = care.read_status_ready()
        source = self.dev.hierarchy()
        energy = status.get('体力')
        clean = status.get('清洁')
        log(f'PK 前状态: 体力={energy} 清洁={clean}，本轮计划 {planned} 局（各需 {need}）')
        cared = False
        if energy is not None and energy < need:
            log(f'体力 {energy} 不足 {need}，喂食到 {need}')
            care.energy_threshold = need
            care.feed(source)
            cared = True
            source = self.dev.hierarchy()
        if clean is not None and clean < need:
            log(f'清洁 {clean} 不足 {need}，洗澡到 {need}')
            care.clean_threshold = need
            care.shower(source)
            cared = True
            source = self.dev.hierarchy()
        if cared:
            source = care.exit_care_mode(source)
        care.toggle_status(source)
        log('PK 前状态检查完成，已收起宠物状态')

    @staticmethod
    def _round_limit(max_times: int, done: int) -> int:
        """本轮 PK 目标次数：一次最多 PK_ROUND_CAP 局。"""
        if max_times:
            return min(max_times, done + PK_ROUND_CAP)
        return done + PK_ROUND_CAP

    def run(self, max_times: int | None = None, max_rounds: int = 0) -> bool:
        """回主页面 -> 检查/补充体力清洁 -> 进好友面板逐好友 PK，结束点 back 回主页面。

        一次最多 PK_ROUND_CAP 局（配置次数更多时由执行器下一轮接着跑）。
        max_rounds 参数仅为与其他场景签名一致。
        返回本次是否 PK 了至少一局。
        """
        if max_times is None:
            max_times = self.times_per_day
        today, done, history = load_progress(PROGRESS_FILE)
        log_history(history, today)
        if max_times and done >= max_times:
            log(f'今天已 PK 满 {max_times} 次，无需再 PK')
            return False
        start_done = done
        round_target = self._round_limit(max_times, done)
        self.ensure_main_page()
        self._prepare_stats(round_target - done)
        done = self._pk_all(round_target, today, done, history)
        self.close()  # 点 back 收掉好友相关页面，再确认回主页面
        self.ensure_main_page()
        return done > start_done

if __name__ == '__main__':
    import argparse

    ap = argparse.ArgumentParser(description='PK 场景')
    ap.add_argument('--times', type=int, default=None,
                    help='当天 PK 次数上限，0 为不限；不指定则读 config.yaml 的 pk.times_per_day')
    args = ap.parse_args()

    try:
        PKScenario().run(max_times=args.times)
    except KeyboardInterrupt:
        log('手动停止')
