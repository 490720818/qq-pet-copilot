"""学校上课场景。

流程（u2 控件/OCR 文字定位，分辨率无关）：
1. 主页面（main_sign="出门"）-> 点击 leave_home 出门
2. 出门后若正在上课/工作/冒险/被雇佣中（school_in / work_in / adventure_in / employed_in）
   -> 等待结束并退出，等完的课程/工作计入对应场景的当天次数，
      回主页面结束本轮，由执行器重新判断限制条件后再决定下一步
3. 每 1 秒点击一次 school，直到出现 school_start 按钮；
   若出现毕业标志（"去找同学玩"——毕业时学校面板没有"去上课"），
   点"关闭"再点两次 back 回主页面，重新进学校选择下一阶段课程
4. 选课：先 OCR 上半屏识别学园阶段（初级/中级学园课程顺序固定为
   力量/智力/魅力；高级学园/进修学院固定为 魅力/力量/智力，
   每次上课前重新判断），再把第一框拖到第三框归位（两次），点击对应选择框
5. 点击 school_start，直到页面出现 school_in 标志（进入上课）
6. 上课中：按配置的检查间隔（schedule.check_interval）检查，直到出现 school_end 标志
7. 点击 quit 结束，当天已学次数 +1 并持久化到 runs/school_progress.json
   （含 history 字段按日期保存每天的学习次数，跨天自动归档）
8. 一轮只上一节课就返回（供执行器逐节判断金币）；没有更多课程时结束

运行：python scenarios/school.py            （Ctrl+C 停止）
      python scenarios/school.py --times 5  （覆盖配置的每天学习次数，0 为不限）
"""

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.ocr import ocr_texts
from src.progress import (
    SCHOOL_PROGRESS_FILE,
    count_cross,
    load_progress,
    log,
    log_history,
    record_study_finish,
    save_progress,
    set_current_school,
)
from src.scenario import CLICK_INTERVAL, DeviceScenario, NAV_TIMEOUT

# 属性点 -> 三栏选择框定位名（力量/智力/魅力 对应第一/二/三框；初级/中级学园用）
ATTRIBUTE_COURSES = {
    '力量': 'select_box_1',
    '智力': 'select_box_2',
    '魅力': 'select_box_3',
}

# 高级学园的课程顺序固定为 魅力/力量/智力（与初级/中级不同，
# 选课前需 OCR 上半屏识别学园阶段来决定点哪个框）
ADVANCED_ATTRIBUTE_COURSES = {
    '魅力': 'select_box_1',
    '力量': 'select_box_2',
    '智力': 'select_box_3',
}
# 进修学院的课程顺序固定为 力量/魅力/智力（与高级学园不同）
INSTITUTE_ATTRIBUTE_COURSES = {
    '力量': 'select_box_1',
    '魅力': 'select_box_2',
    '智力': 'select_box_3',
}
ADVANCED_STAGES = ('高级学园', '进修学院')
# 面板标题识别规则（在选课页检测，页面只有一个学园标题，不需要靠编号/后缀防误判）：
# - 初级/中级/高级学园：形如"初级学园 5年级"（年级可省略）
# - 进修学院：形如"进修学院 研修生12" / "进修学院 研修生Ⅰ"，真实 OCR 常把编号
#   （Ⅰ/Ⅱ 等罗马数字实际ocr可能出现缺读）（如"进修学院研修生"），
#   所以只要求"进修学院"出现即可，"研修生"可选
_STAGE_RE = re.compile(
    r'(初级学园|中级学园|高级学园)(?:\s*[\d一二三四五六七八九十]+\s*年级)?'
    r'|(进修学院)(?:\s*研修生)?')

PROGRESS_FILE = SCHOOL_PROGRESS_FILE


class SchoolScenario(DeviceScenario):
    def __init__(self, dev=None):
        super().__init__(dev)
        self.attribute = self.cfg.school.attribute
        if self.attribute not in ATTRIBUTE_COURSES:
            raise ValueError(
                f'config.yaml 中 school.attribute 配置无效: {self.attribute!r}，'
                f'可选: {"/".join(ATTRIBUTE_COURSES)}'
            )
        self.times_per_day = self.cfg.school.times_per_day
        # 毕业处理防循环标志：关闭毕业面板后重新进学校仍出现毕业标志时抛异常，
        # 走重试链而不是无限"毕业->回主页面->再进"空转；成功看到 school_start 时重置
        self._graduated_once = False
        log(f'属性点: {self.attribute}，每天学习次数: '
            f'{self.times_per_day if self.times_per_day else "不限"}')

    # ---- 各阶段 ----

    def goto_school(self) -> str | None:
        """主页面 -> 出门 -> 反复点学校直到出现 school_start。

        出门后若正在上课/工作/冒险/被雇佣中（上次中途停止），等待结束并退出、回主页面，
        返回等完的是哪种（'school' / 'work' / 'adventure' / 'employed'）——此时不再继续进学校，
        由调用方/执行器重新判断限制条件后再决定下一步；
        学校面板出现毕业标志（"去找同学玩"，没有"去上课"）时，点"关闭"再点两次 back
        回主页面，返回 'graduated'，由 run() 重新进学校选择下一阶段课程；
        正常情况返回 None。
        """
        self.leave_home()
        finished = self.wait_busy_end()
        if finished:
            self.ensure_main_page()
            return finished
        try:
            clicked = False
            for attempt in range(1, NAV_TIMEOUT + 1):
                source = self.dev.hierarchy()
                if self.see('school_start', None, source):
                    self._graduated_once = False
                    return None
                if self.see('school_graduated', None, source):
                    if self._graduated_once:
                        raise RuntimeError('毕业面板关闭后重新进学校仍出现毕业标志')
                    self._graduated_once = True
                    self._close_graduation()
                    return 'graduated'
                school = self.see('school', None, source)
                if school:
                    self.click(school[0], school[1])
                    clicked = True
                elif clicked:
                    # 学校气泡点完消失但面板标志没识别到：已进入面板，继续选课
                    log('前往学校: school 已消失，进入选课')
                    return None
                else:
                    log(f'前往学校: 未找到 school，等待重试 ({attempt}/{NAV_TIMEOUT})')
                time.sleep(CLICK_INTERVAL)
            raise RuntimeError(f'前往学校: 重试 {NAV_TIMEOUT} 次仍未出现 school_start')
        except RuntimeError:
            # 正在打工/冒险等时点学校入口不会进入选课面板，导航必然超时；
            # 屏幕早已稳定，重新检测一次进行中状态再下结论
            finished = self._recheck_busy_after_nav('前往学校')
            if finished:
                return finished
            raise

    def _close_graduation(self) -> None:
        """毕业面板：点"关闭"按钮，再点两次 back 回主页面。"""
        log('检测到毕业标志（去找同学玩），点关闭并回主页面重新进学校')
        close = self.see('school_graduate_close')
        if not close:
            raise RuntimeError('毕业面板未找到"关闭"按钮')
        self.click(close[0], close[1])
        time.sleep(CLICK_INTERVAL)
        for _ in range(2):
            if not self.go_back():
                raise RuntimeError('毕业面板关闭后未找到 back 按钮')
            time.sleep(CLICK_INTERVAL)

    def select_course(self) -> None:
        """选课：先 OCR 上半屏识别学园阶段决定点哪个框，
        再把第一框拖到第三框归位（两次）后点选。"""
        box = self.resolve_course_box()
        self.reset_select_boxes(drags=3)
        log(f'选择课程: {self.attribute} ({box})')
        hit = self.see(box)
        if not hit:
            raise RuntimeError(f'未定位到课程选择框: {box}')
        time.sleep(CLICK_INTERVAL)
        self.click(hit[0], hit[1])


    def resolve_course_box(self) -> str:
        """OCR 上半屏识别学园阶段，返回该点哪个课程选择框。

        初级/中级学园课程顺序固定 力量/智力/魅力 -> 第一/二/三框；
        高级学园固定 魅力/力量/智力；进修学院固定 力量/魅力/智力。
        识别不到阶段回退默认顺序（初级/中级的 力量/智力/魅力）。
        """
        screen = self.screen()
        results = ocr_texts(screen[: screen.shape[0] // 2])
        stage = self._detect_stage(results)
        if stage:
            # 学习开始时把当前学园持久化到 school_progress.json（不一致才更新，
            # 结算时按它累计学习时长：初级10/中级20/高级30/进修45 分钟）
            set_current_school(stage)
        if stage == '进修学院':
            box = INSTITUTE_ATTRIBUTE_COURSES[self.attribute]
            log(f'学园阶段: {stage}，课程顺序 力量/魅力/智力，{self.attribute} -> {box}')
            return box
        if stage in ADVANCED_STAGES:
            box = ADVANCED_ATTRIBUTE_COURSES[self.attribute]
            log(f'学园阶段: {stage}，课程顺序 魅力/力量/智力，{self.attribute} -> {box}')
            return box
        box = ATTRIBUTE_COURSES[self.attribute]
        log(f'学园阶段: {stage or "未识别"}，课程顺序 力量/智力/魅力，{self.attribute} -> {box}')
        return box

    @staticmethod
    def _detect_stage(results: list[tuple[str, int, int, float]]) -> str | None:
        """从上半屏 OCR 结果里识别学园阶段，返回匹配到的阶段名或 None。

        用子串包含匹配（不是精确相等）：实际文案带年级后缀（'初级学园 5年级'）、
        图标前缀等都能命中；单个文本块没命中时再拼全部文本兜底（防止拆块）。
        """
        for text, *_ in results:
            m = _STAGE_RE.search(text.replace(' ', ''))
            if m:
                return m.group(1) or m.group(2)
        merged = ''.join(t.replace(' ', '') for t, *_ in results)
        m = _STAGE_RE.search(merged)
        return (m.group(1) or m.group(2)) if m else None

    def wait_class_end(self) -> bool:
        """等待下课并点击 quit。返回 True 表示还能继续学。"""
        self.wait_end('school_in', 'school_end', encourage=True)
        again = self.see('school_start')
        if again:
            log('还可以继续学习')
            return True
        log('没有 school_start 了，返回主页面')
        return False

    def attend_class(self) -> bool:
        """选课 -> 开始学习 -> 等待下课 -> quit。返回 True 表示还能继续学。"""
        self.select_course()
        self.click_until_gone_or_see('school_start', 'school_in', '开始学习')
        log('已进入课堂，等待下课...')
        if self.defer_wait:
            # 延时收尾：进行中登记 pending（到点由调度器 finish_pending 收尾）或
            # 已结束原地收尾，然后回主页面；计数统一走 on_finish（count_cross），
            # 本地 learned 不再自增，防止重复计数
            self.defer_busy_end('school_in', 'school_end',
                                lambda: count_cross('school'), '上课', encourage=True)
            self.ensure_main_page()
            return False
        return self.wait_class_end()

    def run(self, max_times: int | None = None, max_rounds: int = 0) -> bool:
        """max_times: 当天学习次数上限，0 表示不限；None 表示用配置值。
        max_rounds: 最多跑多少轮后返回，0 为不限。
        一轮 = 回主页面进学校上一节课，课后返回主页面（供执行器逐节判断金币）。
        返回本次调用是否完成了至少一次学习。
        """
        if max_times is None:
            max_times = self.times_per_day
        today, learned, history = load_progress(PROGRESS_FILE)
        log_history(history, today)
        start_learned = learned
        if max_times and learned >= max_times:
            log(f'今天已学满 {max_times} 次，无需再学')
            return False
        round_no = 0
        while True:
            round_no += 1
            log(f'===== 第 {round_no} 轮 =====')
            self.ensure_main_page()
            finished = self.goto_school()
            if finished:
                if finished == 'graduated':
                    # 毕业面板已关闭并回主页面：不计数也不算一轮（毕业不是上课），
                    # 立即重新进学校选下一阶段课程，不等下一轮调度（否则要等
                    # success_interval 才重试）；防循环由 goto_school 的
                    # _graduated_once 保证（连续毕业抛异常走重试链）
                    log('毕业处理完成，重新进学校')
                    continue
                if self.pending is not None:
                    # 延时收尾模式：出门检测到的进行中活动已登记 pending，计数由
                    # finish_pending 收尾时统一进行（on_finish），本地不再计数，
                    # 本轮直接结束
                    return True
                if finished == 'school':
                    # 出门时等完了一节上次未结束的课，计入当天次数 + 学习时长
                    learned += 1
                    save_progress(PROGRESS_FILE, today, learned, history)
                    record_study_finish()
                    log(f'已完成第 {learned} 次学习' + (f' / 目标 {max_times} 次' if max_times else ''))
                    if max_times and learned >= max_times:
                        log('达到当天学习次数，结束')
                        return True
                elif finished != 'employed':
                    # 出门时等完的是别的活动（打工/冒险），计入对应次数
                    # （被雇佣在召回点 quit 时已计数）
                    count_cross(finished)
                # 等完了一次活动，计数已变化，本轮结束，
                # 回主页面交由执行器重新判断限制条件
                if max_rounds and round_no >= max_rounds:
                    return True
                log('本轮结束，回主页面重新开始')
                continue
            cont = self.attend_class()
            if self.defer_wait:
                # 延时收尾模式：计数在 pending 收尾时统一进行，本轮直接结束
                return True
            learned += 1
            save_progress(PROGRESS_FILE, today, learned, history)
            record_study_finish()
            log(f'已完成第 {learned} 次学习' + (f' / 目标 {max_times} 次' if max_times else ''))
            if max_times and learned >= max_times:
                log('达到当天学习次数，结束')
                return learned > start_learned
            if not cont:
                log('本次没有更多课程了')
            if max_rounds and round_no >= max_rounds:
                log(f'已跑完 {max_rounds} 轮，返回')
                return learned > start_learned
            log('本轮结束，回主页面重新开始')


if __name__ == '__main__':
    import argparse

    ap = argparse.ArgumentParser(description='学校上课场景')
    ap.add_argument('--times', type=int, default=None,
                    help='当天学习次数上限，0 为不限；不指定则读 config.yaml 的 school.times_per_day')
    args = ap.parse_args()

    try:
        SchoolScenario().run(max_times=args.times)
    except KeyboardInterrupt:
        log('手动停止')
