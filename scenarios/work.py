"""打工场景。

流程（u2 控件/OCR 文字定位，分辨率无关）：
1. 主页面（main_sign）-> 点击 leave_home 出门
2. 出门后若正在上课/工作/冒险/被雇佣中（school_in / work_in / adventure_in / employed_in）
   -> 等待结束并退出，等完的课程/工作计入对应场景的当天次数，
      回主页面结束本轮，由执行器重新判断限制条件后再决定下一步
3. 点击 town 进入小镇
4. 点 back 重置默认地点，OCR 整屏文字找到配置的打工地点并点击进入；
   没进面板就回主页面重新进小镇再试
5. 把第一框拖到第三框归位（两次），点击第二框选择第二个工作（最高收益）
6. 点击 work_outworker 进入雇佣好友界面（OCR 标题确认弹出）：
   - 识别到雇佣按钮（OCR 右侧第一个）-> 点击最上方的一个
   - 没有（当前页好友不可雇佣时不渲染按钮）-> 点工作面板顶部"智力"坐标
     关闭雇佣面板并确认弹层已关，回到打工面板由下一步点 work_start 直接开工（不雇佣）
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

from src.locators import locate_cached
from src.ocr import find_text, ocr_fullscreen, parse_panel_location
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
PROGRESS_FILE = WORK_PROGRESS_FILE

# 进入打工地点面板的最多重试次数（点空/页面加载慢时快速重试，不再长时间确认）
WORK_PLACE_ATTEMPTS = 3


class WorkScenario(DeviceScenario):
    def __init__(self, dev=None):
        super().__init__(dev)
        self.location = self.cfg.work.location
        if not self.location:
            raise ValueError('config.yaml 中 work.location 未配置打工地点')
        self.times_per_day = self.cfg.work.times_per_day
        # employ_scroll_limit 保留配置兼容（旧流程下滑找雇佣按钮已移除，
        # 当前页没有雇佣按钮时直接关闭面板开工），runner 仍会赋值
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
        """确认当前面板是不是配置的打工地点：是就直接用，不是才 back 重置再选。

        进小镇后游戏默认停在上次工作的打工面板，但面板有 1~2 秒加载过渡
        （刚进时力量/智力/魅力还没渲染出来，OCR 判不出地点名）：先等面板
        稳定再 OCR 确认，是配置的地点就直接返回，省掉 back 重置 -> OCR 找地点
        -> 点击的流程；不是才走 back 重置重选（重试时回主页面重新进小镇）。
        点击后要等打工面板（选择框）出现才算进入。
        """
        for _wait in range(1, 4):
            time.sleep(CLICK_INTERVAL)
            if self.is_correct_place_panel():
                log(f'当前面板已是打工地点 {self.location}，直接使用')
                return
        for attempt in range(1, WORK_PLACE_ATTEMPTS + 1):
            if attempt > 1:
                # 回主页面重新进小镇（干净状态），再走 back 重置
                self.ensure_main_page()
                finished = self.goto_town()
                if finished:
                    raise RuntimeError(f'重新进入小镇时检测到进行中状态: {finished}')
            back = self.see('back', source=self.dev.hierarchy())
            if not back:
                raise RuntimeError('未找到 back 按钮，无法重置打工地点')
            self.click(back[0], back[1])
            time.sleep(CLICK_INTERVAL)
            results = ocr_fullscreen(self.screen())
            hit = find_text(results, self.location)
            if not hit:
                log(f'未识别到打工地点 {self.location}，回主页面重试 ({attempt}/{WORK_PLACE_ATTEMPTS})')
                continue
            log(f'OCR 找到打工地点 {self.location} '
                f'({hit[0]}, {hit[1]}, score={hit[2]:.2f})')
            self.click(hit[0], hit[1])
            # 等打工面板出现：选择框在 + OCR 确认地点名是配置的打工地点（确认 3 次即可）
            entered = False
            for _wait in range(1, 4):
                time.sleep(CLICK_INTERVAL)
                # select_box_1 由容器推导：容器 cache 后秒回，不重复 dump
                if self.see('select_box_1') and self.is_correct_place_panel():
                    entered = True
                    break
            if entered:
                return
            log(f'点击 {self.location} 后未出现打工面板，回主页面重试 ({attempt}/{WORK_PLACE_ATTEMPTS})')
        raise RuntimeError(f'多次未能进入打工地点面板: {self.location}')

    def panel_location(self, screen=None) -> str | None:
        """当前面板的地点名：力量/智力/魅力属性面板正下方第一串字符（整屏 OCR）。"""
        if screen is None:
            screen = self.screen()
        return parse_panel_location(ocr_fullscreen(screen))

    def is_correct_place_panel(self, screen=None) -> bool:
        """判断当前工作面板是不是配置的打工地点（OCR 地点名 == self.location）。"""
        loc = self.panel_location(screen)
        if loc is None:
            return False
        return loc == self.location.replace(' ', '')

    def select_job(self) -> None:
        """先把第一框拖到第三框归位（两次），点第二个工作（最高收益），
        再点 work_outworker 进雇佣界面。

        轮播有 4 个（打工）/7 个（学园）选择框、xpath 只认可见 3 个，
        必须归位到第一页再选，否则可能选到绝对第几个不是第 2 个；
        拖动已提速（duration 0.3，约 2.3s/次），选择框槽位固定（cache 命中秒回）。
        """
        # select_box_N 由容器推导（容器 cache 后秒回）；work_outworker 首次未缓存时
        # 抓一次控件树快照，与容器推导共用，总共只 dump 一次
        source = None if locate_cached('work_outworker') else self.dev.hierarchy()
        self.reset_select_boxes(source=source)
        hit = self.see('select_box_2', source=source)
        if not hit:
            raise RuntimeError('未定位到工作选择框: select_box_2')
        self.click(hit[0], hit[1])
        # work_outworker 是独立按钮，不依赖选框结果，点完直接进雇佣面板
        self._open_employ_panel(source)

    def _open_employ_panel(self, source=None) -> None:
        """点 work_outworker 进雇佣面板；点击后用 OCR 标题确认面板弹出。

        work_outworker 位置固定且有 cache：首次命中后直接复用缓存点；
        source 传 select_job 共享的控件树快照时，首次也只用这一次 dump。
        """
        target = self.see('work_outworker', source=source)
        if not target:
            raise RuntimeError('未找到雇佣好友按钮')
        self.click(target[0], target[1])
        for _wait in range(1, 4):
            time.sleep(CLICK_INTERVAL)
            if self._employ_panel_open():
                log('选择雇佣好友: 雇佣面板已弹出')
                return
        raise RuntimeError('点击雇佣好友后雇佣面板未弹出')

    def _find_employ_button(self) -> tuple[int, int, float] | None:
        """雇佣面板好友列表的"雇佣"按钮：整屏 OCR 取右侧从上到下第一个。

        面板标题"宠友雇佣加成排行榜（实时刷新）"也含"雇佣"，但它在左侧；
        雇佣按钮在每行右侧（x >= 屏宽一半），按 x 排除标题后取最上面一个。
        整屏 OCR 约 0.5s，比 dump 控件树（4s+）快得多。
        """
        screen = self.screen()
        w = screen.shape[1]
        for x, y, score in self.see_all('employ', screen):
            if x >= w / 2:
                return x, y, score
        return None

    def hire_friend(self) -> None:
        """雇佣好友：有雇佣按钮就点；没有则直接关闭雇佣页面去开工。

        当前页好友不可雇佣时列表不渲染雇佣按钮（纯图片/无按钮）：
        OCR 检测一次没有就直接关闭雇佣面板，由 run() 点 work_start 直接开工。
        """
        btn = self._find_employ_button()
        if btn:
            log(f'找到雇佣按钮，点击 ({btn[0]}, {btn[1]})')
            self.click(btn[0], btn[1])
            time.sleep(CLICK_INTERVAL)
            return
        log('未找到雇佣按钮，直接关闭雇佣页面')
        self._close_employ_panel()

    def _employ_panel_open(self) -> bool:
        """雇佣排行榜面板是否还开着：整屏 OCR 检测标题"宠友雇佣加成排行榜"。

        OCR 约 0.5s，比 dump 控件树（4s+）快得多；关闭后标题消失即判已关。
        """
        try:
            results = ocr_fullscreen(self.screen())
            return find_text(results, '宠友雇佣加成排行榜') is not None
        except Exception as e:
            log(f'雇佣面板标题 OCR 失败，按未关闭处理: {e}')
            return True

    def _close_employ_panel(self, max_tries: int = 3) -> None:
        """点工作面板顶部"智力"坐标关闭雇佣面板（面板是弹层，点面板外即关闭）。

        雇佣面板打开时工作面板顶部的"力量/智力/魅力"仍在屏幕上方（面板外），
        整屏 OCR 出"智力"坐标点击即关闭弹层（实测有效，比 × 按钮更稳）；
        关闭后用 OCR 标题确认已关，没关就重试。
        """
        for attempt in range(1, max_tries + 1):
            results = ocr_fullscreen(self.screen())
            hit = find_text(results, '智力')
            if not hit:
                log(f'未找到"智力"坐标，尝试 {attempt}/{max_tries}')
                time.sleep(CLICK_INTERVAL)
                continue
            log(f'点击"智力"关闭雇佣页面 ({hit[0]}, {hit[1]})，尝试 {attempt}/{max_tries}')
            self.click(hit[0], hit[1])
            time.sleep(CLICK_INTERVAL)
            if not self._employ_panel_open():
                log('雇佣页面已关闭')
                return
        raise RuntimeError(f'点击 {max_tries} 次仍未关闭雇佣页面')

    def _recover_work_start(self) -> None:
        """work_start 未出现（被"去照顾一下"弹窗挡住）时的恢复流程。

        点"去照顾一下" -> 一键护理（含"支付并护理"确认）-> back 回工作面板，
        之后 work_start 应重新出现。
        """
        care = self.see('go_care')
        if care:
            log(f'检测到"去照顾一下"，点击 ({care[0]}, {care[1]})')
            self.click(care[0], care[1])
            time.sleep(CLICK_INTERVAL)
        else:
            log('未找到"去照顾一下"按钮，跳过护理恢复')
            return
        hit = self.see('one_click_care')
        if hit:
            self.click(hit[0], hit[1])
            time.sleep(CLICK_INTERVAL)
            pay = self.see('one_click_pay')
            if pay:
                log('检测到"支付并护理"，点击确认')
                self.click(pay[0], pay[1])
                time.sleep(CLICK_INTERVAL)
        else:
            log('未找到一键护理按钮')
        back = self.see('back')
        if back:
            log(f'点击 back 回工作面板 ({back[0]}, {back[1]})')
            self.click(back[0], back[1])
            time.sleep(CLICK_INTERVAL)
        else:
            log('未找到 back 按钮')

    def _has_work_start(self) -> bool:
        """工作面板是否有"去打工"按钮：整屏 OCR（约 0.5s），比 dump 控件树快。"""
        return find_text(ocr_fullscreen(self.screen()), '去打工') is not None

    def _start_work(self) -> None:
        """点"去打工"开始工作，OCR 确认已开始（正在打工）或按钮消失。

        OCR 约 0.5s/次，替代原 click_until_gone_or_see 每轮 3 次控件树 dump（十几秒）。
        """
        clicked = False
        for attempt in range(1, 4):
            results = ocr_fullscreen(self.screen())
            if find_text(results, '正在打工'):
                log('开始工作: 已出现 work_in')
                return
            target = find_text(results, '去打工')
            if target:
                log(f'开始工作: 点击"去打工" ({target[0]}, {target[1]})')
                self.click(target[0], target[1])
                clicked = True
                time.sleep(CLICK_INTERVAL)
                continue
            if clicked:
                log('开始工作: work_start 已消失，进入下一阶段')
                return
            if attempt == 1 or attempt == 3:
                log(f'开始工作: 未找到 work_start，等待重试 ({attempt}/3)')
            time.sleep(CLICK_INTERVAL)
        raise RuntimeError('开始工作: 重试 3 次仍未出现 work_in')

    def wait_work_end(self) -> None:
        """等待 work_end 出现并点击 quit 退出；等待期间点"鼓励宠物"。"""
        self.wait_end('work_in', 'work_end', encourage=True)

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
            # work_start 没出现时，先处理"去照顾一下"弹窗（护理 + back 回工作面板）
            if not self._has_work_start():
                self._recover_work_start()
            # work_start 仍未出现（面板没加载出来/被关闭等）：重进打工地点再选一次，
            # 避免白等 3 次后直接重启设备；仍失败才交给上层重启恢复
            if not self._has_work_start():
                log('未回到工作面板，重新进入打工地点再选一次')
                self.select_place()
                self.select_job()
                self.hire_friend()
            self._start_work()
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
