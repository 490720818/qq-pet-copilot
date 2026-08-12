"""启动入口（PyQt6 GUI）：左侧嵌入 scrcpy 窗口，右侧实时显示调度器日志。

- 启动前清理本设备遗留的 scrcpy（只清本实例/本设备，多开互不影响），再重新拉起并以 --window-borderless 嵌入
- scrcpy 以 --turn-screen-off 运行（手机屏幕关闭，镜像照常）
- 右侧顶部"开始/停止"按钮：开始 = 子进程启动调度器，停止 = 立即结束调度器进程
- 右侧选项卡：日志（顶部当日统计条）/ 统计（各任务近 N 天平滑折线图）/ 设置
- 调度器子进程的 stdout 实时显示在右侧日志区
- scrcpy 看门狗：进程断开（设备 adb reboot/掉线）后自动重拉并重嵌入
- 关闭窗口时结束由本程序拉起的 scrcpy 和调度器进程

运行：python main.py
控制台模式（无 GUI）：python scenarios/runner.py
"""

import os
import queue
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import win32con
import win32gui
import win32process

from src import settings as settings_io
from src.adb.device import Device
from src.config import APP_ROOT, PROJECT_ROOT, find_adb, is_emulator_build, load_config, resource_path
from src.progress import (
    ADVENTURE_PROGRESS_FILE,
    PK_PROGRESS_FILE,
    SCHOOL_PROGRESS_FILE,
    VISIT_PROGRESS_FILE,
    WORK_PROGRESS_FILE,
    add_log_listener,
    known_accounts,
    load_exp_daily,
    load_progress,
    log,
)
from src.stats_chart import StatsPanel
from src.status_cache import FIELDS as STATUS_FIELDS
from src.status_cache import load_accounts

SCRCPY = resource_path('resources/scrcpy-win64') / 'scrcpy.exe'
SCRCPY_TITLE_PREFIX = 'QQPetCopilotScrcpy'
RUNNER_SCRIPT = PROJECT_ROOT / 'scenarios' / 'runner.py'
EMBED_TRIES = 40  # 查找 scrcpy 窗口的次数（每次 500ms）
SCRCPY_WATCHDOG_MS = 5000    # scrcpy 看门狗轮询间隔（毫秒）
SCRCPY_RETRY_INTERVAL = 15.0  # 重拉失败后的退避（秒；设备重启要几十秒，别刷日志）

# Windows 下隐藏子进程的命令行窗口（scrcpy/taskkill 等都是控制台程序）
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0

# 按钮样式
_BTN_BASE = """
    QPushButton {{
        color: white; border: none; border-radius: 5px;
        padding: 3px 16px; font-size: 14px; font-weight: bold;
    }}
    QPushButton:hover {{ background-color: {hover}; }}
    QPushButton:pressed {{ background-color: {pressed}; }}
    QPushButton:disabled {{ background-color: {disabled}; color: #f5f5f5; }}
"""
START_BTN_STYLE = 'QPushButton { background-color: #4CAF50; }' + _BTN_BASE.format(
    hover='#45a049', pressed='#3d8b40', disabled='#c8e6c9')
STOP_BTN_STYLE = 'QPushButton { background-color: #f44336; }' + _BTN_BASE.format(
    hover='#e53935', pressed='#d32f2f', disabled='#ffcdd2')
# scrcpy 开关：未勾选=关闭（灰），勾选=开启（蓝）
SCRCPY_BTN_STYLE = """
QPushButton {{
    color: white; border: none; border-radius: 5px;
    padding: 3px 16px; font-size: 14px; font-weight: bold;
    background-color: #9e9e9e;
}}
QPushButton:hover {{ background-color: #bdbdbd; }}
QPushButton:checked {{ background-color: #2196F3; }}
QPushButton:checked:hover {{ background-color: #1e88e5; }}
QPushButton:disabled {{ background-color: #e0e0e0; color: #f5f5f5; }}
"""

# OnePush 各提供方参数配置教程（ALAS wiki 中文文档）
ONEPUSH_HELP_URL = ('https://github.com/LmeSzinc/AzurLaneAutoScript'
                    '/wiki/Onepush-configuration-%5BCN%5D')

class _TestSignals(QObject):
    """连接测试按钮：后台线程 -> GUI 主线程 的信号（跨线程安全）。"""

    finished = pyqtSignal(bool)  # True=测试结束，恢复按钮可用


class _FocusOutPlainTextEdit(QPlainTextEdit):
    """失焦时触发保存回调的多行文本框（QPlainTextEdit 没有 editingFinished）。"""

    def __init__(self, on_focus_out):
        super().__init__()
        self._on_focus_out = on_focus_out

    def focusOutEvent(self, event):
        self._on_focus_out()
        super().focusOutEvent(event)


class _NoWheelSpinBox(QSpinBox):
    """数字输入框：禁用鼠标滚轮改值（滚轮悬停数字框容易误触加减，防手滑）。

    仅屏蔽滚轮事件，键盘上下键、直接输入等行为不受影响；
    保存由设置页的 valueChanged 触发（数值真的变化才保存，滚轮不再误触发）。
    """

    def wheelEvent(self, event):
        event.ignore()


# 设置页面字段：(点路径, 显示名, 类型)
# 类型: 'int' / 'str' / 'bool' / 'text'(多行文本) / 'devices'(adb 设备下拉) / 选项列表
SETTING_FIELDS = [
    ('adb.path', 'adb 路径', 'str'),
    ('adb.device_serial', '设备序列号', 'devices'),
    ('school.attribute', '属性点课程', ['力量', '智力', '魅力']),
    ('school.times_per_day', '每天学习次数（0 不限）', 'int'),
    ('work.location', '打工地点', 'str'),
    ('work.times_per_day', '每天打工次数（0 不限）', 'int'),
    ('work.employ_scroll_limit', '雇佣拖动上限', 'int'),
    ('schedule.coin_threshold', '金币阈值', 'int'),
    ('schedule.school_factor', '学习点数系数', 'int'),
    ('schedule.work_factor', '打工点数系数', 'int'),
    ('schedule.daily_point_limit', '每日点数上限', 'int'),
    ('schedule.check_interval', '状态检查间隔（秒）', 'int'),
    ('adventure.times_per_day', '每天冒险次数（0 不冒险）', 'int'),
    ('adventure.start_time', '冒险调度时间（HH:MM）', 'str'),
    ('adventure.skip_bad_weather', '冒险跳过"天色不对"', 'bool'),
    ('adventure.batch', '单轮冒险次数', 'int'),
    ('visit.times_per_day', '每天踩踩次数（0 不踩）', 'int'),
    ('visit.start_time', '踩踩调度时间（HH:MM）', 'str'),
    ('pk.times_per_day', '每天 PK 次数（0 不 PK）', 'int'),
    ('pk.start_time', 'PK 调度时间（HH:MM）', 'str'),
    ('care.energy_threshold', '体力阈值', 'int'),
    ('care.clean_threshold', '清洁阈值', 'int'),
    ('care.method', '护理方式', ['一键护理', 'ocr检测']),
    ('employed.action', '被雇佣后处理', ['等到25/75（小于45min）', '等到25/75', '立刻召回']),
    ('recover.method', '异常处理方式', ['重启设备', '重启游戏']),
    ('notify.win_toast', '失败告警 Windows 通知', 'bool'),
    ('notify.onepush_config', '失败告警 OnePush 配置', 'text'),
]


def _scrcpy_title() -> str:
    """本实例唯一的 scrcpy 窗口标题：设备序列号 + 本进程 PID。

    多开时各实例标题互不相同，嵌入查找只匹配自己的窗口，
    不会把别的实例的画面抓到本窗口里（同实例内看门狗重拉标题不变）。
    """
    serial = load_config().adb.device_serial or 'auto'
    return f'{SCRCPY_TITLE_PREFIX}-{serial}-{os.getpid()}'


def _kill_scrcpy_by_marker(marker: str) -> None:
    """结束命令行里包含 marker 的 scrcpy.exe 进程（不影响其他实例/程序）。

    taskkill /IM 会杀掉所有实例的 scrcpy（多开相互影响），这里用 PowerShell CIM
    按命令行精确过滤；marker 经环境变量传入，避免引号/通配符转义问题。
    """
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='scrcpy.exe'\" "
        "| Where-Object { $_.CommandLine -and $_.CommandLine.Contains($env:QQPET_SCRCPY_MARKER) } "
        "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    env = dict(os.environ, QQPET_SCRCPY_MARKER=marker)
    subprocess.run(
        ['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
        capture_output=True, timeout=20, creationflags=_NO_WINDOW, env=env,
    )


def kill_our_scrcpy(proc: subprocess.Popen | None = None) -> None:
    """结束本实例的 scrcpy：跟踪的进程 + 命令行带本实例唯一标记的残留进程。

    多开时只清自己的，不碰别的实例。
    """
    if proc is not None and proc.poll() is None:
        proc.terminate()
    try:
        _kill_scrcpy_by_marker(_scrcpy_title())
    except Exception:
        log('结束本实例 scrcpy 残留进程失败（可忽略）')


def kill_previous_scrcpy() -> None:
    """启动时清理本设备上次崩溃遗留的 scrcpy（还占着镜像/关屏）。

    只按"设备序列号"前缀匹配，不碰其他设备上的实例；未指定序列号时
    无法安全区分，跳过（多开安全优先）。
    """
    serial = load_config().adb.device_serial
    if not serial:
        return
    try:
        _kill_scrcpy_by_marker(f'{SCRCPY_TITLE_PREFIX}-{serial}-')
    except Exception:
        log('清理本设备遗留 scrcpy 失败（可忽略）')


def start_scrcpy() -> subprocess.Popen | None:
    """以无边框、关屏、固定标题启动 scrcpy，返回进程。"""
    if not SCRCPY.is_file():
        log(f'未找到 {SCRCPY}，跳过 scrcpy 启动')
        return None
    cmd = [str(SCRCPY)]
    serial = load_config().adb.device_serial
    if serial:  # 指定设备序列号
        cmd += ['-s', serial]
    cmd += ['--no-audio',  # 只要画面，不要音频（镜像/自动化用不到声音）
            '--turn-screen-off', '--window-borderless', '--stay-awake',
            f'--window-title={_scrcpy_title()}',
            # 先放到屏幕外，嵌入容器时再移回来，避免窗口先弹出再嵌入的闪烁
            '--window-x=-2000', '--window-y=-2000']
    log(f'启动 scrcpy（--no-audio --turn-screen-off --window-borderless'
        + (f'，设备 {serial}）...' if serial else '）...'))
    proc = subprocess.Popen(
        cmd,
        cwd=str(SCRCPY.parent),  # scrcpy 需要同目录的 scrcpy-server 等文件
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )
    time.sleep(1)
    if proc.poll() is not None:
        log('警告: scrcpy 启动后立刻退出了，请检查设备连接')
        return None
    return proc


def start_scrcpy_screen_off() -> subprocess.Popen | None:
    """无头 scrcpy 关闭设备屏幕：--turn-screen-off + 保持唤醒，不传画面/音频/不开窗口。

    画面镜像关闭后用它把设备屏幕真正关掉（比亮度 0 更彻底）；
    --stay-awake 让设备保持唤醒（渲染管线不断，OCR/自动化照常），
    --no-window 不显示任何窗口。返回进程；失败返回 None（屏幕保持原状）。
    """
    if not SCRCPY.is_file():
        log(f'未找到 {SCRCPY}，跳过屏幕关闭')
        return None
    cmd = [str(SCRCPY), '--turn-screen-off', '--no-video', '--no-audio',
           '--stay-awake', '--no-window']
    serial = load_config().adb.device_serial
    if serial:
        cmd += ['-s', serial]
    log('启动 scrcpy 关闭屏幕（--turn-screen-off --no-video --no-audio '
        '--stay-awake --no-window）...')
    proc = subprocess.Popen(
        cmd,
        cwd=str(SCRCPY.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )
    time.sleep(1)
    if proc.poll() is not None:
        log('警告: 屏幕关闭 scrcpy 启动后立刻退出了')
        return None
    return proc


def find_scrcpy_hwnd(proc: subprocess.Popen | None = None) -> int | None:
    """按本实例唯一标题查找 scrcpy 窗口句柄。

    proc 传本实例跟踪的 scrcpy 进程时，再按进程 PID 过滤：
    重启瞬间旧窗口可能还没销毁（同标题），或别的实例窗口标题撞上，
    只有属于自己进程的窗口才会被嵌入。
    """
    title = _scrcpy_title()
    want_pid = proc.pid if proc is not None and proc.poll() is None else None
    found = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        if win32gui.GetWindowText(hwnd) != title:
            return
        if want_pid is not None:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid != want_pid:
                return
        found.append(hwnd)

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else None


class ScrcpyContainer(QWidget):
    """scrcpy 窗口的嵌入容器，按手机屏幕比例等比适配并居中。"""

    def __init__(self):
        super().__init__()
        self._hwnd: int | None = None
        self._aspect: tuple[int, int] | None = None  # 手机屏幕物理像素 (宽, 高)
        self.setStyleSheet('background: black;')
        self.setMinimumWidth(280)

    def set_hwnd(self, hwnd: int | None) -> None:
        self._hwnd = hwnd

    def embed(self, hwnd: int, aspect: tuple[int, int] | None = None) -> None:
        self._hwnd = hwnd
        self._aspect = aspect
        win32gui.SetParent(hwnd, int(self.winId()))
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        win32gui.SetWindowLong(
            hwnd, win32con.GWL_STYLE,
            style & ~(win32con.WS_CAPTION | win32con.WS_THICKFRAME
                      | win32con.WS_MINIMIZEBOX | win32con.WS_MAXIMIZEBOX),
        )
        win32gui.SetWindowPos(
            hwnd, None, 0, 0, self.width(), self.height(),
            win32con.SWP_NOZORDER | win32con.SWP_FRAMECHANGED | win32con.SWP_SHOWWINDOW,
        )
        self._fit()
        log('scrcpy 窗口已嵌入')

    def _fit(self) -> None:
        """把 scrcpy 窗口等比缩放到容器内最大并居中，避免内部留黑边。"""
        if not self._hwnd:
            return
        cw, ch = self.width(), self.height()
        x, y, w, h = 0, 0, cw, ch
        if self._aspect and self._aspect[0] and self._aspect[1]:
            aw, ah = self._aspect
            scale = min(cw / aw, ch / ah)
            w, h = int(aw * scale), int(ah * scale)
            x, y = (cw - w) // 2, (ch - h) // 2
        win32gui.MoveWindow(self._hwnd, x, y, w, h, True)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()


def device_aspect() -> tuple[int, int] | None:
    """读取手机屏幕物理像素 (宽, 高)，用于等比嵌入；失败返回 None。"""
    try:
        from src.adb.device import Device
        from src.config import find_adb, load_config

        cfg = load_config()
        dev = Device(find_adb(cfg.adb.path), cfg.adb.device_serial)
        return dev.screen_size()
    except Exception:
        return None


class MainWindow(QMainWindow):
    def __init__(self, emulator_mode: bool = False, emulator_device: str | None = None):
        super().__init__()
        self.emulator_mode = emulator_mode
        self.emulator_device = emulator_device
        if emulator_mode:
            log('模拟器模式：调度器将用 qqpet-module-opener 打开 QQ 宠物主页')
        self.setWindowTitle('QQ 宠物自动化助手' + ('（模拟器版）' if emulator_mode else ''))
        self.resize(1200, 750)

        self.scrcpy_view = ScrcpyContainer()
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(5000)

        # 右侧：顶部按钮行（开始/停止）+ 选项卡（日志/统计/设置）
        self.btn_start = QPushButton('开始')
        self.btn_stop = QPushButton('停止')
        self.btn_start.setStyleSheet(START_BTN_STYLE)
        self.btn_stop.setStyleSheet(STOP_BTN_STYLE)
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self.start_runner)
        self.btn_stop.clicked.connect(self.stop_runner)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        btn_row.addStretch()  # 左侧：开始/停止；右侧：画面镜像开关 + 连接测试按钮
        # 右侧按钮放进独立子布局，间距紧凑；不影响左侧开始/停止的间距
        right_btns = QHBoxLayout()
        right_btns.setSpacing(0)  # 右侧按钮间距：紧凑排列（需要留缝可改回 2~4）
        # 画面镜像开关：开=启动 scrcpy 并看门狗自动重连，关=结束进程且不再自动拉起
        self.btn_scrcpy = QPushButton()
        self.btn_scrcpy.setCheckable(True)
        self.btn_scrcpy.setChecked(True)  # 默认开启（沿用原来启动即拉 scrcpy 的行为）
        self.btn_scrcpy.setStyleSheet(SCRCPY_BTN_STYLE)
        self.btn_scrcpy.clicked.connect(self._toggle_scrcpy)
        self._update_scrcpy_btn()
        right_btns.addWidget(self.btn_scrcpy)
        # 连接测试：u2 截图 + OCR 识别 + 控件树拉取 耗时（后台线程执行，结果在日志页）
        self._btn_connect_test = QPushButton('连接测试')
        self._btn_connect_test.clicked.connect(self._test_connect)
        right_btns.addWidget(self._btn_connect_test)
        btn_row.addLayout(right_btns)

        # 日志页：顶部账号状态条 + 当日统计条 + 日志区
        self.status_label = QLabel()
        self.status_label.setStyleSheet(
            'color: #ffd54f; background: #263238; padding: 4px 8px; font-size: 13px;')
        self.stats_label = QLabel()
        self.stats_label.setStyleSheet(
            'color: #ddd; background: #263238; padding: 4px 8px; font-size: 13px;')
        log_page = QWidget()
        log_layout = QVBoxLayout(log_page)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(0)
        log_layout.addWidget(self.status_label)
        log_layout.addWidget(self.stats_label)
        log_layout.addWidget(self.log_view)

        self.tabs = QTabWidget()
        self.tabs.addTab(log_page, '日志')
        self.stats_panel = StatsPanel()
        self.tabs.addTab(self.stats_panel, '统计')
        self.tabs.addTab(self._build_settings_page(), '设置')
        self.tabs.currentChanged.connect(self._on_tab_changed)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addLayout(btn_row)
        right_layout.addWidget(self.tabs)

        splitter = QSplitter()
        splitter.addWidget(self.scrcpy_view)
        splitter.addWidget(right_panel)
        splitter.setSizes([420, 780])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        self.setCentralWidget(splitter)

        self._scrcpy_proc: subprocess.Popen | None = None
        self._runner_proc: subprocess.Popen | None = None
        self._runner_started_at: float | None = None  # 调度器启动时刻（monotonic），日志页显示运行时间用

        # 日志：本进程监听器 + 调度子进程 stdout -> 队列 -> 定时器刷到界面
        self._log_queue: queue.Queue = queue.Queue()
        add_log_listener(self._log_queue.put)
        self._log_timer = QTimer(self, timeout=self._drain_logs)
        self._log_timer.start(100)
        # 当日统计：每 5 秒从进度文件刷新一次
        self._stats_timer = QTimer(self, timeout=self._refresh_stats)
        self._stats_timer.start(5000)
        self._refresh_stats()

        self._embed_tries = 0
        self._embed_timer = QTimer(self, timeout=self._try_embed)
        # scrcpy 看门狗：设备重启/掉线后 scrcpy 进程会退出，自动重拉并重嵌入
        self._scrcpy_retry_at = 0.0
        self._scrcpy_watchdog = QTimer(self, timeout=self._check_scrcpy)
        self._scrcpy_watchdog.start(SCRCPY_WATCHDOG_MS)
        # 画面镜像关闭时关屏一次：GUI 侧 adb Device（懒加载复用）
        self._adb_dev = None
        self._adb_dev_key = None
        self._screen_off_proc: subprocess.Popen | None = None  # 无头关屏 scrcpy
        # 配置保存后重启调度器的防抖定时器
        self._restart_timer = QTimer(self, singleShot=True, interval=1500,
                                     timeout=self._restart_runner)
        # 连接测试：GUI 侧独立 u2 连接（懒加载复用，adb 配置变化自动重建）
        self._test_dev = None
        self._test_dev_key = None
        self._test_lock = threading.Lock()
        # 后台测试线程完成 -> 主线程恢复按钮（QTimer.singleShot 在非 Qt 线程不可靠，
        # 用信号跨线程投递）
        self._test_signals = _TestSignals()
        self._test_signals.finished.connect(self._set_test_btn_enabled)

        QTimer.singleShot(0, self._start_all)

    # ---- 启动流程 ----

    def _start_all(self) -> None:
        # Qt 槽里未捕获的异常会直接 abort 进程（无 traceback 的"闪退"），
        # 启动失败记日志并继续，调度器仍可手动开始
        try:
            kill_previous_scrcpy()
            if self.btn_scrcpy.isChecked():
                self._scrcpy_proc = start_scrcpy()
            else:
                log('画面镜像开关关闭，跳过启动')
                self._screen_off_proc = start_scrcpy_screen_off()
        except Exception:
            import traceback

            log(f'启动 scrcpy 失败:\n{traceback.format_exc()}')
            self._scrcpy_proc = None
        if self._scrcpy_proc:
            self._embed_tries = 0
            self._embed_timer.start(500)

    def _try_embed(self) -> None:
        hwnd = find_scrcpy_hwnd(self._scrcpy_proc)
        if hwnd:
            self.scrcpy_view.embed(hwnd, device_aspect())
            self._embed_timer.stop()
            return
        self._embed_tries += 1
        if self._embed_tries >= EMBED_TRIES:
            self._embed_timer.stop()
            log('未找到 scrcpy 窗口，嵌入失败（调度器仍可正常开始）')

    def _check_scrcpy(self) -> None:
        """看门狗：scrcpy 进程掉了（设备 adb reboot/掉线会断开）就重拉并重嵌入。

        重拉失败（设备还没开机完成）退避 SCRCPY_RETRY_INTERVAL 秒再试，
        避免设备重启期间每 5 秒刷一次失败日志。
        """
        if not self.btn_scrcpy.isChecked():
            return  # 画面镜像已关闭，不自动拉起
        if not SCRCPY.is_file() or self._embed_timer.isActive():
            return  # 没有 scrcpy 可拉，或启动/重嵌流程正在进行
        if self._scrcpy_proc is not None and self._scrcpy_proc.poll() is None:
            return  # 活着
        now = time.monotonic()
        if now < self._scrcpy_retry_at:
            return
        had_proc = self._scrcpy_proc is not None
        self.scrcpy_view.set_hwnd(None)
        self._scrcpy_proc = start_scrcpy()
        if self._scrcpy_proc:
            log('scrcpy 已重连' if had_proc else 'scrcpy 已启动')
            self._embed_tries = 0
            self._embed_timer.start(500)
        else:
            self._scrcpy_retry_at = now + SCRCPY_RETRY_INTERVAL

    # ---- 当日统计 ----

    def _run_time_prefix(self) -> str:
        """调度器运行时长前缀（未运行时显示 0小时0分钟）。"""
        if (self._runner_started_at is not None
                and self._runner_proc is not None
                and self._runner_proc.poll() is None):
            secs = int(time.monotonic() - self._runner_started_at)
        else:
            secs = 0
        return f'运行时间 {secs // 3600}小时{(secs % 3600) // 60}分钟　'

    def _refresh_stats(self) -> None:
        """刷新日志页顶部：运行时间 + 账号状态条（状态缓存，每账号一行）+ 各任务当日统计。"""
        try:
            run_prefix = self._run_time_prefix()
            accounts = load_accounts()
            if accounts:
                # 多账号兼容：缓存里每个账号一行（运行时间只在首行显示一次）
                lines = []
                for i, (_, st) in enumerate(accounts.items()):
                    parts = '　'.join(f'{label} {st.get(key, "-")}'
                                      for key, label in STATUS_FIELDS)
                    lines.append(run_prefix + parts if i == 0 else parts)
                self.status_label.setText('\n'.join(lines))
            else:
                self.status_label.setText(run_prefix + '账号状态: 暂无（调度器运行后自动更新）')
        except Exception as e:
            self.status_label.setText(f'账号状态读取失败: {e}')
        try:
            cfg = load_config()
            tasks = [
                ('学习', SCHOOL_PROGRESS_FILE, cfg.school.times_per_day),
                ('打工', WORK_PROGRESS_FILE, cfg.work.times_per_day),
                ('冒险', ADVENTURE_PROGRESS_FILE, cfg.adventure.times_per_day),
                ('踩踩', VISIT_PROGRESS_FILE, cfg.visit.times_per_day),
                ('PK', PK_PROGRESS_FILE, cfg.pk.times_per_day),
            ]
            accounts = known_accounts()
            if not accounts:
                accounts = ['']  # 未识别账号：读默认路径，单行显示（不带账号前缀）
            lines = []
            for name in accounts:
                parts = []
                for label, progress_file, limit in tasks:
                    _, done, _ = load_progress(progress_file, quiet=True, account=name)
                    parts.append(f'{label} {done}/{limit}' if limit else f'{label} {done}')
                    if label == '踩踩':
                        # 经验日常（好友照顾）当日是否完成：踩踩次数满但经验未完成时仍会继续
                        _, exp_done, _ = load_exp_daily(quiet=True, account=name)
                        parts.append('经验日常' + ('✓' if exp_done else '✗'))
                lines.append('今日: ' + '　'.join(parts))
            self.stats_label.setText('\n'.join(lines))
        except Exception as e:
            self.stats_label.setText(f'今日统计读取失败: {e}')

    # ---- 设置页面 ----

    def _build_settings_page(self) -> QWidget:
        """设置页：表单编辑 config.yaml，字段失焦自动保存（保留注释）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self._setting_widgets: dict = {}
        # 护理方式选"一键护理"时体力/清洁阈值用不上，隐藏对应表单行（label + 控件）
        self._care_threshold_rows: dict = {}
        for key, label, kind in SETTING_FIELDS:
            if kind == 'int':
                w = _NoWheelSpinBox()
                # 体力/清洁是 0-100，其余次数/阈值放宽
                w.setRange(0, 100 if key.startswith('care.') else 99999)
                # 用 valueChanged 而非 editingFinished：滚轮/滚动导致的失焦不再误触发保存，
                # 只有数值真的变化（箭头/键盘/输入提交）才保存；_load_settings 用 blockSignals 防误存
                w.valueChanged.connect(lambda _v, k=key: self.save_field(k))
            elif kind == 'bool':
                w = QCheckBox()
                w.stateChanged.connect(lambda _s, k=key: self.save_field(k))
            elif kind == 'text':
                # 多行文本（如 OnePush YAML 配置）：失焦自动保存，下方附配置教程链接
                w = _FocusOutPlainTextEdit(lambda k=key: self.save_field(k))
                w.setMinimumHeight(60)
                w.setMaximumHeight(110)
                w.setPlaceholderText('provider: bark\nkey: 你的Key')
                help_label = QLabel(f'<a href="{ONEPUSH_HELP_URL}">OnePush 配置教程</a>')
                help_label.setOpenExternalLinks(True)
                col = QWidget()
                col_layout = QVBoxLayout(col)
                col_layout.setContentsMargins(0, 0, 0, 0)
                col_layout.setSpacing(4)
                col_layout.addWidget(w)
                col_layout.addWidget(help_label)
                self._setting_widgets[key] = (w, kind)
                form.addRow(label, col)
                continue
            elif kind == 'devices' or isinstance(kind, list):
                w = QComboBox()
                if kind == 'devices':
                    # 设备序列号：可编辑下拉——既可从在线设备里选，也可手动输入
                    # （模拟器 127.0.0.1:7555 这类地址可能还没连进 adb，下拉里没有）
                    w.setEditable(True)
                    w.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)  # 手动输入不追加进下拉
                    w.lineEdit().setPlaceholderText('自动（第一台）或输入序列号，如 127.0.0.1:7555')
                    w.setToolTip('从下拉选择在线设备，或直接输入设备序列号/模拟器 adb 地址')
                    # 手动输入每敲一个字符就保存会反复重启 scrcpy/调度器，
                    # 只在 选了下拉项（activated）或 输入结束（Enter/失焦）时保存
                    w.activated.connect(lambda _i, k=key: self.save_field(k))
                    w.lineEdit().editingFinished.connect(lambda k=key: self.save_field(k))
                else:
                    if isinstance(kind, list):
                        w.addItems(kind)
                    w.currentTextChanged.connect(lambda _t, k=key: self.save_field(k))
                    if key == 'care.method':
                        # 护理方式变化时联动显隐体力/清洁阈值（一键护理不读状态，阈值无意义）
                        w.currentTextChanged.connect(self._on_care_method_changed)
            else:
                w = QLineEdit()
                w.editingFinished.connect(lambda k=key: self.save_field(k))
            self._setting_widgets[key] = (w, kind)
            form.addRow(label, w)
            if key in ('care.energy_threshold', 'care.clean_threshold'):
                self._care_threshold_rows[key] = (form.labelForField(w), w)
        # 通知测试：按当前 config.yaml 的 notify 配置发一条测试告警。
        # 点击按钮会先让输入框失焦（失焦自动保存），未落盘的修改也会先生效；
        # 各渠道发送结果见日志页
        test_btn = QPushButton('发送通知测试')
        test_btn.clicked.connect(self._test_notify)
        form.addRow('通知测试', test_btn)
        form_widget = QWidget()
        form_widget.setLayout(form)
        scroll = QScrollArea()
        scroll.setWidget(form_widget)
        scroll.setWidgetResizable(True)
        layout.addWidget(scroll)
        return page

    def _on_tab_changed(self, index: int) -> None:
        """切到设置页（第 3 个选项卡）时加载当前配置。"""
        if index == 2:
            self.load_settings()

    def _on_care_method_changed(self, method: str) -> None:
        """护理方式选"一键护理"时隐藏体力/清洁阈值行（不读状态，阈值用不上）。"""
        hidden = method == '一键护理'
        for label, w in self._care_threshold_rows.values():
            label.setVisible(not hidden)
            w.setVisible(not hidden)

    def _test_notify(self) -> None:
        """设置页"通知测试"按钮：发一条测试告警，各渠道结果打到日志页。"""
        from src.notify import send_alert  # 按需导入（winotify/onepush 均为懒加载）

        log('发送通知测试...')
        sent = send_alert('通知测试：收到这条说明告警渠道配置正常')
        log('通知测试已送达' if sent else '通知测试未送达（检查配置，各渠道详情见上方日志）')

    def _get_test_dev(self) -> 'U2Device':
        """测试用 u2 连接：懒加载并复用；adb 路径/序列号变化时自动重建。

        供 OCR/控件树测试的后台线程调用（须已持有 self._test_lock）。
        """
        from src.config import find_adb

        cfg = load_config()
        key = (find_adb(cfg.adb.path), cfg.adb.device_serial)
        if self._test_dev is None or self._test_dev_key != key:
            from src.u2dev import U2Device

            self._test_dev = U2Device(key[0], key[1])
            self._test_dev_key = key
        return self._test_dev

    def _get_adb_dev(self) -> Device:
        """GUI 侧 adb Device（维持屏幕关闭用），懒加载并按 adb 配置变化重建。"""
        from src.config import find_adb

        cfg = load_config()
        key = (find_adb(cfg.adb.path), cfg.adb.device_serial)
        if self._adb_dev is None or self._adb_dev_key != key:
            self._adb_dev = Device(key[0], key[1])
            self._adb_dev_key = key
        return self._adb_dev

    def _set_test_btn_enabled(self, enabled: bool) -> None:
        """启用/禁用"连接测试"按钮（避免测试期间重复触发）。"""
        try:
            self._btn_connect_test.setEnabled(enabled)
        except RuntimeError:  # 窗口已关闭，控件已销毁
            pass

    def _test_connect(self) -> None:
        """顶部"连接测试"按钮：u2 截图 + OCR 识别 + 控件树拉取 的实测耗时。

        不计入 u2 连接与 OCR 引擎首次加载/预热时间（预热一轮后计时），
        后台线程执行，结果打到日志页。
        """
        self._set_test_btn_enabled(False)

        def work() -> None:
            try:
                from src.ocr import get_engine, ocr_fullscreen

                with self._test_lock:
                    dev = self._get_test_dev()
                    w, h = dev.window_size()
                    log(f'[连接测试] 设备 {w}x{h}，u2 连接就绪（不计时）')
                    get_engine()  # OCR 引擎首次加载（懒加载，不计时）
                    log('[连接测试] OCR 引擎已加载（首次加载不计时）')
                    # 预热一轮：截图+OCR、控件树各一次（首次调用初始化，不计时）
                    ocr_fullscreen(dev.screenshot())
                    dev.d.dump_hierarchy()
                    log('[连接测试] 预热完成（不计时），开始计时...')
                    # OCR 部分：截图 + 识别
                    for i in (1, 2):
                        t0 = time.perf_counter()
                        screen = dev.screenshot()
                        t1 = time.perf_counter()
                        results = ocr_fullscreen(screen)
                        t2 = time.perf_counter()
                        shot_ms = (t1 - t0) * 1000
                        ocr_ms = (t2 - t1) * 1000
                        log(f'[连接测试] OCR 第{i}轮: 截图 {shot_ms:.1f}ms + 识别 {ocr_ms:.1f}ms'
                            f' = {shot_ms + ocr_ms:.1f}ms，识别 {len(results)} 处文本')
                    # 控件树部分：整树拉取
                    for i in (1, 2):
                        t0 = time.perf_counter()
                        xml = dev.d.dump_hierarchy()
                        t1 = time.perf_counter()
                        ms = (t1 - t0) * 1000
                        size_kb = len(xml.encode('utf-8')) / 1024
                        nodes = xml.count('<node')
                        log(f'[连接测试] 控件树 第{i}轮: 拉取 {ms:.1f}ms，'
                            f'节点 {nodes} 个，XML {size_kb:.1f}KB')
            except Exception as e:
                log(f'[连接测试] 失败: {e}')
            finally:
                try:  # 窗口可能已关闭（信号对象已销毁）
                    self._test_signals.finished.emit(True)
                except RuntimeError:
                    pass

        threading.Thread(target=work, daemon=True).start()

    def load_settings(self) -> None:
        try:
            data = settings_io.load_raw()
        except Exception as e:
            log(f'读取配置失败: {e}')
            return
        for key, (w, kind) in self._setting_widgets.items():
            value = settings_io.get_value(data, key)
            if value is None:
                # 旧 config.yaml 可能缺新增字段：回退默认值，避免 int('') 崩溃
                value = settings_io.DEFAULTS.get(key, '')
            w.blockSignals(True)  # 加载时不触发自动保存
            if kind == 'devices':
                self._fill_devices(w, str(value))
            elif kind == 'int':
                w.setValue(int(value))
            elif kind == 'bool':
                # 配置里缺该键时回退 DEFAULTS，避免与运行时的 dataclass 默认值不一致
                w.setChecked(bool(settings_io.DEFAULTS.get(key) if value == '' else value))
            elif kind == 'text':
                w.setPlainText(str(value))
            elif isinstance(kind, list):
                w.setCurrentText(str(value))
            else:
                w.setText(str(value))
            w.blockSignals(False)
        # blockSignals 抑制了 care.method 的联动信号，加载后手动同步阈值行显隐
        method_w, _ = self._setting_widgets.get('care.method', (None, None))
        if method_w is not None:
            self._on_care_method_changed(method_w.currentText())

    def _fill_devices(self, combo: QComboBox, current: str) -> None:
        """枚举在线 adb 设备填充序列号下拉（可编辑，支持手动输入），
        首项为 自动（第一台）。手动输入的序列号（不在线/模拟器地址）下拉里没有时写回编辑框。"""
        combo.clear()
        combo.addItem('自动（第一台）', '')
        try:
            from src.adb.device import Device
            from src.config import find_adb

            dev = Device(find_adb(load_config().adb.path))
            for serial in dev.online_devices():
                combo.addItem(serial, serial)
        except Exception as e:
            log(f'枚举设备失败: {e}')
        idx = combo.findData(current)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        else:
            combo.setEditText(current)  # 下拉里没有的自定义序列号，填回编辑框

    def save_field(self, key: str) -> None:
        """字段失焦自动保存：校验 -> 写回 config.yaml -> 调度器在跑则延时重启生效。"""
        w, kind = self._setting_widgets[key]
        if kind == 'devices':
            # 可编辑下拉：用户手动输入不匹配任何下拉项时，Qt 仍保留上次选中项的
            # currentData（会误判成已选中的值），所以统一按显示文本保存：
            # 下拉的“自动（第一台）”项 → ''，其余（在线设备/手动输入）直接用文本本身
            text = w.currentText().strip()
            value = '' if text == '自动（第一台）' else text
        elif kind == 'int':
            value = w.value()
        elif kind == 'bool':
            value = w.isChecked()
        elif kind == 'text':
            value = w.toPlainText().strip()
        elif isinstance(kind, list):
            value = w.currentText()
        else:
            value = w.text().strip()
        ok, fixed = settings_io.validate_field(key, value)
        if not ok:
            log(f'配置 {key} 的值 {value!r} 无效，已恢复默认值 {fixed!r}')
            w.blockSignals(True)  # 恢复默认值不再触发一次保存
            if kind == 'devices':
                idx = w.findData(fixed)
                if idx >= 0:
                    w.setCurrentIndex(idx)
                else:
                    w.setEditText(str(fixed))
            elif kind == 'int':
                w.setValue(fixed)
            elif kind == 'bool':
                w.setChecked(bool(fixed))
            elif kind == 'text':
                w.setPlainText(str(fixed))
            elif isinstance(kind, list):
                w.setCurrentText(fixed)
            else:
                w.setText(str(fixed))
            w.blockSignals(False)
        try:
            data = settings_io.load_raw()  # 重新读取，避免覆盖其他字段
            settings_io.set_value(data, key, fixed)
            settings_io.save_raw(data)
        except Exception as e:
            log(f'保存配置失败: {e}')
            return
        log(f'配置已保存: {key} = {fixed}')
        if key in ('adb.device_serial', 'adb.path'):
            # adb 连接相关：重拉 scrcpy，调度器也需要重启重建连接
            self._restart_scrcpy()
            if self._runner_proc and self._runner_proc.poll() is None:
                self._restart_timer.start()  # 防抖：连续修改多个字段只重启一次
        elif self._runner_proc and self._runner_proc.poll() is None:
            log('调度器每轮自动重读配置，最迟下一轮生效（无需重启）')

    def _connect_emulator_adb(self) -> None:
        """模拟器模式下，先确保 adb 已连接远程模拟器（127.0.0.1:port）。

        模拟器（MuMu/雷电等）可能还没进 adb devices，不先 connect 的话
        scrcpy/u2 都连不上；失败只记日志（可能本来就已连接）。
        """
        if not self.emulator_mode:
            return
        try:
            serial = self.emulator_device or load_config().adb.device_serial
            if serial and ':' in serial:
                Device(find_adb(load_config().adb.path), serial).connect_remote(serial)
        except Exception as e:
            log(f'adb connect 模拟器失败: {e}')

    def _restart_scrcpy(self) -> None:
        """杀掉并重拉 scrcpy（换设备/换 adb 后画面也需要切换）。"""
        if not self.btn_scrcpy.isChecked():
            return  # 开关关闭时不启动 scrcpy
        log('重新初始化 scrcpy...')
        kill_our_scrcpy(self._scrcpy_proc)
        self.scrcpy_view.set_hwnd(None)
        self._connect_emulator_adb()
        self._scrcpy_proc = start_scrcpy()
        if self._scrcpy_proc:
            self._embed_tries = 0
            self._embed_timer.start(500)

    def _update_scrcpy_btn(self) -> None:
        """按开关状态刷新按钮文字（开/关样式由 checked 状态驱动）。"""
        self.btn_scrcpy.setText('画面镜像: 开' if self.btn_scrcpy.isChecked() else '画面镜像: 关')

    def _toggle_scrcpy(self) -> None:
        """scrcpy 开关点击：开=启动并嵌入，关=结束进程且不再自动拉起。"""
        if self.btn_scrcpy.isChecked():
            log('开启 scrcpy...')
            self._enable_scrcpy()
        else:
            self._disable_scrcpy()
        self._update_scrcpy_btn()

    def _enable_scrcpy(self) -> None:
        """启动 scrcpy 并开始查找嵌入（看门狗随后自动维护重连）。"""
        if self._screen_off_proc is not None and self._screen_off_proc.poll() is None:
            log('结束屏幕关闭 scrcpy')
            self._screen_off_proc.terminate()
        self._screen_off_proc = None
        if self._scrcpy_proc is not None and self._scrcpy_proc.poll() is None:
            return  # 已在运行
        self.scrcpy_view.set_hwnd(None)
        self._connect_emulator_adb()
        self._scrcpy_proc = start_scrcpy()
        if self._scrcpy_proc:
            self._embed_tries = 0
            self._embed_timer.start(500)

    def _disable_scrcpy(self) -> None:
        """结束 scrcpy 并停止嵌入/看门狗维护（开关关闭状态）。"""
        self._embed_timer.stop()
        kill_our_scrcpy(self._scrcpy_proc)
        self._scrcpy_proc = None
        self.scrcpy_view.set_hwnd(None)
        # 镜像关闭：用无头 scrcpy 真正关掉设备屏幕（保持自动化可用）
        self._screen_off_proc = start_scrcpy_screen_off()

    def _restart_runner(self) -> None:
        if self._runner_proc and self._runner_proc.poll() is None:
            log('重启调度器使配置即时生效...')
            self.stop_runner()
            QTimer.singleShot(500, self.start_runner)

    # ---- 调度器控制：开始 = 启动子进程，停止 = 结束子进程 ----

    def start_runner(self) -> None:
        if self._runner_proc and self._runner_proc.poll() is None:
            return
        log('启动调度器...')
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        # onefile 子进程默认复用父进程的 _MEI 解压目录（_MEIPASS2 环境变量），
        # 调度器快速杀拉/并发时可能把共享目录搞坏（表现为惰性导入的模块/资源
        # 突然"找不到"）；去掉后 runner 用自己的独立解压目录，互不影响
        env.pop('_MEIPASS2', None)
        if getattr(sys, 'frozen', False):
            cmd = [sys.executable, '--runner']  # 打包后：以 --runner 参数重启自身
        else:
            cmd = [sys.executable, '-u', str(RUNNER_SCRIPT)]
        if self.emulator_mode:
            cmd.append('--emulator')
        else:
            cmd.append('--no-emulator')
        if self.emulator_device:
            cmd += ['--emulator-device', self.emulator_device]
        self._runner_proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            env=env,
            creationflags=_NO_WINDOW,
        )
        threading.Thread(
            target=self._read_runner_logs, args=(self._runner_proc,), daemon=True
        ).start()
        self._runner_started_at = time.monotonic()

    def stop_runner(self) -> None:
        if self._runner_proc and self._runner_proc.poll() is None:
            log('结束调度器进程')
            self._runner_proc.terminate()
        self._runner_started_at = None

    def _read_runner_logs(self, proc: subprocess.Popen) -> None:
        """把调度器子进程的输出逐行送入日志队列。"""
        for line in proc.stdout:
            self._log_queue.put(line.rstrip())
        log('调度器已结束')

    # ---- 日志刷新 ----

    def _drain_logs(self) -> None:
        bar = self.log_view.verticalScrollBar()
        # 只有用户本来就在底部时才跟随新日志；向上翻看时保持当前位置
        was_at_bottom = bar.value() >= bar.maximum() - 2
        added = False
        while True:
            try:
                line = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_view.appendPlainText(line)
            added = True
        if added and was_at_bottom:
            bar.setValue(bar.maximum())
        # 按调度器进程状态同步按钮
        running = bool(self._runner_proc and self._runner_proc.poll() is None)
        if not running:
            self._runner_started_at = None
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)

    # ---- 退出 ----

    def closeEvent(self, event) -> None:
        if self._runner_proc and self._runner_proc.poll() is None:
            self._runner_proc.terminate()
        # 只结束由本程序拉起的 scrcpy
        if self._scrcpy_proc and self._scrcpy_proc.poll() is None:
            log('关闭 scrcpy')
            self._scrcpy_proc.terminate()
        if self._screen_off_proc and self._screen_off_proc.poll() is None:
            log('结束屏幕关闭 scrcpy')
            self._screen_off_proc.terminate()
        event.accept()


def _ensure_runtime_resources(emulator: bool) -> None:
    """确保 scrcpy / 模拟器版 frida-server 已就位（缺失不阻塞，后台线程）。

    源码运行：缺失时自动调用对应 fetch 工具下载（幂等），失败给出手动下载地址与放置位置。
    打包运行（frozen）：资源随包或放在 exe 旁 runs/（可写数据目录，覆盖随包资源）；缺失时提示
    可手动放到 exe 旁 runs/ 的对应目录（或重新打包），不联网下载。
    """
    frozen = getattr(sys, 'frozen', False)

    def work() -> None:
        # scrcpy：画面镜像必需（不区分普通/模拟器模式）
        if not SCRCPY.is_file():
            if frozen:
                log(f'未找到 scrcpy。需要画面镜像请手动放置（或重新打包），exe 旁 runs 目录：'
                    f'{APP_ROOT / "runs" / "resources" / "scrcpy-win64"}/（内含 scrcpy.exe）')
            else:
                log('未找到 scrcpy，正在自动下载（tools/fetch_scrcpy.py）...')
                fetch = PROJECT_ROOT / 'tools' / 'fetch_scrcpy.py'
                if fetch.is_file():
                    subprocess.run([sys.executable, str(fetch)], check=False)
                if SCRCPY.is_file():
                    log('scrcpy 已就绪')
                else:
                    # 下载失败不阻塞：给出下载地址与放置位置，用户手动处理
                    log(f'scrcpy 自动下载失败。请手动下载 scrcpy win64 并解压到 {SCRCPY.parent}：\n'
                        f'  地址: https://github.com/Genymobile/scrcpy/releases （scrcpy-win64-vX.zip，需含 scrcpy.exe）')
        # frida-server：模拟器模式需要
        if emulator:
            from src.opener import FRIDA_SERVER_REL
            frida_dir = resource_path(FRIDA_SERVER_REL)
            if not any(frida_dir.glob('frida-server-*.xz')):
                if frozen:
                    log(f'未找到 frida-server 离线包。需要模拟器功能请手动下载并放到 exe 旁 runs 目录：'
                        f'{APP_ROOT / "runs" / FRIDA_SERVER_REL}/（frida-server-<版本>-android-x86_64.xz，'
                        f'版本须与 requirements.txt 的 frida 一致），或重新打包模拟器版')
                else:
                    log('未找到 frida-server 离线包，正在自动下载（tools/fetch_frida_server.py）...')
                    fetch = PROJECT_ROOT / 'tools' / 'fetch_frida_server.py'
                    if fetch.is_file():
                        subprocess.run([sys.executable, str(fetch)], check=False)
                    if any(frida_dir.glob('frida-server-*.xz')):
                        log('frida-server 离线包已就绪')
                    else:
                        # 下载失败不阻塞：给出下载地址与放置位置，用户手动处理
                        try:
                            import frida
                            ver = frida.__version__
                        except Exception:
                            ver = '<版本>'
                        log(f'frida-server 自动下载失败。请手动下载并放到 {frida_dir}：\n'
                            f'  地址: https://github.com/frida/frida/releases/download/{ver}/'
                            f'frida-server-{ver}-android-x86_64.xz\n'
                            f'  （文件名中的版本须与 requirements.txt 的 frida 一致）')

    threading.Thread(target=work, daemon=True).start()


def _parse_emulator_args() -> tuple[bool, str | None]:
    """解析 --emulator / --no-emulator / --emulator-device。

    未显式指定时：打包的模拟器版（内置 emulator_mode.txt 标记）默认开启，
    普通版/源码默认关闭。返回 (是否模拟器模式, 模拟器设备地址或 None)。
    """
    emulator = is_emulator_build()
    device = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == '--emulator':
            emulator = True
        elif arg == '--no-emulator':
            emulator = False
        elif arg == '--emulator-device' and i + 1 < len(args):
            device = args[i + 1]
            i += 1
        i += 1
    return emulator, device


def _strip_emulator_args(argv: list[str]) -> list[str]:
    """从 argv 里去掉 emulator 专用参数（QApplication 不认识的参数不报错，
    但清理干净更稳妥）。"""
    out = [argv[0]]
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ('--emulator', '--no-emulator'):
            i += 1
            continue
        if arg == '--emulator-device':
            i += 2  # 连后面的值一起去掉
            continue
        out.append(arg)
        i += 1
    return out


def main() -> None:
    emulator, emulator_device = _parse_emulator_args()
    if '--runner' in sys.argv:
        # 调度器子进程模式（打包后由 GUI 以 --runner 参数拉起）
        # windowed 打包的程序 stdout 用本地编码(GBK)，强制改 UTF-8，否则 GUI 日志乱码
        for stream in (sys.stdout, sys.stderr):
            if stream is not None and hasattr(stream, 'reconfigure'):
                try:
                    stream.reconfigure(encoding='utf-8', errors='replace')
                except (OSError, ValueError):
                    pass  # windowed 下 stdout/stderr 可能是无效流
        from scenarios.runner import Runner

        Runner(use_opener=emulator, opener_serial=emulator_device).run()
        return
    _ensure_runtime_resources(emulator)
    app = QApplication(_strip_emulator_args(sys.argv))
    window = MainWindow(emulator_mode=emulator, emulator_device=emulator_device)
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
