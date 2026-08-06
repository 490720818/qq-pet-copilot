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
- 踩踩/PK：到达各自 start_time 且当天次数未满时处理；
  场景长等待（上课/打工/冒险进行中）的间隙也会插空处理（wait_hook，run_inline）；
  PK 每轮最多 16 局（超出下一轮接着跑），开始前检查体力/清洁
  （每局各耗 5，不足则喂食/洗澡到 90）
- 异常分级重试：场景执行抛异常 -> 先回主页面重进场景重试一次（页面状态
  错乱多半能自愈，不必重启）-> 仍失败才 adb reboot 重启设备 -> 启动 QQ ->
  点 Q宠-* 入口回宠物页面（src/recover.py）-> 最后再试一次；
  连续恢复 RECOVERY_LIMIT 次仍失败则放弃恢复
- 多次重试仍失败按任务类型分流：
  学习/打工是主任务 -> 发告警通知（src/notify.py：Windows Toast / OnePush，
  见 config.yaml 的 notify 段；附当前手机屏幕截图）后退出调度器，
  不静默挂起空跑——设备/游戏状态异常需要人工介入；
  冒险/踩踩/PK 是支线任务 -> 重新排期延后 SIDE_TASK_RETRY_DELAY 秒重试
  （参考 qq-farm-copilot 的 failure_interval 队列机制），先执行其他任务；
  主任务当天结束后若还有延后重试的支线任务，调度器睡到重试点继续，不提前退出

运行：python scenarios/runner.py                     （调度循环，Ctrl+C 停止）
单测：python scenarios/runner.py --test coins         （只测主页金币识别）
      python scenarios/runner.py --test recover       （只测异常恢复链路：reboot -> 重进宠物页）
      python scenarios/runner.py --test work.select_place   （只跑某个阶段方法）
      python scenarios/runner.py --test school.select_course
"""

import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.coins import read_coins
from src.config import PROJECT_ROOT, find_adb, load_config
from src.notify import send_alert
from src.ocr import get_engine
from src.progress import (
    ADVENTURE_PROGRESS_FILE,
    PK_PROGRESS_FILE,
    SCHOOL_PROGRESS_FILE,
    VISIT_PROGRESS_FILE,
    WORK_PROGRESS_FILE,
    load_progress,
    log,
)
from src.recover import reenter_pet
from src.status_cache import update_status
from src.u2dev import U2Device
from PIL import Image
from scenarios.adventure import AdventureScenario
from scenarios.care import CareScenario
from scenarios.pk import PKScenario
from scenarios.school import ATTRIBUTE_COURSES, SchoolScenario
from scenarios.visit import VisitScenario
from scenarios.work import WorkScenario

# 连续异常恢复（adb reboot）次数上限，超过认为设备/环境有硬故障，放弃
RECOVERY_LIMIT = 3
# 支线任务（冒险/踩踩/PK）多次重试仍失败后的延后重试间隔（秒）：
# 参考 qq-farm-copilot 的 failure_interval 队列机制——失败任务重新排期，
# 调度器先执行其他任务，到点后 due() 自动放行重试
SIDE_TASK_RETRY_DELAY = 1800


class ScenarioFailed(Exception):
    """支线任务多次重试仍失败（内部信号：调用方捕获后重新排期，不退出调度器）。"""


def parse_hhmm(value, field: str):
    """解析 HH:MM 时间配置（YAML 1.1 会把不带引号的 9:00 解析成分钟数 540）。"""
    if isinstance(value, int):
        value = f'{value // 60:02d}:{value % 60:02d}'
    try:
        return datetime.strptime(value, '%H:%M').time()
    except ValueError:
        raise ValueError(f'config.yaml 中 {field} 格式无效: {value!r}，应为 HH:MM') from None


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
        self.visit = VisitScenario(dev)
        self.pk = PKScenario(dev)
        self.recoveries = 0  # 连续异常恢复次数（成功跑完一轮清零）
        self.retry_after: dict[str, datetime] = {}  # 支线任务名 -> 失败后的下次可执行时间
        self.visit_dead = False  # 踩踩今天不再可用（执行失败）
        self.pk_dead = False     # PK 今天不再可用（执行失败）
        # 场景长等待（上课/打工进行中）的间隙插空处理踩踩/PK
        for scen in (self.school, self.work, self.adventure, self.care):
            scen.wait_hook = self._idle_wait_hook
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
        visit = self.school.cfg.visit
        self.visit_times = visit.times_per_day
        self.visit_start = parse_hhmm(visit.start_time, 'visit.start_time')
        pk = self.school.cfg.pk
        self.pk_times = pk.times_per_day
        self.pk_start = parse_hhmm(pk.start_time, 'pk.start_time')
        log(f'踩踩: 每天 {self.visit_times} 次 @ {self.visit_start.strftime("%H:%M")}'
            f'（等待间隙插空处理），'
            f'PK: 每天 {self.pk_times} 次 @ {self.pk_start.strftime("%H:%M")}')

    def _deferred(self, name: str) -> bool:
        """支线任务是否处于失败延后期（未到重新排期时间）。"""
        return datetime.now() < self.retry_after.get(name, datetime.min)

    def adventure_due(self) -> bool:
        """是否该冒险了：已到调度时间、当天次数未满且不在失败延后期。"""
        if not self.adventure_times or self._deferred('冒险'):
            return False
        _, done, _ = load_progress(ADVENTURE_PROGRESS_FILE, quiet=True)
        if done >= self.adventure_times:
            return False
        return datetime.now().time() >= self.adventure_start

    def visit_due(self) -> bool:
        """是否该踩踩了：已到调度时间、当天次数未满且不在失败延后期。"""
        if not self.visit_times or self._deferred('踩踩'):
            return False
        _, done, _ = load_progress(VISIT_PROGRESS_FILE, quiet=True)
        if done >= self.visit_times:
            return False
        return datetime.now().time() >= self.visit_start

    def pk_due(self) -> bool:
        """是否该 PK 了：已到调度时间、当天次数未满且不在失败延后期。"""
        if not self.pk_times or self._deferred('PK'):
            return False
        _, done, _ = load_progress(PK_PROGRESS_FILE, quiet=True)
        if done >= self.pk_times:
            return False
        return datetime.now().time() >= self.pk_start

    def _idle_wait_hook(self) -> None:
        """场景长等待间隙的回调：到点且没满则插空处理踩踩/PK（scenario.wait_hook）。"""
        if not self.visit_dead and self.visit_due():
            log('等待间隙：插空处理踩踩')
            if self.visit.run_inline():
                log('等待间隙踩踩完成')
        if not self.pk_dead and self.pk_due():
            log('等待间隙：插空处理 PK')
            if self.pk.run_inline():
                log('等待间隙 PK 完成')

    def today_points(self) -> tuple[int, int, int]:
        """当天 (学习次数, 打工次数, 点数)。"""
        _, learned, _ = load_progress(SCHOOL_PROGRESS_FILE, quiet=True)
        _, worked, _ = load_progress(WORK_PROGRESS_FILE, quiet=True)
        return learned, worked, learned * self.school_factor + worked * self.work_factor

    def read_main_coins(self) -> int | None:
        """回主页面 OCR 金币数量，失败返回 None。识别成功写状态缓存（GUI 状态条）。"""
        self.school.ensure_main_page()
        coins = read_coins(self.school.screen())
        if coins is None:
            log('金币 OCR 识别失败')
        else:
            update_status(None, coins=coins)
        return coins

    def run_one(self, scen, name: str, fatal: bool = True) -> bool:
        """跑一个场景一轮（一节课/一次打工）。

        抛异常分级重试：先回主页面重进场景试一次（页面状态错乱多半能自愈，
        不必重启设备），仍失败才走 adb reboot 恢复（重进宠物页面）后试最后一次。
        都失败时按任务类型分流：
        - fatal=True（学习/打工主任务）：发告警通知并退出调度器
          （设备/游戏状态异常，需要人工介入，不静默挂起空跑）；
        - fatal=False（冒险/踩踩/PK 支线任务）：抛 ScenarioFailed，
          由调度循环重新排期延后重试，先执行其他任务。
        场景 run() 正常返回 False（达当天上限等）不算失败。
        """
        try:
            return self._run_round(scen)
        except Exception as e:
            log(f'{name} 执行异常: {e}，回主页面重试一次')
        try:
            return self._run_round(scen)
        except Exception as e:
            log(f'{name} 回主页面重试仍失败: {e}，尝试重启设备恢复')
        if self.recover():
            try:
                return self._run_round(scen)
            except Exception as e:
                last = f'{name} 恢复后重试仍失败: {e}'
        else:
            last = f'{name} 恢复失败'
        if fatal:
            self._alert_and_exit(last)
        raise ScenarioFailed(last)

    def _reschedule(self, name: str) -> None:
        """支线任务多次重试仍失败：延后 SIDE_TASK_RETRY_DELAY 秒重试
        （参考 qq-farm-copilot 的失败重排期），先执行其他任务。"""
        at = datetime.now() + timedelta(seconds=SIDE_TASK_RETRY_DELAY)
        self.retry_after[name] = at
        log(f'{name} 多次重试仍失败，延后到 {at:%H:%M} 重试，先执行其他任务')

    def _wait_for_deferred(self, adventure_dead: bool) -> bool:
        """主任务（学习/打工）当天结束后，若还有延后重试的支线任务，
        睡到最近的重试点再继续调度（返回 True）；没有则返回 False（正常结束）。

        不等待就直接退出会让延后重试永远不发生——调度器是长驻进程，
        支线任务到点重试完（或再次失败重新排期）后才真正收工。
        """
        dead = {'冒险': adventure_dead, '踩踩': self.visit_dead, 'PK': self.pk_dead}
        future = [at for name, at in self.retry_after.items()
                  if at > datetime.now() and not dead.get(name, False)]
        if not future:
            return False
        at = min(future)
        log(f'学习/打工当天已结束，还有支线任务延后到 {at:%H:%M} 重试，调度器等待')
        time.sleep(max(1.0, (at - datetime.now()).total_seconds()))
        return True

    def _alert_and_exit(self, reason: str) -> None:
        """多次重试仍失败：发告警通知（附当前手机屏幕截图）后退出调度器。"""
        log(f'{reason}，发送告警通知并退出调度器')
        send_alert(reason, self._capture_alert_image())
        raise SystemExit(1)

    def _capture_alert_image(self) -> str | None:
        """告警时截取当前手机屏幕存到 runs/，随通知附上；失败不阻塞告警。"""
        try:
            path = PROJECT_ROOT / 'runs' / f'alert_{datetime.now():%Y%m%d_%H%M%S}.png'
            Image.fromarray(self.school.dev.screenshot()).save(path)
            log(f'告警截图已保存: {path}')
            return str(path)
        except Exception as e:
            log(f'告警截图失败: {e}')
            return None

    def _run_round(self, scen) -> bool:
        result = scen.run(max_rounds=1)
        self.recoveries = 0  # 成功跑完一轮，重置连续恢复计数
        return result

    def recover(self) -> bool:
        """异常恢复：adb reboot -> 启动 QQ -> 点 Q宠-* 入口回宠物页面，
        并刷新各场景的设备连接。返回是否成功。"""
        if self.recoveries >= RECOVERY_LIMIT:
            log(f'已连续恢复 {self.recoveries} 次仍异常，放弃恢复')
            return False
        self.recoveries += 1
        try:
            dev = reenter_pet(self.school.dev.adb)
        except Exception as e:
            log(f'恢复失败: {e}')
            return False
        for scen in (self.school, self.work, self.adventure, self.care, self.visit, self.pk):
            scen.dev = dev
        log('恢复完成，继续调度')
        return True

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
        self.visit.times_per_day = cfg.visit.times_per_day
        self.visit_times = cfg.visit.times_per_day
        try:
            self.visit_start = parse_hhmm(cfg.visit.start_time, 'visit.start_time')
        except ValueError as e:
            log(f'{e}，沿用旧值')
        self.pk.times_per_day = cfg.pk.times_per_day
        self.pk_times = cfg.pk.times_per_day
        try:
            self.pk_start = parse_hhmm(cfg.pk.start_time, 'pk.start_time')
        except ValueError as e:
            log(f'{e}，沿用旧值')

    def run(self) -> None:
        school_dead = False  # 学习今天不再可用（达上限/没有课程/执行失败）
        work_dead = False    # 打工今天不再可用
        adventure_dead = False  # 冒险今天不再可用（执行失败）
        while True:
            try:
                # 热修改：每轮调度前重读配置，设置页存盘最迟下一轮生效
                self.reload_config()

                # 所有任务开始之前：检查一次体力/清洁，不足则喂食/洗澡；
                # 失败直接抛给外层：走 adb reboot 恢复链路（src/recover.py）
                self.care.check_and_care()

                # 冒险优先：到达调度时间且当天次数未满
                if not adventure_dead and self.adventure_due():
                    log('到达冒险调度时间，优先处理冒险')
                    try:
                        done = self.run_one(self.adventure, '冒险', fatal=False)
                    except ScenarioFailed:
                        self._reschedule('冒险')  # 延后重试，继续判断后面的任务
                    else:
                        if done:
                            _, adv_done, _ = load_progress(ADVENTURE_PROGRESS_FILE, quiet=True)
                            if adv_done >= self.adventure_times:
                                log(f'今日冒险 {adv_done}/{self.adventure_times} 次已满，'
                                    f'明天 {self.adventure_start.strftime("%H:%M")} 后再冒险')
                            continue
                        adventure_dead = True
                        log('冒险当天不可继续')

                # 踩踩：到达调度时间且当天次数未满（等待间隙也会插空处理）
                if not self.visit_dead and self.visit_due():
                    log('到达踩踩调度时间，处理踩踩')
                    try:
                        done = self.run_one(self.visit, '踩踩', fatal=False)
                    except ScenarioFailed:
                        self._reschedule('踩踩')
                    else:
                        if done:
                            continue
                        self.visit_dead = True
                        log('踩踩当天不可继续')

                # PK：到达调度时间且当天次数未满
                if not self.pk_dead and self.pk_due():
                    log('到达 PK 调度时间，处理 PK')
                    try:
                        done = self.run_one(self.pk, 'PK', fatal=False)
                    except ScenarioFailed:
                        self._reschedule('PK')
                    else:
                        if done:
                            continue
                        self.pk_dead = True
                        log('PK 当天不可继续')

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
                        if self._wait_for_deferred(adventure_dead):
                            continue
                        log('打工当天已达上限，结束')
                        return
                    if not self.run_one(self.work, '打工'):
                        work_dead = True
                        if self._wait_for_deferred(adventure_dead):
                            continue
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
                    if self._wait_for_deferred(adventure_dead):
                        continue
                    log('学习和打工都已达当天上限，结束')
                    return
            except Exception as e:
                # 兜底：循环体内未捕获的异常（u2 断开、设备卡死等）走重启恢复，
                # 恢复失败（连续 RECOVERY_LIMIT 次）发告警通知后退出
                log(f'调度循环异常: {e}')
                if not self.recover():
                    self._alert_and_exit(f'调度循环异常且恢复失败: {e}')


def run_test(name: str) -> None:
    """单模块测试：coins / recover 或 <school|work>.<方法名>（手机需已在对应界面）。"""
    if name == 'coins':
        sc = SchoolScenario()  # 任意场景实例，仅借用设备与主页面导航
        sc.ensure_main_page()
        coins = read_coins(sc.screen())
        log(f'金币数量: {coins if coins is not None else "识别失败"}')
        return
    if name == 'recover':
        # 异常恢复链路：adb reboot -> 启动 QQ -> 点 Q宠-* 入口回宠物页面
        sc = SchoolScenario()  # 任意场景实例，仅借用 adb 连接
        reenter_pet(sc.dev.adb)
        log('recover 测试完成')
        return

    scen_name, _, method = name.partition('.')
    scenarios = {'school': SchoolScenario, 'work': WorkScenario,
                 'adventure': AdventureScenario, 'care': CareScenario,
                 'visit': VisitScenario, 'pk': PKScenario}
    if scen_name not in scenarios or not method:
        raise ValueError(f'--test 参数无效: {name!r}，应为 coins / recover 或 school./work./adventure./care. 开头的方法名')
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
