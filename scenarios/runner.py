"""统一执行器：按主页金币数量调度学习 / 打工。

调度引擎（config.yaml 的 runner.engine）：
- task_queue（默认）：任务队列调度（TaskQueueRunner），执行顺序由 tasks.order
  配置（> 分隔，越靠前越优先，不在 order 里的任务不调度），每个任务有独立的
  enabled / trigger（interval 间隔 / daily 每日时间点）/ enabled_time_range /
  success_interval / failure_interval 调度设置（config.yaml 的 tasks 段）；
  冒险/学习/打工/雇佣好友互斥，作为主任务组按 tasks.main_order（默认
  学习>雇佣好友>冒险>打工）统一调度，且非阻塞等待：进行中 OCR 剩余时间后
  先调度其他任务，到点再收尾计数
- legacy：老主循环调度（Runner.run），顺序写死：护理检查 -> 冒险 -> 踩踩 -> PK
  -> 好友雇佣 -> 好友护理 -> 学习/打工

调度逻辑（两种引擎共通）：
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
- 踩踩/PK：到达各自 start_time 且当天次数未满时，在主页面处理；
  PK 每轮最多 16 局（超出下一轮接着跑），开始前检查体力/清洁
  （每局各耗 5，不足则喂食/洗澡到 90）
- 好友护理：friend_care.enabled 开启且配置了好友名称时，在 friend_care.time_range
  时间段内按 friend_care.interval_seconds 间隔调度：每次访问该好友家按
  friend_care.method 护理一次（单次巡检，不再场景内切换好友刷新状态）后回主页面
  （scenarios/friend_care.py）
- 好友雇佣：hire_friend.enabled 开启且配置了好友名称时，在 hire_friend.time_range
  时间段内按 hire_friend.interval_seconds 间隔调度且当天次数未满
  （hire_friend.times_per_day）时访问该好友家，OCR hire 按钮上的
  雇佣剩余 CD，有 CD 抛 TaskDeferred 延后 60 秒复测（不原地等待），
  没有 CD 点 hire 进打工面板（固定等 3 秒加载，重试点击），
  按打工流程 select_place 确认/重选打工地点后按 work.duration 选工作选择框
  （10分钟/45分钟/2小时 -> select_box_1/2/3）点 work_start
  打工一轮；打工结束后计数（雇佣好友 + 打工各一次）（scenarios/hire_friend.py）
- 异常分级重试：场景执行抛异常 -> 先回主页面重进场景重试一次（页面状态
  错乱多半能自愈，不必重启）-> 仍失败才 adb reboot 重启设备 -> 启动 QQ ->
  点 Q宠-* 入口回宠物页面（src/recover.py）-> 最后再试一次；
  连续恢复 RECOVERY_LIMIT 次仍失败则放弃恢复
- 多次重试仍失败按任务类型分流：
  学习/打工是主任务 -> 发告警通知（src/notify.py：Windows Toast / OnePush，
  见 config.yaml 的 notify 段；附当前手机屏幕截图）后退出调度器，
  不静默挂起空跑——设备/游戏状态异常需要人工介入；
  冒险/踩踩/PK/好友护理/好友雇佣 是支线任务 -> 重新排期延后 SIDE_TASK_RETRY_DELAY 秒重试
  （参考 qq-farm-copilot 的 failure_interval 队列机制），先执行其他任务；
  主任务当天结束后若还有延后重试的支线任务，调度器睡到重试点继续，不提前退出

模拟器（Root 模拟器）：python scenarios/runner.py --emulator [--emulator-device 127.0.0.1:7555]
  （QQ 搜索卡片空入口打不开宠物主页，用 qqpet-module-opener frida 注入打开）

运行：python scenarios/runner.py                     （调度循环，Ctrl+C 停止）
单测：python scenarios/runner.py --test coins         （只测主页金币识别）
      python scenarios/runner.py --test recover       （只测异常恢复链路：reboot -> 重进宠物页）
      python scenarios/runner.py --test opener        （模拟器：直接用 opener 打开宠物主页）
      python scenarios/runner.py --test work.select_place   （只跑某个阶段方法）
      python scenarios/runner.py --test school.select_course
"""

import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.adb.device import Device
from src.coins import read_coins
from src.config import (
    MAIN_TASK_KEYS,
    PROJECT_ROOT,
    TASK_KEYS,
    TaskItemConfig,
    find_adb,
    is_emulator_build,
    load_config,
)
from src.notify import send_alert
from src.opener import open_pet_page
from src.ocr import get_engine
from src.progress import (
    ADVENTURE_PROGRESS_FILE,
    HIRE_FRIEND_PROGRESS_FILE,
    PK_PROGRESS_FILE,
    SCHOOL_PROGRESS_FILE,
    VISIT_PROGRESS_FILE,
    WORK_PROGRESS_FILE,
    exp_daily_done,
    load_progress,
    log,
)
from src.queue_status import save_queue_status
from src.recover import reenter_pet
from src.scenario import StatBlocked, TaskDeferred
from src.status_cache import update_status
from src.u2dev import U2Device
from PIL import Image
from scenarios.adventure import AdventureScenario
from scenarios.care import CareScenario
from scenarios.employed import EmployedScenario
from scenarios.friend_care import FriendCareScenario, in_time_range, parse_time_range
from scenarios.hire_friend import FriendHireScenario
from scenarios.pk import PKDeferred, PKScenario
from scenarios.school import ATTRIBUTE_COURSES, SchoolScenario
from scenarios.visit import VisitScenario
from scenarios.work import DURATION_BOXES, WorkScenario

# 连续异常恢复（adb reboot）次数上限，超过认为设备/环境有硬故障，放弃
RECOVERY_LIMIT = 3
# 距上次恢复超过该时长后计数重置：只拦"短时间内连续恢复"的死循环，
# 支线任务长期失败（每 SIDE_TASK_RETRY_DELAY 重试一轮）不应永久锁死恢复能力
RECOVERY_RESET_AFTER = 3600
# 支线任务（冒险/踩踩/PK/好友护理/好友雇佣）多次重试仍失败后的延后重试间隔（秒）：
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
    def __init__(self, use_opener: bool = False, opener_serial: str | None = None,
                 skip_opener: bool = False):
        '''use_opener: 模拟器模式，用 qqpet-module-opener（frida 注入）打开宠物主页；
        opener_serial: 模拟器 ADB 地址（如 127.0.0.1:7555），默认用 config 的 adb.device_serial；
        skip_opener: 宠物主页已打开（如 GUI 手动重启刚恢复完），启动时跳过 opener 打开。'''
        self.use_opener = use_opener
        self.opener_serial = opener_serial
        self.skip_opener = skip_opener
        if use_opener:
            log('模拟器模式已开启，启动时用 opener 打开宠物主页' if not skip_opener
                else '模拟器模式已开启，宠物主页已就绪，启动跳过 opener 打开')
        else:
            log('未开启模拟器模式（源码运行需加 --emulator；打包的模拟器版默认开启）')
        # 启动时就加载 OCR 引擎（模型加载要几秒，避免第一轮调度才卡）
        log('加载 OCR 引擎...')
        get_engine()
        # 共享一个 u2 连接，避免每个场景重复连接和打印
        cfg = load_config()
        self._last_cfg = cfg  # 最近一次加载的配置（任务队列调度读 tasks 段用）
        serial = opener_serial or cfg.adb.device_serial
        if use_opener and serial and ':' in serial:
            # 模拟器（MuMu/雷电等 127.0.0.1:port）可能还没进 adb devices，先 connect 一次
            try:
                Device(find_adb(cfg.adb.path), serial).connect_remote(serial)
            except Exception as e:
                log(f'adb connect {serial} 失败: {e}')
        dev = U2Device(find_adb(cfg.adb.path), serial)
        self.school = SchoolScenario(dev)
        self.work = WorkScenario(dev)
        self.adventure = AdventureScenario(dev)
        self.care = CareScenario(dev)
        self.visit = VisitScenario(dev)
        self.pk = PKScenario(dev)
        self.friend_care = FriendCareScenario(dev)
        self.hire_friend = FriendHireScenario(dev)
        self.employed = EmployedScenario(dev)
        self.recoveries = 0  # 连续异常恢复次数（成功跑完一轮清零；距上次超过 RECOVERY_RESET_AFTER 也清零）
        self.last_recovery_at = 0.0  # 上次发起恢复的 monotonic 时间
        self.retry_after: dict[str, datetime] = {}  # 支线任务名 -> 失败后的下次可执行时间
        self.visit_dead = False  # 踩踩今天不再可用（执行失败）
        self.pk_dead = False     # PK 今天不再可用（执行失败）
        self.friend_care_dead = False  # 好友护理今天不再可用（执行失败）
        self.hire_friend_dead = False  # 好友雇佣今天不再可用（执行失败）
        self._fc_bad_range_logged = False  # 时间段格式错误只记一次日志（due() 每轮都调）
        self._hf_bad_time_logged = False   # 雇佣好友时间格式错误只记一次日志（同上）
        self._ec_bad_range_logged = False  # 被雇佣时间段格式错误只记一次日志（同上）
        self._employed_last_check = None   # 上次被雇佣检查时间（interval_seconds 起算点）
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
            f'，PK: 每天 {self.pk_times} 次 @ {self.pk_start.strftime("%H:%M")}')

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
        """是否该踩踩了：已到调度时间、当天次数未满且不在失败延后期；
        或踩满但经验日常未完成（需继续遍历好友做经验照顾）。"""
        if not self.visit_times or self._deferred('踩踩'):
            return False
        _, done, _ = load_progress(VISIT_PROGRESS_FILE, quiet=True)
        if done >= self.visit_times and exp_daily_done():
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

    def care_due(self) -> bool:
        """是否该做护理检查了：距上次检查已过 care.interval_seconds（默认 60 秒）。"""
        last = getattr(self.care, 'last_care_at', None)
        interval = max(1, int(getattr(self.care.cfg.care, 'interval_seconds', 60) or 60))
        return last is None or datetime.now() >= last + timedelta(seconds=interval)

    def friend_care_due(self) -> bool:
        """是否该好友护理了：已启用、配置了好友名称、当前时间在时间段内、
        距上次巡检已过 friend_care.interval_seconds 且不在失败延后期。"""
        fc = self.friend_care.cfg.friend_care
        if not fc.enabled or not fc.friend_name.strip() or self._deferred('好友护理'):
            return False
        last = getattr(self.friend_care, 'last_care_at', None)
        interval = max(0, int(getattr(fc, 'interval_seconds', 1800) or 0))
        if last is not None and datetime.now() < last + timedelta(seconds=interval):
            return False
        try:
            start, end = parse_time_range(fc.time_range)
        except ValueError as e:
            if not self._fc_bad_range_logged:
                log(f'{e}，好友护理不调度')
                self._fc_bad_range_logged = True
            return False
        self._fc_bad_range_logged = False
        return in_time_range(datetime.now().time(), start, end)

    def hire_friend_due(self) -> bool:
        """是否该雇佣好友了：已启用、配置了好友名称、当前在雇佣时间段内、
        距上次执行已过 hire_friend.interval_seconds、当天次数未满且不在失败延后期。
        纯查询无副作用：last_hire_at 由执行处记录——本方法每轮会被主任务组内
        多个任务的扫描重复调用（_main_choice），在判定里记时间会把雇佣卡死。"""
        hf = self.hire_friend.cfg.hire_friend
        if (not hf.enabled or not hf.times_per_day or not hf.friend_name.strip()
                or self._deferred('雇佣好友')):
            return False
        _, done, _ = load_progress(HIRE_FRIEND_PROGRESS_FILE, quiet=True)
        if done >= hf.times_per_day:
            return False
        last = getattr(self.hire_friend, 'last_hire_at', None)
        interval = max(1, int(getattr(hf, 'interval_seconds', 5) or 5))
        if last is not None and datetime.now() < last + timedelta(seconds=interval):
            return False
        try:
            start, end = parse_time_range(hf.time_range, 'hire_friend.time_range')
        except ValueError as e:
            if not self._hf_bad_time_logged:
                log(f'{e}，好友雇佣不调度')
                self._hf_bad_time_logged = True
            return False
        self._hf_bad_time_logged = False
        if not in_time_range(datetime.now().time(), start, end):
            return False
        return True

    def employed_due(self) -> bool:
        """是否该被雇佣检查了：已启用、当前在被雇佣时间段内、
        距上次检查已过 employed.interval_seconds 且不在失败延后期。"""
        ec = self.school.cfg.employed
        if not ec.enabled or self._deferred('被雇佣'):
            return False
        last = self._employed_last_check
        interval = max(1, int(getattr(ec, 'interval_seconds', 60) or 60))
        if last is not None and datetime.now() < last + timedelta(seconds=interval):
            return False
        try:
            start, end = parse_time_range(ec.time_range, 'employed.time_range')
        except ValueError as e:
            if not self._ec_bad_range_logged:
                log(f'{e}，被雇佣检查不调度')
                self._ec_bad_range_logged = True
            return False
        self._ec_bad_range_logged = False
        return in_time_range(datetime.now().time(), start, end)

    def employed_window_active(self) -> bool:
        """被雇佣时间段是否生效中（开关打开且当前在时间段内）：
        生效期间主任务（冒险/学习/打工/雇佣好友）不触发。"""
        ec = self.school.cfg.employed
        if not ec.enabled:
            return False
        try:
            start, end = parse_time_range(ec.time_range, 'employed.time_range')
        except ValueError:
            return False
        return in_time_range(datetime.now().time(), start, end)

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
        不必重启设备），仍失败才走 adb reboot 恢复（重进宠物页面）后试最后一次；
        每次中间异常都会截图存 runs/error_*.png 供排查（不发告警）。
        都失败时按任务类型分流：
        - fatal=True（学习/打工主任务）：发告警通知并退出调度器
          （设备/游戏状态异常，需要人工介入，不静默挂起空跑）；
        - fatal=False（冒险/踩踩/PK/好友护理/好友雇佣 支线任务）：抛 ScenarioFailed，
          由调度循环重新排期延后重试，先执行其他任务。
        场景 run() 正常返回 False（达当天上限等）不算失败。
        """
        try:
            return self._run_round(scen)
        except StatBlocked:
            # 开始任务时体力/清洁不足弹窗：handle_low_stat_dialog 已回主页面护理
            # 一次，立即重试当前任务一次（不截图/不重启/不算失败）；再被拦截则
            # 按常规失败分流（护理一次没解决，交给失败退避/告警）
            log(f'{name}: 体力/清洁不足已护理，重试当前任务')
            try:
                return self._run_round(scen)
            except StatBlocked:
                last = f'{name} 护理后重试仍被体力/清洁不足拦截'
                log(last)
                if fatal:
                    self._alert_and_exit(last)
                raise ScenarioFailed(last)
        except (PKDeferred, TaskDeferred):
            raise  # 场景主动要求临时推迟（如 PK 连续超时 / 雇佣好友发现活动进行中），不做重试/恢复
        except Exception as e:
            log(f'{name} 执行异常: {e}，回主页面重试一次')
            self._capture_failure_image('retry1')  # 截图记录现场，不发告警
        try:
            return self._run_round(scen)
        except Exception as e:
            log(f'{name} 回主页面重试仍失败: {e}，尝试重启设备恢复')
            self._capture_failure_image('retry2')  # 截图记录现场，不发告警
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
        dead = {'冒险': adventure_dead, '踩踩': self.visit_dead, 'PK': self.pk_dead,
                '好友护理': self.friend_care_dead, '雇佣好友': self.hire_friend_dead}
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

    def _capture_failure_image(self, stage: str) -> str | None:
        """分级重试的中间异常截图存到 runs/ 供排查（不发告警通知）。

        stage 用于文件名区分阶段（如 retry1=首次执行异常、retry2=回主页面重试仍失败）；
        失败只记日志，不阻塞重试/恢复流程。
        """
        try:
            path = PROJECT_ROOT / 'runs' / f'error_{stage}_{datetime.now():%Y%m%d_%H%M%S}.png'
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(self.school.dev.screenshot()).save(path)
            log(f'异常截图已保存: {path}')
            return str(path)
        except Exception as e:
            log(f'异常截图失败: {e}')
            return None

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
        """异常恢复：重启设备/模拟器（或重启游戏）-> 启动 QQ -> 回宠物页面，
        并刷新各场景的设备连接。返回是否成功。"""
        now = time.monotonic()
        if self.recoveries >= RECOVERY_LIMIT:
            if now - self.last_recovery_at < RECOVERY_RESET_AFTER:
                log(f'已连续恢复 {self.recoveries} 次仍异常，放弃恢复')
                return False
            log(f'距上次恢复已超 {RECOVERY_RESET_AFTER}s，重置恢复计数再试')
            self.recoveries = 0
        self.recoveries += 1
        self.last_recovery_at = now
        try:
            dev = reenter_pet(self.school.dev.adb, self.school.cfg.recover.method,
                              use_opener=self.use_opener,
                              opener_serial=self.opener_serial,
                              emulator_restart_cmd=self.school.cfg.recover.emulator_restart_cmd,
                              emulator_cfg=self.school.cfg.emulator)
        except Exception as e:
            log(f'恢复失败: {e}')
            return False
        for scen in (self.school, self.work, self.adventure, self.care, self.visit, self.pk,
                     self.friend_care, self.hire_friend, self.employed):
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
        self._last_cfg = cfg
        sched = cfg.schedule
        self.threshold = sched.coin_threshold
        self.school_factor = sched.school_factor
        self.work_factor = sched.work_factor
        self.daily_point_limit = sched.daily_point_limit
        # schedule 整体替换到各场景实例：check_interval / encourage_times /
        # main_page_checks / 金币阈值等全部热加载（设置页保存后下一轮即生效）
        for scen in (self.school, self.work, self.adventure, self.care, self.visit, self.pk,
                     self.friend_care, self.hire_friend, self.employed):
            scen.cfg.schedule = sched
            # 控制方案热加载（各场景共享同一个 dev，同步一次即全部生效；
            # minitouch 会话懒加载，切换方案后下次点击自动按新方案走）
            scen.dev.control_method = cfg.control.method
            # 被雇佣配置整体替换（开关/时间段/检查间隔/处理方式下一轮即生效）
            scen.cfg.employed = cfg.employed
            scen.cfg.recover.method = cfg.recover.method
            scen.cfg.recover.emulator_restart_cmd = cfg.recover.emulator_restart_cmd
            scen.cfg.emulator = cfg.emulator
        # 好友护理/好友雇佣配置整体替换（启用开关/时间段/好友名称/方式/次数下一轮即生效）
        self.friend_care.cfg.friend_care = cfg.friend_care
        self.hire_friend.cfg.hire_friend = cfg.hire_friend

        adv = cfg.adventure
        self.adventure_times = adv.times_per_day
        # 场景 run(max_times=None) 时用自己的 times_per_day，必须一起热更新
        # （否则 _run_round 跑 adventure 时日志/内部上限还是旧值）
        self.adventure.times_per_day = adv.times_per_day
        self.adventure.skip_bad_weather = adv.skip_bad_weather
        self.adventure.batch = adv.batch
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
        self.care.method = cfg.care.method
        # care.interval_seconds 热加载（care_due 读 self.care.cfg.care.interval_seconds）
        self.care.cfg.care = cfg.care
        self.school.times_per_day = cfg.school.times_per_day
        if cfg.school.attribute in ATTRIBUTE_COURSES:
            self.school.attribute = cfg.school.attribute
        else:
            log(f'属性点配置无效 {cfg.school.attribute!r}，沿用 {self.school.attribute}')
        self.work.location = cfg.work.location
        # work.duration 热加载：work.py 选工作选择框用 self.duration 副本，
        # hire_friend.py 用 cfg.work.duration，两个都要更新；非法值回退旧值
        if cfg.work.duration in DURATION_BOXES:
            self.work.duration = cfg.work.duration
        else:
            log(f'打工时长配置无效 {cfg.work.duration!r}，沿用 {self.work.duration}')
        self.hire_friend.cfg.work = cfg.work
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

    def _open_pet_page_or_exit(self) -> None:
        '''模拟器模式启动：QQ 搜索卡片空入口无法手动进宠物主页，先由 opener 打开。

        失败视为硬故障（模拟器上没宠物主页后续任务无从谈起），发告警后退出调度器。'''
        log('模拟器模式：正在用 qqpet-module-opener 打开 QQ 宠物主页...')
        try:
            serial = self.opener_serial or self.school.dev.adb.serial
            open_pet_page(serial=serial, adb_path=self.school.dev.adb.adb)
        except Exception as e:
            self._alert_and_exit(f'模拟器模式打开宠物主页失败: {e}')

    def run(self) -> None:
        if self.use_opener and not self.skip_opener:
            self._open_pet_page_or_exit()
        school_dead = False  # 学习今天不再可用（达上限/没有课程/执行失败）
        work_dead = False    # 打工今天不再可用
        adventure_dead = False  # 冒险今天不再可用（执行失败）
        while True:
            try:
                # 热修改：每轮调度前重读配置，设置页存盘最迟下一轮生效
                self.reload_config()

                # 所有任务开始之前：按护理间隔检查一次体力/清洁，不足则喂食/洗澡；
                # 失败直接抛给外层：走 adb reboot 恢复链路（src/recover.py）
                if self.care_due():
                    self.care.check_and_care()
                    self.care.last_care_at = datetime.now()

                # 被雇佣时间段内主任务（冒险/学习/打工/雇佣好友）不触发
                suppress_main = self.employed_window_active()

                # 被雇佣检查：到达检查间隔出门看是否被雇佣中，是则按配置召回
                if self.employed_due():
                    log('到达被雇佣检查时间，出门检查是否被雇佣中')
                    try:
                        self.run_one(self.employed, '被雇佣检查', fatal=False)
                    except ScenarioFailed:
                        self._reschedule('被雇佣')
                        # 中断时页面可能停在出门页，先退回主页面再继续调度
                        try:
                            self.school.ensure_main_page()
                        except Exception as e:
                            log(f'被雇佣检查失败后回主页面失败: {e}')
                        continue
                    self._employed_last_check = datetime.now()
                    continue

                # 冒险优先：到达调度时间且当天次数未满
                if not suppress_main and not adventure_dead and self.adventure_due():
                    log('到达冒险调度时间，优先处理冒险')
                    try:
                        done = self.run_one(self.adventure, '冒险', fatal=False)
                    except ScenarioFailed:
                        self._reschedule('冒险')  # 延后重试，回顶部先检查体力/清洁再判断其他任务
                        continue
                    else:
                        if done:
                            _, adv_done, _ = load_progress(ADVENTURE_PROGRESS_FILE, quiet=True)
                            if adv_done >= self.adventure_times:
                                log(f'今日冒险 {adv_done}/{self.adventure_times} 次已满，'
                                    f'明天 {self.adventure_start.strftime("%H:%M")} 后再冒险')
                            continue
                        adventure_dead = True
                        log('冒险当天不可继续')

                # 踩踩：到达调度时间且当天次数未满
                if not self.visit_dead and self.visit_due():
                    log('到达踩踩调度时间，处理踩踩')
                    try:
                        done = self.run_one(self.visit, '踩踩', fatal=False)
                    except ScenarioFailed:
                        self._reschedule('踩踩')
                        continue
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
                    except (PKDeferred, ScenarioFailed):
                        self._reschedule('PK')
                        # 推迟后先把页面退回主页面，再继续调度其他任务
                        # （PK 中断时页面停在好友/PK 页，主任务正常会自己回主页面，
                        #   但主任务当天已结束时会在 _wait_for_deferred 里长睡，先退回来更稳）
                        try:
                            self.school.ensure_main_page()
                        except Exception as e:
                            log(f'PK 推迟后回主页面失败: {e}')
                        # 回循环顶部重新检查体力/清洁（PK 会消耗体力/清洁），
                        # 再判断冒险/踩踩/主任务；PK 已延后，due() 不会立即放行
                        continue
                    else:
                        if done:
                            continue
                        self.pk_dead = True
                        log('PK 当天不可继续')

                # 好友雇佣：到达调度时间且当天次数未满（雇佣 CD 未到时场景抛
                # TaskDeferred 延后 60 秒复测，不原地等待）
                if not suppress_main and not self.hire_friend_dead and self.hire_friend_due():
                    log('到达雇佣好友调度时间，处理好友雇佣')
                    self.hire_friend.last_hire_at = datetime.now()  # 调度间隔从实际执行起算
                    try:
                        done = self.run_one(self.hire_friend, '雇佣好友', fatal=False)
                    except TaskDeferred as d:
                        # 宠物正在打工/学习/冒险/被雇佣中：按剩余时间延后，不算失败
                        self.retry_after['雇佣好友'] = d.until
                        log(f'雇佣好友: {d}，先执行其他任务')
                        try:
                            self.school.ensure_main_page()
                        except Exception as e:
                            log(f'好友雇佣延后后回主页面失败: {e}')
                        continue
                    except ScenarioFailed:
                        self._reschedule('雇佣好友')
                        # 中断时页面停在好友/打工页，先退回主页面再继续调度其他任务
                        try:
                            self.school.ensure_main_page()
                        except Exception as e:
                            log(f'好友雇佣失败后回主页面失败: {e}')
                        continue
                    else:
                        if done:
                            continue
                        self.hire_friend_dead = True
                        log('好友雇佣当天不可继续')

                # 好友护理：到达时间段且启用（场景内持续护理直到时间段结束）
                if not self.friend_care_dead and self.friend_care_due():
                    log('到达好友护理时间段，处理好友护理')
                    try:
                        done = self.run_one(self.friend_care, '好友护理', fatal=False)
                    except ScenarioFailed:
                        self._reschedule('好友护理')
                        # 中断时页面停在好友页，先退回主页面再继续调度其他任务
                        try:
                            self.school.ensure_main_page()
                        except Exception as e:
                            log(f'好友护理失败后回主页面失败: {e}')
                        continue
                    else:
                        if done:
                            continue
                        self.friend_care_dead = True
                        log('好友护理当天不可继续')

                if suppress_main:
                    # 被雇佣时间段内主任务（学习/打工）不触发：睡到下次被雇佣检查
                    # （醒来到点由循环顶部的被雇佣检查处理，支线任务下轮仍可调度）
                    ec = self.school.cfg.employed
                    time.sleep(max(1, int(getattr(ec, 'interval_seconds', 60) or 60)))
                    continue

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


# ---- 任务队列调度（engine: task_queue） ----

TASK_NAMES = {'care': '护理', 'adventure': '冒险', 'visit': '踩踩', 'pk': 'PK',
              'hire_friend': '雇佣好友', 'friend_care': '好友护理',
              'school': '学习', 'work': '打工'}
# 支线任务（异常重排期/主任务结束后可等待的任务）。
# 注：雇佣好友属于主任务组（互斥统一调度），但失败处理仍按支线语义
# （回主页面 + failure_interval 退避重试，不发告警不退出）
SIDE_TASK_KEYS = ('adventure', 'visit', 'pk', 'hire_friend', 'friend_care')
# 主任务组键定义在 src/config.py 的 MAIN_TASK_KEYS（GUI 设置校验也用）
# 没有任务可执行且没有明确等待点时的短轮询间隔（秒），顺带热加载配置
QUEUE_POLL_INTERVAL = 30

_COINS_UNSET = object()  # 本轮循环还没读过金币


class _QueueTask:
    """任务队列里的单个任务：配置 + 运行时调度状态。"""

    def __init__(self, key: str):
        self.key = key
        self.name = TASK_NAMES[key]
        self.cfg = TaskItemConfig()
        self.next_at = datetime.min  # 间隔/退避的最早可执行时间
        self.dead = False            # run() 返回 False（当天上限等），当天不再可用
        self.window: datetime | None = None  # daily trigger：当前服务的每日时间点


def _parse_clock(value, field: str):
    """解析 HH:MM 或 HH:MM:SS 时间（YAML 1.1 会把不带引号的 9:00 解析成分钟数 540）。"""
    if isinstance(value, int):
        value = f'{value // 60:02d}:{value % 60:02d}'
    for fmt in ('%H:%M:%S', '%H:%M'):
        try:
            return datetime.strptime(str(value), fmt).time()
        except ValueError:
            continue
    raise ValueError(f'{field} 时间格式无效: {value!r}，应为 HH:MM 或 HH:MM:SS')


def _parse_range(value, field: str) -> tuple:
    """解析 'HH:MM:SS-HH:MM:SS'（也接受 HH:MM）时间窗。"""
    try:
        start_s, end_s = str(value).split('-', 1)
        return _parse_clock(start_s.strip(), field), _parse_clock(end_s.strip(), field)
    except ValueError:
        raise ValueError(f'{field} 格式无效: {value!r}，应为 HH:MM:SS-HH:MM:SS') from None


def _in_clock_range(now, start, end) -> bool:
    """now 是否在 [start, end) 时间窗内（end <= start 视为跨零点）。"""
    if end > start:
        return start <= now < end
    return now >= start or now < end


class TaskQueueRunner(Runner):
    """任务队列调度：按 config.yaml 的 tasks.order 顺序扫描，执行第一个可执行的任务。

    与 legacy 主循环的差异：
    - 执行顺序完全由 tasks.order 配置（> 分隔，越靠前越优先），不在 order 里的任务不调度；
    - 每个任务有独立的调度设置（tasks.<任务名>）：enabled 开关、trigger
      （interval=按间隔秒数 / daily=按每日时间点列表，到点打开执行窗口，窗口内可反复执行
      直到任务返回 False；下一个时间点会重新打开窗口并清除当天不可继续标记）、
      enabled_time_range 执行时间窗、success_interval / failure_interval 成功/失败退避；
    - 冒险/学习/打工/雇佣好友互斥（共用"出门-进行中"一条线，不能同时做），作为**主任务组
      统一调度**：组内优先级由 tasks.main_order 配置（默认 学习>雇佣好友>冒险>打工），
      按顺序判定——冒险（到点且次数未满）/ 学习（点数未超限且金币 >= 阈值，
      或打工当天不可继续时回退）/ 雇佣好友（到点且次数未满）/ 打工（兜底，始终可执行），
      不受 tasks.order 里四者相对位置影响；四者当天都结束后调度器才退出
      （等支线退避，没有则退出）；
    - 主任务组**非阻塞等待**（延时收尾）：出发后识别到进行中状态（"正在学习"等）时
      OCR 剩余时间（"剩余00:02:50"）登记场景的 pending（参考 qq-farm-copilot 的
      倒计时调度），立即回主页面调度其他任务；到点由 _main_choice 调
      finish_pending() 收尾计数后再选下一个主任务；组内有 pending 且未到收尾时间时
      本轮不调度主任务（跳过，先去跑支线）；
    - 没有任务可执行时睡到最近的等待点（退避/每日窗口/pending 收尾时间，上限
      QUEUE_POLL_INTERVAL 轮询，顺带热加载配置）；主任务组当天结束后只等支线任务的
      失败退避，没有则退出调度器；
    - 被雇佣检查（employed.enabled 开启时）不在 tasks.order 里：被雇佣时间段内
      按 employed.interval_seconds 间隔优先于队列任务出门检查，被雇佣中则按
      employed.action 召回；被雇佣时间段内主任务组不触发（_main_choice 返回 None，
      pending 收尾不受影响）。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 主任务组开启延时收尾（非阻塞等待）：进行中 OCR 剩余时间登记 pending 后
        # 先回主页面调度其他任务，到点由 finish_pending() 收尾计数；
        # legacy 引擎不开启（保持原场景内阻塞等待）
        for scen in (self.adventure, self.school, self.hire_friend, self.work):
            scen.defer_wait = True
        self._main_order: list[str] = list(MAIN_TASK_KEYS)
        self._current_task: str | None = None  # 正在执行的任务名（队列状态展示用）

    def run(self) -> None:
        if self.use_opener and not self.skip_opener:
            self._open_pet_page_or_exit()
        tasks: dict[str, _QueueTask] = {}
        order: list[str] = []
        self._apply_tasks_config(tasks, order)
        log(f'任务队列调度已启用，执行顺序: {" > ".join(TASK_NAMES[k] for k in order)}')
        while True:
            try:
                # 热修改：每轮调度前重读配置（含 tasks.order 与各任务调度设置）
                self.reload_config()
                self._apply_tasks_config(tasks, order)
                executed = self._run_first_due(tasks, order)
                # 队列状态写 runs/queue_status.json，供 GUI 状态条显示
                # （在睡前写，睡眠期间 GUI 读到的是最新状态）
                self._write_queue_status(tasks, order)
                if not executed:
                    if not self._sleep_until_next(tasks, order):
                        return
            except Exception as e:
                # 兜底：循环体内未捕获的异常（u2 断开、设备卡死等）走重启恢复，
                # 恢复失败（连续 RECOVERY_LIMIT 次）发告警通知后退出
                log(f'调度循环异常: {e}')
                if not self.recover():
                    self._alert_and_exit(f'调度循环异常且恢复失败: {e}')

    # ---- 配置 ----

    def _apply_tasks_config(self, tasks: dict, order: list) -> None:
        """按最新配置刷新任务列表与执行顺序（支持热修改 tasks.order）。"""
        tcfg = self._last_cfg.tasks
        new_order = []
        for raw_key in tcfg.order.split('>'):
            key = raw_key.strip()
            if not key:
                continue
            if key not in TASK_KEYS:
                if key not in getattr(self, '_bad_task_keys', set()):
                    self._bad_task_keys = getattr(self, '_bad_task_keys', set()) | {key}
                    log(f'tasks.order 中未知任务名: {key!r}，已忽略（可选: {"/".join(TASK_KEYS)}）')
                continue
            new_order.append(key)
        if not new_order:
            log('tasks.order 为空或全部无效，使用默认顺序')
            new_order = list(TASK_KEYS)
        for key in new_order:
            task = tasks.get(key)
            if task is None:
                task = tasks[key] = _QueueTask(key)
            task.cfg = getattr(tcfg, key)
        order[:] = new_order
        # 主任务组内优先级（tasks.main_order）：没列出的主任务按默认顺序兜底排最后
        main_order = []
        for raw_key in tcfg.main_order.split('>'):
            key = raw_key.strip()
            if not key:
                continue
            if key not in MAIN_TASK_KEYS:
                if key not in getattr(self, '_bad_main_keys', set()):
                    self._bad_main_keys = getattr(self, '_bad_main_keys', set()) | {key}
                    log(f'tasks.main_order 中未知主任务名: {key!r}，已忽略'
                        f'（可选: {"/".join(MAIN_TASK_KEYS)}）')
                continue
            if key not in main_order:
                main_order.append(key)
        for key in MAIN_TASK_KEYS:
            if key not in main_order:
                main_order.append(key)
        self._main_order = main_order

    # ---- 可执行判定 ----

    def _latest_daily_time(self, daily_times: list, now: datetime) -> datetime | None:
        """daily trigger：今天已到点的最近一个每日时间点（没有返回 None）。"""
        latest = None
        for value in daily_times or []:
            try:
                dt = datetime.combine(now.date(), _parse_clock(value, 'daily_times'))
            except ValueError as e:
                log(f'{e}，忽略该时间点')
                continue
            if dt <= now and (latest is None or dt > latest):
                latest = dt
        return latest

    def _next_daily_time(self, daily_times: list, now: datetime) -> datetime | None:
        """daily trigger：下一个每日时间点（今天没到点的，否则明天最早一个）。"""
        future = []
        for value in daily_times or []:
            try:
                dt = datetime.combine(now.date(), _parse_clock(value, 'daily_times'))
            except ValueError:
                continue
            future.append(dt if dt > now else dt + timedelta(days=1))
        return min(future) if future else None

    def _eligible(self, task: _QueueTask, now: datetime) -> bool:
        """任务当前是否可进入执行判定：开关、执行时间窗、trigger 窗口、退避间隔。"""
        cfg = task.cfg
        if not cfg.enabled:
            return False
        try:
            start, end = _parse_range(cfg.enabled_time_range, 'enabled_time_range')
        except ValueError as e:
            log(f'{e}，{task.name} 不调度')
            return False
        if not _in_clock_range(now.time(), start, end):
            return False
        if cfg.trigger == 'daily':
            latest = self._latest_daily_time(cfg.daily_times, now)
            if latest is None:
                return False
            if task.window is None or task.window < latest:
                # 新每日时间点：打开执行窗口，清除当天不可继续标记
                task.window = latest
                task.dead = False
                task.next_at = datetime.min
                log(f'{task.name}: 到达每日时间点 {latest:%H:%M}，打开执行窗口')
        if task.dead:
            return False
        return now >= task.next_at

    @staticmethod
    def _dead(tasks: dict, key: str) -> bool:
        """任务当天是否不可继续；不在 tasks.order 里的任务视为不可用。"""
        task = tasks.get(key)
        return task is None or task.dead

    def _main_pending_scen(self):
        """主任务组里当前有 pending（进行中活动延时收尾）的场景，没有返回 None。"""
        for scen in (self.adventure, self.school, self.hire_friend, self.work):
            if scen.pending is not None:
                return scen
        return None

    def _school_due(self, tasks: dict, ctx: dict) -> bool:
        """学习是否可执行（主任务组内）：点数未超限，且金币 >= 阈值
        （金币识别失败/不足时本来该打工；打工当天不可继续才回退学习）。

        点数/金币每轮循环只读一次（ctx 缓存）：金币读取要截图 OCR，不能每个任务读一次。
        """
        if ctx.get('points') is None:
            learned, worked, points = self.today_points()
            ctx['points'] = points
            log(f'今日点数: 学习{learned}次x{self.school_factor}+打工{worked}次x{self.work_factor}'
                f' = {points} / {self.daily_point_limit}')
        over_limit = ctx['points'] > self.daily_point_limit
        if over_limit:
            if not ctx.get('over_logged'):
                ctx['over_logged'] = True
                log('点数超限，今天不再学习，只打工')
            return False
        if ctx.get('coins', _COINS_UNSET) is _COINS_UNSET:
            try:
                ctx['coins'] = self.read_main_coins()
            except RuntimeError as e:
                log(f'金币读取失败: {e}，默认先去打工')
                ctx['coins'] = None
            coins = ctx['coins']
            if coins is None:
                log('金币识别失败，默认先去打工')
            elif coins >= self.threshold:
                log(f'金币 {coins} >= 阈值 {self.threshold}，去学习')
            else:
                log(f'金币 {coins} < 阈值 {self.threshold}，先去打工')
        if ctx['coins'] is not None and ctx['coins'] >= self.threshold:
            return True
        return self._dead(tasks, 'work')  # 打工不可继续且点数未超限，回退学习

    def _main_choice(self, tasks: dict, ctx: dict) -> str | None:
        """主任务组（冒险/学习/打工/雇佣好友，互斥不能同时做）本轮该执行哪个。

        组内有 pending（进行中活动延时收尾）时：未到收尾时间返回 None（本轮不调度
        主任务，先去跑支线）；到点调 finish_pending() 收尾计数，还没结束（已重估
        收尾时间）也返回 None，收尾完成本轮继续选择下一个主任务。
        否则按 tasks.main_order 顺序判定：冒险（到点且当天次数未满）/ 学习
        （点数未超限且金币 >= 阈值，或打工不可继续时回退）/ 雇佣好友（到点且
        次数未满）/ 打工（兜底，当天可继续就可执行）。都不可以返回 None。
        """
        pend = self._main_pending_scen()
        if pend is not None:
            if datetime.now() < pend.pending['until']:
                return None  # 还没到收尾时间，本轮不调度主任务
            if not pend.finish_pending():
                return None  # 尚未结束（已重估收尾时间），本轮继续等
            # 收尾完成（已计数），本轮继续选择下一个主任务
        if self.employed_window_active():
            return None  # 被雇佣时间段内主任务不触发
        now = datetime.now()
        for key in self._main_order:
            task = tasks.get(key)
            # 跳过不可执行的任务（不在 order / 当天不可继续 / 退避未到点）：
            # 否则幻影命中（如雇佣好友 CD 复测退避 60 秒，但 hire_friend_due 的
            # 调度间隔只有几秒）会把排它后面的主任务（冒险/打工）全部卡住
            if task is None or not self._eligible(task, now):
                continue
            if key == 'adventure':
                if self.adventure_due():
                    return 'adventure'
            elif key == 'school':
                if self._school_due(tasks, ctx):
                    return 'school'
            elif key == 'hire_friend':
                if self.hire_friend_due():
                    return 'hire_friend'
            elif key == 'work':
                return 'work'  # 兜底：打工当天可继续就可执行
        return None

    def _task_due(self, key: str, tasks: dict, ctx: dict) -> bool:
        """任务自身的执行条件（配额/场景时间窗/主任务组统一判定），在 _eligible 之后判定。"""
        if key in MAIN_TASK_KEYS:
            # 主任务组互斥：组内统一决定本轮执行谁，order 里谁先扫到不影响结果
            return self._main_choice(tasks, ctx) == key
        if key == 'care':
            return self.care_due()
        if key == 'visit':
            return self.visit_due()
        if key == 'pk':
            return self.pk_due()
        if key == 'friend_care':
            return self.friend_care_due()
        return False

    # ---- 执行 ----

    def _run_first_due(self, tasks: dict, order: list) -> bool:
        """按 order 扫描，执行第一个可执行的任务，返回是否执行了任务。
        被雇佣检查不在 tasks.order 里：到点优先于队列任务先检查。"""
        now = datetime.now()
        if self.employed_due():
            self._run_employed_check(tasks, order, now)
            return True
        ctx: dict = {}  # 本轮循环的 点数/金币 缓存（_main_due 用）
        for key in order:
            task = tasks[key]
            if not self._eligible(task, now):
                continue
            if not self._task_due(key, tasks, ctx):
                continue
            self._execute(task, tasks, now, order)
            return True
        return False

    def _run_employed_check(self, tasks: dict, order: list, now: datetime) -> None:
        """被雇佣检查一轮：出门看是否被雇佣中，是则按 employed.action 召回处理。
        成功后按 employed.interval_seconds 间隔再次检查；失败延后重试。"""
        self._current_task = '被雇佣检查'
        self._write_queue_status(tasks, order)  # 检查期间 GUI 状态条显示"当前任务"
        try:
            try:
                self.run_one(self.employed, '被雇佣检查', fatal=False)
            except ScenarioFailed:
                at = now + timedelta(seconds=SIDE_TASK_RETRY_DELAY)
                self.retry_after['被雇佣'] = at
                log(f'被雇佣检查多次重试仍失败，延后到 {at:%H:%M} 重试，先执行其他任务')
                # 中断时页面可能停在出门页，先退回主页面再调度其他任务
                self._back_to_main('被雇佣检查失败后')
                return
            self._employed_last_check = datetime.now()
        finally:
            self._current_task = None

    def _execute(self, task: _QueueTask, tasks: dict, now: datetime,
                 order: list) -> None:
        """执行一个任务一轮，按结果更新退避/不可继续状态。"""
        self._current_task = task.name
        self._write_queue_status(tasks, order)  # 执行期间 GUI 状态条显示"当前任务"
        try:
            self._execute_body(task, tasks, now)
        finally:
            self._current_task = None

    def _execute_body(self, task: _QueueTask, tasks: dict, now: datetime) -> None:
        """任务执行主体（_execute 包了当前任务状态展示）。"""
        cfg = task.cfg
        if task.key == 'care':
            # 护理检查异常直接抛给外层走重启恢复（同 legacy）
            self.care.check_and_care()
            self.care.last_care_at = datetime.now()
            task.next_at = self._success_at(cfg, now)
            return
        scen = {'adventure': self.adventure, 'visit': self.visit, 'pk': self.pk,
                'hire_friend': self.hire_friend, 'friend_care': self.friend_care,
                'school': self.school, 'work': self.work}[task.key]
        if task.key == 'hire_friend':
            # 调度间隔从实际执行起算（不在 hire_friend_due 判定里记：_main_choice
            # 每轮会被同组任务的扫描重复评估，查询副作用会把雇佣好友卡死）
            self.hire_friend.last_hire_at = datetime.now()
        try:
            produced = self.run_one(scen, task.name, fatal=task.key in ('school', 'work'))
        except PKDeferred:
            self._fail_task(task, now, 'PK 结果连续超时，临时推迟')
            self._back_to_main('PK 推迟后')
            return
        except TaskDeferred as d:
            # 场景主动延后（如雇佣好友发现宠物正在打工/学习/冒险/被雇佣中）：
            # 到 until 再调度，不算失败
            task.next_at = d.until
            log(f'{task.name}: {d}，先执行其他任务')
            self._back_to_main(f'{task.name} 延后后')
            return
        except ScenarioFailed:
            self._fail_task(task, now, f'{task.name} 多次重试仍失败')
            if task.key in SIDE_TASK_KEYS:
                # 中断时页面可能停在好友页，先退回主页面再调度其他任务
                self._back_to_main(f'{task.name} 失败后')
            return
        if produced:
            if getattr(scen, 'pending', None) is not None:
                # 延时收尾（pending）的任务节奏由 pending.until 控制：next_at 立即到期，
                # 结算完成的同一轮调度就能接力下一个主任务；若按 success_interval
                # 从出发起算，活动短于 success_interval 时结算后会凭空多等
                task.next_at = now
            else:
                task.next_at = self._success_at(cfg, now)
        else:
            task.dead = True
            log(f'{task.name} 当天不可继续')

    @staticmethod
    def _success_at(cfg: TaskItemConfig, now: datetime) -> datetime:
        """成功后的下次可执行时间：success_interval，interval 触发再叠加最小执行间隔。"""
        secs = cfg.success_interval
        if cfg.trigger == 'interval':
            secs = max(secs, cfg.interval_seconds)
        return now + timedelta(seconds=secs)

    def _fail_task(self, task: _QueueTask, now: datetime, reason: str) -> None:
        """任务失败退避：延后 failure_interval 秒重试，先执行其他任务。"""
        at = now + timedelta(seconds=task.cfg.failure_interval)
        task.next_at = at
        log(f'{reason}，延后到 {at:%H:%M} 重试，先执行其他任务')

    def _back_to_main(self, stage: str) -> None:
        """支线任务中断后退回主页面（页面可能停在好友页），失败只记日志。"""
        try:
            self.school.ensure_main_page()
        except Exception as e:
            log(f'{stage}回主页面失败: {e}')

    # ---- 队列状态（GUI 状态条） ----

    def _write_queue_status(self, tasks: dict, order: list) -> None:
        """把队列状态写 runs/queue_status.json，供 GUI 日志页状态条显示。

        待执行 = 启用且未结束、在等退避/每日窗口的任务数（到了时间点待执行），
        主任务组 pending（进行中活动延时收尾）也算一个待执行项；
        等待中 = 启用且未结束、当前通过 _eligible（时间窗/trigger 窗口/退避）、
        等待调度器轮到它的任务数；
        下一任务 = 最近等待点（退避时间/下一个每日时间点/pending 收尾时间）对应的任务。
        tasks 字段按任务记录状态（GUI"调度"选项卡用）：disabled/dead/ready/waiting
        + 下次执行时间（dead 的 daily 任务取下一个每日时间点）。
        """
        now = datetime.now()
        ready = waiting = 0
        next_at = None
        next_name = ''
        task_states = {}
        for key in order:
            task = tasks[key]
            cfg = task.cfg
            if not cfg.enabled:
                task_states[key] = {'state': 'disabled', 'next': ''}
                continue
            if task.dead:
                # daily 任务在下一个每日时间点复活，也算下次执行时间
                nxt = (self._next_daily_time(cfg.daily_times, now)
                       if cfg.trigger == 'daily' else None)
                task_states[key] = {'state': 'dead',
                                    'next': nxt.strftime('%Y-%m-%d %H:%M:%S') if nxt else ''}
                continue
            if self._eligible(task, now):
                # 现在就能跑、在等调度器轮到 -> 等待中
                waiting += 1
                task_states[key] = {'state': 'ready', 'next': ''}
                continue
            # 在等退避/每日时间点 -> 待执行
            ready += 1
            # 最近的等待点：退避时间 / 下一个每日时间点
            cand = task.next_at if task.next_at > now else None
            if cfg.trigger == 'daily':
                nxt = self._next_daily_time(cfg.daily_times, now)
                if nxt and (cand is None or nxt < cand):
                    cand = nxt
            task_states[key] = {'state': 'waiting',
                                'next': cand.strftime('%Y-%m-%d %H:%M:%S') if cand else ''}
            if cand and (next_at is None or cand < next_at):
                next_at, next_name = cand, task.name
        pend = self._main_pending_scen()
        pending_desc = ''
        if pend is not None:
            # pending 在等收尾时间，也算待执行
            ready += 1
            pending_desc = pend.pending['desc']
            until = pend.pending['until']
            if next_at is None or until < next_at:
                next_at, next_name = until, f'{pending_desc}收尾'
        save_queue_status({
            'current': self._current_task or '',
            'pending': pending_desc,
            'next': next_name,
            'next_at': next_at.strftime('%H:%M:%S') if next_at else '',
            # 时间戳：GUI 每 5 秒刷新时算"剩余xx秒"倒计时用
            'next_ts': next_at.timestamp() if next_at else 0,
            'ready': ready,
            'waiting': waiting,
            'tasks': task_states,
            'updated': now.strftime('%H:%M:%S'),
        })

    # ---- 等待 ----

    def _adventure_done(self, tasks: dict) -> bool:
        """冒险今天是否已结束：不可继续 / 未配置次数 / 当天次数已满。
        只看配额不看 start_time——主任务没结束就不退出调度器，等冒险到点。"""
        if self._dead(tasks, 'adventure') or not self.adventure_times:
            return True
        _, done, _ = load_progress(ADVENTURE_PROGRESS_FILE, quiet=True)
        return done >= self.adventure_times

    def _hire_friend_done(self, tasks: dict) -> bool:
        """雇佣好友今天是否已结束：不可继续 / 未启用或未配置 / 当天次数已满。
        只看配额不看 start_time——主任务没结束就不退出调度器，等雇佣到点。"""
        if self._dead(tasks, 'hire_friend'):
            return True
        hf = self.hire_friend.cfg.hire_friend
        if not hf.enabled or not hf.times_per_day or not hf.friend_name.strip():
            return True
        _, done, _ = load_progress(HIRE_FRIEND_PROGRESS_FILE, quiet=True)
        return done >= hf.times_per_day

    def _main_finished(self, tasks: dict) -> bool:
        """主任务组（冒险/学习/打工/雇佣好友）今天是否已全部结束：
        学习不可继续或点数超限，打工不可继续，冒险/雇佣好友当天结束，
        且组内没有进行中的 pending（pending 未收尾不能退出，否则丢失计数）。"""
        if self._main_pending_scen() is not None:
            return False
        _, _, points = self.today_points()
        school_done = self._dead(tasks, 'school') or points > self.daily_point_limit
        return (school_done and self._dead(tasks, 'work')
                and self._adventure_done(tasks) and self._hire_friend_done(tasks))

    def _sleep_until_next(self, tasks: dict, order: list) -> bool:
        """没有任务可执行时的等待：睡到最近的等待点（退避/每日窗口/pending 收尾时间），
        上限 QUEUE_POLL_INTERVAL 短轮询（顺带热加载配置）。
        主任务当天结束后只等支线任务的失败退避，没有则返回 False（正常结束）。"""
        now = datetime.now()
        if self._main_finished(tasks):
            future = [task.next_at for key in order if key in SIDE_TASK_KEYS
                      for task in (tasks[key],)
                      if task.cfg.enabled and not task.dead and task.next_at > now]
            if not future:
                log('冒险/学习/打工/雇佣好友都已达当天上限，结束')
                return False
            at = min(future)
            log(f'主任务组当天已结束，还有支线任务等待到 {at:%H:%M}，调度器等待')
            time.sleep(max(1.0, (at - now).total_seconds()))
            return True
        future = []
        for key in order:
            task = tasks[key]
            cfg = task.cfg
            if not cfg.enabled:
                continue
            if not task.dead and task.next_at > now:
                future.append(task.next_at)
            if cfg.trigger == 'daily':
                # dead 的 daily 任务在下一个每日时间点复活，也算等待点
                nxt = self._next_daily_time(cfg.daily_times, now)
                if nxt:
                    future.append(nxt)
        # 主任务组 pending（进行中活动）的收尾时间也是等待点
        for scen in (self.adventure, self.school, self.hire_friend, self.work):
            if scen.pending is not None and scen.pending['until'] > now:
                future.append(scen.pending['until'])
        if future:
            delta = (min(future) - now).total_seconds()
            time.sleep(min(max(1.0, delta), QUEUE_POLL_INTERVAL))
        else:
            time.sleep(QUEUE_POLL_INTERVAL)
        return True


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
    if name == 'opener':
        # 模拟器：直接用 qqpet-module-opener 打开宠物主页（绕过空搜索入口）
        from src.opener import open_pet_page as _open
        cfg = load_config()
        _open(serial=cfg.adb.device_serial or None, adb_path=find_adb(cfg.adb.path))
        log('opener 测试完成：宠物主页已打开')
        return

    scen_name, _, method = name.partition('.')
    scenarios = {'school': SchoolScenario, 'work': WorkScenario,
                 'adventure': AdventureScenario, 'care': CareScenario,
                 'visit': VisitScenario, 'pk': PKScenario,
                 'friend_care': FriendCareScenario, 'hire_friend': FriendHireScenario}
    if scen_name not in scenarios or not method:
        raise ValueError(f'--test 参数无效: {name!r}，应为 coins / recover 或 school./work./adventure./care./friend_care. 开头的方法名')
    scen = scenarios[scen_name]()
    fn = getattr(scen, method, None)
    if not callable(fn):
        raise ValueError(f'{scen_name} 场景没有方法: {method!r}')
    log(f'单测 {name} ...')
    result = fn()
    log(f'{name} 返回: {result}')


def run_scheduler(use_opener: bool, opener_serial: str | None = None,
                  skip_opener: bool = False) -> None:
    """按 config.yaml 的 runner.engine 选择调度引擎运行（控制台与 GUI 打包后的
    --runner 子进程共用，保证两边引擎一致）。"""
    engine = str(getattr(load_config().runner, 'engine', 'task_queue')).strip()
    if engine not in ('task_queue', 'legacy'):
        log(f'runner.engine 配置无效: {engine!r}，使用默认 task_queue')
        engine = 'task_queue'
    log(f'调度引擎: {engine}')
    runner_cls = TaskQueueRunner if engine == 'task_queue' else Runner
    runner_cls(use_opener=use_opener, opener_serial=opener_serial,
               skip_opener=skip_opener).run()


if __name__ == '__main__':
    import argparse

    ap = argparse.ArgumentParser(description='统一执行器：按金币调度学习/打工')
    ap.add_argument('--test', metavar='TARGET',
                    help='单模块测试: coins / recover / opener 或 school.<方法名> / work.<方法名>')
    ap.add_argument('--emulator', action='store_true',
                    help='模拟器模式：用 qqpet-module-opener 打开宠物主页（绕过空搜索入口）')
    ap.add_argument('--no-emulator', action='store_true',
                    help='强制关闭模拟器模式（打包的模拟器版默认开启时用）')
    ap.add_argument('--emulator-device', metavar='SERIAL',
                    help='模拟器 ADB 地址（如 127.0.0.1:7555），默认用 config 的 adb.device_serial')
    ap.add_argument('--skip-opener', action='store_true',
                    help='宠物主页已打开（如 GUI 手动重启刚恢复完），启动时跳过 opener 打开')
    args = ap.parse_args()

    if args.test:
        run_test(args.test)
    else:
        if args.emulator:
            use_opener = True
        elif args.no_emulator:
            use_opener = False
        else:
            use_opener = is_emulator_build()  # 打包的模拟器版默认开启
        try:
            run_scheduler(use_opener, args.emulator_device, skip_opener=args.skip_opener)
        except KeyboardInterrupt:
            log('手动停止')
