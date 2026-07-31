"""启动入口（PyQt6 GUI）：左侧嵌入 scrcpy 窗口，右侧实时显示调度器日志。

- 启动前结束已有 scrcpy.exe 进程，再重新拉起并以 --window-borderless 嵌入
- scrcpy 以 --turn-screen-off 运行（手机屏幕关闭，镜像照常）
- 右侧顶部"开始/停止"按钮：开始 = 子进程启动调度器，停止 = 立即结束调度器进程
- 调度器子进程的 stdout 实时显示在右侧日志区
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

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

import win32con
import win32gui

from src import settings as settings_io
from src.config import PROJECT_ROOT, resource_path
from src.progress import add_log_listener, log

SCRCPY = resource_path('scrcpy-win64') / 'scrcpy.exe'
SCRCPY_TITLE = 'QQPetCopilotScrcpy'
RUNNER_SCRIPT = PROJECT_ROOT / 'scenarios' / 'runner.py'
EMBED_TRIES = 40  # 查找 scrcpy 窗口的次数（每次 500ms）

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
SETTINGS_BTN_STYLE = 'QPushButton { background-color: #607D8B; }' + _BTN_BASE.format(
    hover='#546E7A', pressed='#455A64', disabled='#CFD8DC')

# 设置页面字段：(点路径, 显示名, 类型)  类型: 'int' / 'str' / 选项列表
SETTING_FIELDS = [
    ('adb.path', 'adb 路径', 'str'),
    ('adb.device_serial', '设备序列号', 'str'),
    ('school.attribute', '属性点课程', ['力量', '智力', '魅力']),
    ('school.times_per_day', '每天学习次数（0 不限）', 'int'),
    ('work.location', '打工地点', 'str'),
    ('work.times_per_day', '每天打工次数（0 不限）', 'int'),
    ('work.employ_scroll_limit', '雇佣拖动上限', 'int'),
    ('schedule.coin_threshold', '金币阈值', 'int'),
    ('schedule.school_factor', '学习点数系数', 'int'),
    ('schedule.work_factor', '打工点数系数', 'int'),
    ('schedule.daily_point_limit', '每日点数上限', 'int'),
    ('adventure.times_per_day', '每天冒险次数（0 不冒险）', 'int'),
    ('adventure.start_time', '冒险调度时间（HH:MM）', 'str'),
    ('care.energy_threshold', '体力阈值', 'int'),
    ('care.clean_threshold', '清洁阈值', 'int'),
]


def kill_existing_scrcpy() -> None:
    """结束已在运行的 scrcpy.exe 进程。"""
    proc = subprocess.run(
        ['taskkill', '/F', '/IM', 'scrcpy.exe'],
        capture_output=True, timeout=15, creationflags=_NO_WINDOW,
    )
    if proc.returncode == 0:
        log('已结束之前运行的 scrcpy 进程')


def start_scrcpy() -> subprocess.Popen | None:
    """以无边框、关屏、固定标题启动 scrcpy，返回进程。"""
    if not SCRCPY.is_file():
        log(f'未找到 {SCRCPY}，跳过 scrcpy 启动')
        return None
    log('启动 scrcpy（--turn-screen-off --window-borderless）...')
    proc = subprocess.Popen(
        [str(SCRCPY), '--turn-screen-off', '--window-borderless',
         f'--window-title={SCRCPY_TITLE}',
         # 先放到屏幕外，嵌入容器时再移回来，避免窗口先弹出再嵌入的闪烁
         '--window-x=-2000', '--window-y=-2000'],
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


def find_scrcpy_hwnd() -> int | None:
    """按固定标题查找 scrcpy 窗口句柄。"""
    found = []

    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == SCRCPY_TITLE:
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
    def __init__(self):
        super().__init__()
        self.setWindowTitle('QQ 宠物自动化助手')
        self.resize(1200, 750)

        self.scrcpy_view = ScrcpyContainer()
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(5000)

        # 右侧：顶部按钮行（开始/停止/设置）+ 页面（日志/设置切换）
        self.btn_start = QPushButton('开始')
        self.btn_stop = QPushButton('停止')
        self.btn_settings = QPushButton('设置')
        self.btn_start.setStyleSheet(START_BTN_STYLE)
        self.btn_stop.setStyleSheet(STOP_BTN_STYLE)
        self.btn_settings.setStyleSheet(SETTINGS_BTN_STYLE)
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self.start_runner)
        self.btn_stop.clicked.connect(self.stop_runner)
        self.btn_settings.clicked.connect(self.toggle_settings)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        btn_row.addWidget(self.btn_settings)
        btn_row.addStretch()  # 按钮收缩到文字宽度，不铺满整行

        self.pages = QStackedWidget()
        self.pages.addWidget(self.log_view)  # 页 0：日志
        self.pages.addWidget(self._build_settings_page())  # 页 1：设置

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addLayout(btn_row)
        right_layout.addWidget(self.pages)

        splitter = QSplitter()
        splitter.addWidget(self.scrcpy_view)
        splitter.addWidget(right_panel)
        splitter.setSizes([420, 780])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        self.setCentralWidget(splitter)

        # 日志：本进程监听器 + 调度子进程 stdout -> 队列 -> 定时器刷到界面
        self._log_queue: queue.Queue = queue.Queue()
        add_log_listener(self._log_queue.put)
        self._log_timer = QTimer(self, timeout=self._drain_logs)
        self._log_timer.start(100)

        self._scrcpy_proc: subprocess.Popen | None = None
        self._runner_proc: subprocess.Popen | None = None
        self._embed_tries = 0
        self._embed_timer = QTimer(self, timeout=self._try_embed)
        # 配置保存后重启调度器的防抖定时器
        self._restart_timer = QTimer(self, singleShot=True, interval=1500,
                                     timeout=self._restart_runner)

        QTimer.singleShot(0, self._start_all)

    # ---- 启动流程 ----

    def _start_all(self) -> None:
        kill_existing_scrcpy()
        self._scrcpy_proc = start_scrcpy()
        if self._scrcpy_proc:
            self._embed_tries = 0
            self._embed_timer.start(500)

    def _try_embed(self) -> None:
        hwnd = find_scrcpy_hwnd()
        if hwnd:
            self.scrcpy_view.embed(hwnd, device_aspect())
            self._embed_timer.stop()
            return
        self._embed_tries += 1
        if self._embed_tries >= EMBED_TRIES:
            self._embed_timer.stop()
            log('未找到 scrcpy 窗口，嵌入失败（调度器仍可正常开始）')

    # ---- 设置页面 ----

    def _build_settings_page(self) -> QWidget:
        """设置页：表单编辑 config.yaml，字段失焦自动保存（保留注释）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self._setting_widgets: dict = {}
        for key, label, kind in SETTING_FIELDS:
            if kind == 'int':
                w = QSpinBox()
                # 体力/清洁是 0-100，其余次数/阈值放宽
                w.setRange(0, 100 if key.startswith('care.') else 99999)
                w.editingFinished.connect(lambda k=key: self.save_field(k))
            elif isinstance(kind, list):
                w = QComboBox()
                w.addItems(kind)
                w.currentTextChanged.connect(lambda _t, k=key: self.save_field(k))
            else:
                w = QLineEdit()
                w.editingFinished.connect(lambda k=key: self.save_field(k))
            self._setting_widgets[key] = (w, kind)
            form.addRow(label, w)
        form_widget = QWidget()
        form_widget.setLayout(form)
        scroll = QScrollArea()
        scroll.setWidget(form_widget)
        scroll.setWidgetResizable(True)
        layout.addWidget(scroll)
        return page

    def toggle_settings(self) -> None:
        """日志页 <-> 设置页切换，进入设置页时加载当前配置。"""
        if self.pages.currentIndex() == 0:
            self.load_settings()
            self.pages.setCurrentIndex(1)
            self.btn_settings.setText('日志')
        else:
            self.pages.setCurrentIndex(0)
            self.btn_settings.setText('设置')

    def load_settings(self) -> None:
        try:
            data = settings_io.load_raw()
        except Exception as e:
            log(f'读取配置失败: {e}')
            return
        for key, (w, kind) in self._setting_widgets.items():
            value = settings_io.get_value(data, key)
            if value is None:
                continue
            w.blockSignals(True)  # 加载时不触发自动保存
            if kind == 'int':
                w.setValue(int(value))
            elif isinstance(kind, list):
                w.setCurrentText(str(value))
            else:
                w.setText(str(value))
            w.blockSignals(False)

    def save_field(self, key: str) -> None:
        """字段失焦自动保存：校验 -> 写回 config.yaml -> 调度器在跑则延时重启生效。"""
        w, kind = self._setting_widgets[key]
        if kind == 'int':
            value = w.value()
        elif isinstance(kind, list):
            value = w.currentText()
        else:
            value = w.text().strip()
        ok, fixed = settings_io.validate_field(key, value)
        if not ok:
            log(f'配置 {key} 的值 {value!r} 无效，已恢复默认值 {fixed!r}')
            w.blockSignals(True)  # 恢复默认值不再触发一次保存
            if kind == 'int':
                w.setValue(fixed)
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
        if self._runner_proc and self._runner_proc.poll() is None:
            self._restart_timer.start()  # 防抖：连续修改多个字段只重启一次

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
        if getattr(sys, 'frozen', False):
            cmd = [sys.executable, '--runner']  # 打包后：以 --runner 参数重启自身
        else:
            cmd = [sys.executable, '-u', str(RUNNER_SCRIPT)]
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

    def stop_runner(self) -> None:
        if self._runner_proc and self._runner_proc.poll() is None:
            log('结束调度器进程')
            self._runner_proc.terminate()

    def _read_runner_logs(self, proc: subprocess.Popen) -> None:
        """把调度器子进程的输出逐行送入日志队列。"""
        for line in proc.stdout:
            self._log_queue.put(line.rstrip())
        log('调度器已结束')

    # ---- 日志刷新 ----

    def _drain_logs(self) -> None:
        while True:
            try:
                line = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_view.appendPlainText(line)
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())
        # 按调度器进程状态同步按钮
        running = bool(self._runner_proc and self._runner_proc.poll() is None)
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
        event.accept()


def main() -> None:
    if '--runner' in sys.argv:
        # 调度器子进程模式（打包后由 GUI 以 --runner 参数拉起）
        from scenarios.runner import Runner

        Runner().run()
        return
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
