"""统一执行器：按主页金币数量调度学习 / 打工。

调度逻辑：
- 所有任务开始之前：检查一次体力/清洁（主页展开状态面板 OCR），
  低于 care 阈值则喂食/洗澡到达标
- 冒险优先：当天到达 adventure.start_time 且冒险次数未满（adventure.times_per_day）
  -> 优先处理冒险，每次冒险后回主页面重新判断；当天次数用完后等第二天该时间再冒险
- 每日点数规则：学习次数 x school_factor + 打工次数 x work_factor
  超过 daily_point_limit 后，今天不再学习只打工，第二天次数自动清零
- 每轮先在主页面 OCR 金币数量（顶部状态栏最右侧数值）
- 金币 >= schedule.coin_threshold -> 优先学习
- 金币 < 阈值 -> 先打工（每次打工一轮后重新判断），赚够了自然切换去学习
- 金币识别失败 -> 默认先打工
- 首选场景当天已达上限 -> 换另一个；都达上限则结束

运行：python scenarios/runner.py                     （调度循环，Ctrl+C 停止）
单测：python scenarios/runner.py --test coins         （只测主页金币识别）
      python scenarios/runner.py --test work.select_place   （只跑某个阶段方法）
      python scenarios/runner.py --test school.select_course
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.coins import read_coins
from src.config import find_adb, load_config
from src.ocr import get_engine
from src.progress import (
    ADVENTURE_PROGRESS_FILE,
    SCHOOL_PROGRESS_FILE,
    WORK_PROGRESS_FILE,
    load_progress,
    log,
)
from src.u2dev import U2Device
from scenarios.adventure import AdventureScenario
from scenarios.care import CareScenario
from scenarios.school import ATTRIBUTE_COURSES, SchoolScenario
from scenarios.work import WorkScenario


class Runner:
    def __init__(self):
        # 启动时就加载 OCR 引擎（模型加载要几秒，避免第一轮调度才卡）
        log('加载 OCR 引擎...')
        get_engine()
        # 共享一个 u2 连接，避免每个场景重复连接和打印
        cfg = load_config()
        dev = U2Device(find_adb(cfg.adb.path), cfg.adb.device_serial)
        self.school = SchoolScenario(dev)
        self.work = WorkScenario(dev)
        self.adventure = AdventureScenario(dev)
        self.care = CareScenario(dev)
        sched = self.school.cfg.schedule
        self.threshold = sched.coin_threshold
        self.school_factor = sched.school_factor
        self.work_factor = sched.work_factor
        self.daily_point_limit = sched.daily_point_limit
        adv = self.school.cfg.adventure
        self.adventure_times = adv.times_per_day
        start_time = adv.start_time
        if isinstance(start_time, int):
            # YAML 1.1 会把不带引号的 9:00 解析成分钟数 540，转回 HH:MM
            start_time = f'{start_time // 60:02d}:{start_time % 60:02d}'
        try:
            self.adventure_start = datetime.strptime(start_time, '%H:%M').time()
        except ValueError:
            raise ValueError(
                f'config.yaml 中 adventure.start_time 格式无效: {adv.start_time!r}，应为 HH:MM')
        log(f'金币阈值: {self.threshold}，'
            f'每日点数上限: 学习x{self.school_factor}+打工x{self.work_factor} '
            f'> {self.daily_point_limit} 后只打工，'
            f'冒险: 每天 {self.adventure_times} 次 @ {start_time}')

    def adventure_due(self) -> bool:
        """是否该冒险了：已到调度时间且当天次数未满。"""
        if not self.adventure_times:
            return False
        _, done, _ = load_progress(ADVENTURE_PROGRESS_FILE, quiet=True)
        if done >= self.adventure_times:
            return False
        return datetime.now().time() >= self.adventure_start

    def today_points(self) -> tuple[int, int, int]:
        """当天 (学习次数, 打工次数, 点数)。"""
        _, learned, _ = load_progress(SCHOOL_PROGRESS_FILE, quiet=True)
        _, worked, _ = load_progress(WORK_PROGRESS_FILE, quiet=True)
        return learned, worked, learned * self.school_factor + worked * self.work_factor

    def read_main_coins(self) -> int | None:
        """回主页面 OCR 金币数量，失败返回 None。"""
        self.school.ensure_main_page()
        coins = read_coins(self.school.screen())
        if coins is None:
            log('金币 OCR 识别失败')
        return coins

    def run_one(self, scen, name: str) -> bool:
        """跑一个场景一轮（一节课/一次打工）。

        返回 False 或抛 RuntimeError 都视为该场景今天不再可用。
        """
        try:
            return scen.run(max_rounds=1)
        except RuntimeError as e:
            log(f'{name} 执行失败: {e}，今天不再执行该场景')
            return False

    def reload_config(self) -> None:
        """每轮重新读取 config.yaml，设置页热修改无需重启即生效（adb 连接除外）。

        各场景每轮（一节课/一次打工）执行时都读自己的字段，
        所以这里更新字段后下一轮自然生效。
        """
        try:
            cfg = load_config()
        except Exception as e:
            log(f'配置读取失败，沿用旧配置: {e}')
            return
        sched = cfg.schedule
        self.threshold = sched.coin_threshold
        self.school_factor = sched.school_factor
        self.work_factor = sched.work_factor
        self.daily_point_limit = sched.daily_point_limit

        adv = cfg.adventure
        self.adventure_times = adv.times_per_day
        start_time = adv.start_time
        if isinstance(start_time, int):
            # YAML 1.1 会把不带引号的 9:00 解析成分钟数 540，转回 HH:MM
            start_time = f'{start_time // 60:02d}:{start_time % 60:02d}'
        try:
            self.adventure_start = datetime.strptime(start_time, '%H:%M').time()
        except ValueError:
            log(f'冒险时间格式无效 {start_time!r}，沿用旧值')

        self.care.energy_threshold = cfg.care.energy_threshold
        self.care.clean_threshold = cfg.care.clean_threshold
        self.school.times_per_day = cfg.school.times_per_day
        if cfg.school.attribute in ATTRIBUTE_COURSES:
            self.school.attribute = cfg.school.attribute
        else:
            log(f'属性点配置无效 {cfg.school.attribute!r}，沿用 {self.school.attribute}')
        self.work.location = cfg.work.location
        self.work.times_per_day = cfg.work.times_per_day
        self.work.employ_scroll_limit = cfg.work.employ_scroll_limit

    def run(self) -> None:
        school_dead = False  # 学习今天不再可用（达上限/没有课程/执行失败）
        work_dead = False    # 打工今天不再可用
        adventure_dead = False  # 冒险今天不再可用（执行失败）
        while True:
            # 热修改：每轮调度前重读配置，设置页存盘最迟下一轮生效
            self.reload_config()

            # 所有任务开始之前：检查一次体力/清洁，不足则喂食/洗澡
            try:
                self.care.check_and_care()
            except RuntimeError as e:
                log(f'宠物状态检查/照顾失败: {e}，继续调度')

            # 冒险优先：到达调度时间且当天次数未满
            if not adventure_dead and self.adventure_due():
                log('到达冒险调度时间，优先处理冒险')
                if self.run_one(self.adventure, '冒险'):
                    _, adv_done, _ = load_progress(ADVENTURE_PROGRESS_FILE, quiet=True)
                    if adv_done >= self.adventure_times:
                        log(f'今日冒险 {adv_done}/{self.adventure_times} 次已满，'
                            f'明天 {self.adventure_start.strftime("%H:%M")} 后再冒险')
                    continue
                adventure_dead = True
                log('冒险执行失败，今天不再冒险')

            learned, worked, points = self.today_points()
            over_limit = points > self.daily_point_limit
            log(f'今日点数: 学习{learned}次x{self.school_factor}+打工{worked}次x{self.work_factor}'
                f' = {points} / {self.daily_point_limit}')
            if over_limit:
                # 每日点数超限：今天不再学习，只打工直到第二天清零
                log('点数超限，今天不再学习，只打工')

            try:
                coins = None if over_limit else self.read_main_coins()
            except RuntimeError as e:
                log(f'金币读取失败: {e}，默认先去打工')
                coins = None
            prefer_school = not over_limit and coins is not None and coins >= self.threshold
            if not over_limit:
                if coins is None:
                    log('金币识别失败，默认先去打工')
                elif prefer_school:
                    log(f'金币 {coins} >= 阈值 {self.threshold}，去学习')
                else:
                    log(f'金币 {coins} < 阈值 {self.threshold}，先去打工')

            first = (self.school, '学习', school_dead) if prefer_school else (self.work, '打工', work_dead)
            second = (self.work, '打工', work_dead) if prefer_school else (self.school, '学习', school_dead)
            if over_limit:
                # 点数超限时只打工，不回退到学习
                if work_dead:
                    log('打工当天已达上限，结束')
                    return
                if not self.run_one(self.work, '打工'):
                    work_dead = True
                    log('打工当天已达上限，结束')
                    return
                continue
            if not first[2] and self.run_one(first[0], first[1]):
                continue
            if not first[2]:
                log(f'{first[1]}当天不可继续，切换到另一个')
                if prefer_school:
                    school_dead = True
                else:
                    work_dead = True
            if not second[2] and self.run_one(second[0], second[1]):
                continue
            if not second[2]:
                if prefer_school:
                    work_dead = True
                else:
                    school_dead = True
            if school_dead and work_dead:
                log('学习和打工都已达当天上限，结束')
                return


def run_test(name: str) -> None:
    """单模块测试：coins 或 <school|work>.<方法名>（手机需已在对应界面）。"""
    if name == 'coins':
        sc = SchoolScenario()  # 任意场景实例，仅借用设备与主页面导航
        sc.ensure_main_page()
        coins = read_coins(sc.screen())
        log(f'金币数量: {coins if coins is not None else "识别失败"}')
        return

    scen_name, _, method = name.partition('.')
    scenarios = {'school': SchoolScenario, 'work': WorkScenario,
                 'adventure': AdventureScenario, 'care': CareScenario}
    if scen_name not in scenarios or not method:
        raise ValueError(f'--test 参数无效: {name!r}，应为 coins 或 school./work./adventure./care. 开头的方法名')
    scen = scenarios[scen_name]()
    fn = getattr(scen, method, None)
    if not callable(fn):
        raise ValueError(f'{scen_name} 场景没有方法: {method!r}')
    log(f'单测 {name} ...')
    result = fn()
    log(f'{name} 返回: {result}')


if __name__ == '__main__':
    import argparse

    ap = argparse.ArgumentParser(description='统一执行器：按金币调度学习/打工')
    ap.add_argument('--test', metavar='TARGET',
                    help='单模块测试: coins 或 school.<方法名> / work.<方法名>')
    args = ap.parse_args()

    try:
        if args.test:
            run_test(args.test)
        else:
            Runner().run()
    except KeyboardInterrupt:
        log('手动停止')
