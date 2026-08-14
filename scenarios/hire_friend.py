"""好友雇佣场景：访问指定好友家，雇佣好友打工。

流程（好友导航复用 friend_care.py / visit.py：累积名单、按顺序切换）：
0. 雇佣前预检：出门检测宠物是否正在打工/学习/冒险/被雇佣中（detect_busy_remaining），
   命中则 OCR 剩余时间，抛 TaskDeferred 延后到活动结束（非阻塞，调度层到点再调度）
1. 点击 好友（visit_friends）-> 访问（visit）进入第一个好友宠物页
2. 依次切换好友列表（content-desc "好友 xxx"），找到配置的好友名称，点击进入其家
3. OCR hire 控件（//*[@content-desc="hire"]）范围内是否有雇佣剩余 CD 倒计时
   （如 28:05）：有则抛 TaskDeferred 延后 HIRE_CD_POLL_SECONDS 秒复测（不原地等待，
   CD 可能提前结束；延后期间调度器先跑其他任务），没有倒计时才点 hire
4. 点击 hire 雇佣 -> 跳转到打工面板（面板加载需要时间：点击后固定等
   HIRE_PANEL_WAIT 秒再检测，未出现则重试点击；等待期间可能弹职业升级/
   获得新职业弹窗，先 dismiss_career_popup 处理再检测）
5. 后面跟打工流程一样：select_place 确认/重选打工地点（当前面板已是配置地点就直接用，
   不是则 back 重置 -> OCR 找配置地点重进），归位选择框后按 work.duration 点
   对应工作选择框（10分钟/45分钟/2小时 -> select_box_1/2/3）
   （不做打工流程里的雇佣部分 work.hire_friend），点 work_start（去打工）开始打工
6. 等待打工结束（work_end -> quit）后计数：当天雇佣好友次数 +1 持久化到
   runs/hire_friend_progress.json，同时计入一次打工（runs/work_progress.json）

配置（config.yaml 的 hire_friend 段）：enabled 开关 / time_range 雇佣时间段
（HH:MM-HH:MM）/ interval_seconds 调度间隔（秒）/ friend_name 雇佣好友名称 /
times_per_day 每天雇佣次数（0 不雇佣）。

运行：python scenarios/hire_friend.py            （Ctrl+C 停止）
      python scenarios/hire_friend.py --times 2  （覆盖配置的每天雇佣次数，0 为不限）
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.locators import see_bounds
from src.ocr import ocr_texts
from src.progress import (
    HIRE_FRIEND_PROGRESS_FILE,
    WORK_PROGRESS_FILE,
    load_progress,
    log,
    log_history,
    save_progress,
)
from src.scenario import CLICK_INTERVAL, DeviceScenario, TaskDeferred
from scenarios.friend_care import FriendCareScenario
from scenarios.work import DURATION_BOXES, WorkScenario

PROGRESS_FILE = HIRE_FRIEND_PROGRESS_FILE

HIRE_CD_POLL_SECONDS = 60  # 雇佣 CD 未到时的复测间隔（秒）：不原地等（CD 可能提前结束），延后到点再调度
HIRE_PANEL_WAIT = 3.0   # 点 hire 后打工面板的加载等待（秒），等完再检测面板
HIRE_PANEL_ATTEMPTS = 3  # 点 hire 未进打工面板的重试次数
HIRE_PANEL_CHECKS = 3   # 每次等待后面板（选择框）出现的检测次数


class FriendHireScenario(FriendCareScenario):
    def __init__(self, dev=None):
        DeviceScenario.__init__(self, dev)  # 跳过 FriendCare/Visit 的字段与日志
        self.last_hire_at: datetime | None = None  # 上次调度触发时间（调度间隔用）
        hf = self.cfg.hire_friend
        log(f'好友雇佣: {"启用" if hf.enabled else "未启用"}，好友: {hf.friend_name or "未配置"}'
            f'，每天次数: {hf.times_per_day if hf.times_per_day else "不雇佣"}'
            f'，时间段: {hf.time_range}，调度间隔: {hf.interval_seconds}秒')

    # ---- 雇佣 CD ----

    def hire_cd_seconds(self) -> int | None:
        """OCR hire 控件范围，解析雇佣剩余 CD 倒计时（如 28:05），返回剩余秒数；
        没有倒计时返回 None（可雇佣）。hire 控件未定位到也返回 None（由调用方判按钮）。"""
        bounds = see_bounds(self.dev, 'hire')
        if not bounds:
            return None
        x1, y1, x2, y2 = bounds
        results = ocr_texts(self.screen()[y1:y2, x1:x2])
        log('雇佣按钮区域 OCR: '
            + (', '.join(f'{t!r}@({x},{y})' for t, x, y, _ in results) or '无'))
        for text, *_ in results:
            m = re.search(r'(\d{1,3}):(\d{2})', text.replace(' ', ''))
            if m:
                return int(m.group(1)) * 60 + int(m.group(2))
        return None

    def wait_hire_ready(self) -> None:
        """检查雇佣 CD：有剩余倒计时就抛 TaskDeferred 延后 HIRE_CD_POLL_SECONDS
        秒再复测（不原地等待——CD 可能提前结束，且等待期间可以调度其他任务）；
        没有 hire 按钮视为页面不对，抛异常走重试链路。"""
        if not self.see('hire', source=self.dev.hierarchy()):
            raise RuntimeError('好友家未找到 hire 雇佣按钮')
        secs = self.hire_cd_seconds()
        if secs is None:
            log('雇佣 CD 已就绪，可以雇佣')
            return
        until = datetime.now() + timedelta(seconds=HIRE_CD_POLL_SECONDS)
        raise TaskDeferred(until, f'雇佣剩余 CD {secs // 60}:{secs % 60:02d}，'
                                  f'{HIRE_CD_POLL_SECONDS} 秒后复测')

    # ---- 雇佣并打工 ----

    def _enter_work_panel(self) -> None:
        """点 hire 进打工面板：面板加载需要时间，点击后固定等 HIRE_PANEL_WAIT 秒
        再检测选择框是否出现；等待期间可能弹职业升级/获得新职业弹窗（挡住面板），
        先处理弹窗再检测；未出现重试点击，多次失败抛异常走重试链路。"""
        for attempt in range(1, HIRE_PANEL_ATTEMPTS + 1):
            hit = self.see('hire', source=self.dev.hierarchy())
            if hit:
                self.click(hit[0], hit[1])
            elif attempt == 1:
                raise RuntimeError('好友家未找到 hire 雇佣按钮')
            log(f'点击 hire，等待 {HIRE_PANEL_WAIT:.0f} 秒让打工面板加载')
            time.sleep(HIRE_PANEL_WAIT)
            for _check in range(HIRE_PANEL_CHECKS):
                # 职业升级/获得新职业弹窗会挡住打工面板：处理后继续检测
                if self.dismiss_career_popup():
                    time.sleep(CLICK_INTERVAL)
                    continue
                if self.see('select_box_1'):
                    return
                time.sleep(CLICK_INTERVAL)
            log(f'点击 hire 后未出现打工面板，重试 ({attempt}/{HIRE_PANEL_ATTEMPTS})')
        raise RuntimeError('多次点击 hire 仍未进入打工面板')

    def _select_job(self) -> None:
        """按配置 work.duration 选工作（10分钟/45分钟/2小时 -> select_box_1/2/3）：
        归位选择框后点对应选择框，不做打工流程里的雇佣部分（不进 work_outworker 雇佣面板）。"""
        self.reset_select_boxes()
        box = DURATION_BOXES.get(self.cfg.work.duration, 'select_box_2')
        hit = self.see(box)
        if not hit:
            raise RuntimeError(f'未定位到工作选择框: {box}')
        time.sleep(CLICK_INTERVAL)
        self.click(hit[0], hit[1])

    def _hire_and_work(self) -> None:
        """点 hire 进打工面板 -> 确认/重选打工地点 -> 按 work.duration 选工作选择框 ->
        work_start 开工 ->
        等打工结束点 quit（不做打工流程里的雇佣部分，由调用方计数）。"""
        self._enter_work_panel()
        work = WorkScenario(self.dev)
        # 打工地点处理跟打工流程完全一致（select_place）：当前面板已是配置地点就直接用；
        # 不是则走重选分支——back 重置 -> OCR 找配置地点 -> 点击进入，仍不行回主页面
        # 重新进小镇再选（此时 hire 已生效/CD 已起，重选的是自己干活的打工面板）
        work.select_place()
        self._select_job()
        # work_start 没出现时，先处理"去照顾一下"弹窗（护理 + back 回工作面板，同 work.py）
        if not work._has_work_start():
            work._recover_work_start()
        # work_start 仍未出现（面板没加载出来/被关闭等）：归位选择框重选一次，
        # 避免白等后直接重启设备；仍失败才交给上层重启恢复（同 work.py 的兜底）
        if not work._has_work_start():
            log('未回到工作面板，重新归位选择框再选一次')
            self._select_job()
        work._start_work()
        log('已开始雇佣打工，等待结束...')
        if self.defer_wait:
            # 延时收尾：登记 pending（到点由调度器 finish_pending 收尾，计雇佣+打工
            # 各一次）后回主页面，本轮直接结束
            self.defer_busy_end('work_in', 'work_end', self._count_hire_and_work,
                                '雇佣打工', encourage=True)
            self.ensure_main_page()
            return
        work.wait_work_end()

    def _count_hire_and_work(self) -> None:
        """延时收尾的计数（pending 收尾时由 finish_pending 调用）：
        雇佣好友次数 + 打工次数各一次。"""
        today, done, history = load_progress(PROGRESS_FILE)
        done += 1
        save_progress(PROGRESS_FILE, today, done, history)
        log(f'已完成 {done} 次雇佣好友')
        self._count_work()

    @staticmethod
    def _count_work() -> None:
        """雇佣打工同时计入一次打工（runs/work_progress.json，影响每日点数规则）。"""
        today, done, history = load_progress(WORK_PROGRESS_FILE)
        done += 1
        save_progress(WORK_PROGRESS_FILE, today, done, history)
        log(f'计入打工次数: 今天已打工 {done} 次')

    # ---- 入口 ----

    def run(self, max_times: int | None = None, max_rounds: int = 0) -> bool:
        """回主页面 -> 进目标好友家 -> 等雇佣 CD -> 雇佣打工一轮 -> 计数，
        直到当天次数满或跑完 max_rounds 轮，结束回主页面。

        max_times: 当天雇佣次数上限，0 表示不限；None 表示用配置值。
        返回本次调用是否完成了至少一次雇佣。
        """
        hf = self.cfg.hire_friend
        if not hf.enabled:
            log('好友雇佣未启用，跳过')
            return False
        name = hf.friend_name.strip()
        if not name:
            log('未配置雇佣好友名称，跳过好友雇佣')
            return False
        if max_times is None:
            max_times = hf.times_per_day
        today, done, history = load_progress(PROGRESS_FILE)
        log_history(history, today)
        if max_times and done >= max_times:
            log(f'今天已雇佣好友满 {max_times} 次，无需再雇佣')
            return False
        start_done = done
        # 雇佣前先出门检查宠物是否正在打工/学习/冒险/被雇佣中：命中则 OCR 剩余时间，
        # 延后到活动结束（非阻塞，由调度层到点再调度），不阻塞等待
        self.ensure_main_page()
        busy = self.detect_busy_remaining()
        if busy is not None:
            kind, secs = busy
            until = datetime.now() + timedelta(seconds=secs)
            names = {'school': '学习', 'work': '打工', 'adventure': '冒险', 'employed': '被雇佣'}
            raise TaskDeferred(
                until, f'宠物正在{names[kind]}（剩余约 {secs // 60} 分钟），'
                f'雇佣好友延后到 {until:%H:%M:%S}')
        self.ensure_main_page()  # 出门检测后回到主页面再走好友导航
        round_no = 0
        while True:
            round_no += 1
            log(f'===== 雇佣好友第 {round_no} 轮 =====')
            self.ensure_main_page()
            self.goto_friend_home(name)
            self.wait_hire_ready()
            self._hire_and_work()
            if self.defer_wait:
                # 延时收尾模式：计数在 pending 收尾时统一进行（_count_hire_and_work），
                # 本地 done 不再自增，本轮直接结束
                return True
            # 打工结束（点完 quit）后才计数：雇佣好友次数 + 打工次数各一次
            done += 1
            save_progress(PROGRESS_FILE, today, done, history)
            log(f'已完成 {done} 次雇佣好友' + (f' / 目标 {max_times} 次' if max_times else ''))
            self._count_work()
            if max_times and done >= max_times:
                log('达到当天雇佣好友次数，结束')
                return True
            if max_rounds and round_no >= max_rounds:
                log(f'已跑完 {max_rounds} 轮，返回')
                return done > start_done
            log('本轮结束，回主页面重新开始')


if __name__ == '__main__':
    import argparse

    ap = argparse.ArgumentParser(description='好友雇佣场景')
    ap.add_argument('--times', type=int, default=None,
                    help='当天雇佣好友次数上限，0 为不限；不指定则读 config.yaml 的 hire_friend.times_per_day')
    args = ap.parse_args()

    try:
        FriendHireScenario().run(max_times=args.times)
    except KeyboardInterrupt:
        log('手动停止')
