"""验证画面镜像在窗口隐藏/最小化时暂停抓帧（Linux 专属省电逻辑）。

    PATH=resources/platform-tools:$PATH QT_QPA_PLATFORM=offscreen \
        .venv/bin/python tools/test_mirror_pause.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import main as m  # noqa: E402

app = QApplication(sys.argv)
win = m.MainWindow()
win.show()

state = {'phase': 'wait-start', 't': time.monotonic(), 'f0': 0, 'f1': 0, 'f2': 0}
fails: list[str] = []


def _tick() -> None:
    mir = win._mirror
    now = time.monotonic()
    if state['phase'] == 'wait-start':
        if mir is not None and mir.frames >= 2:
            state.update(phase='shown', t=now, f0=mir.frames)
            print(f'[pause] 已出帧 frames={mir.frames}，进入"可见"阶段')
        elif now - state['t'] > 40:
            fails.append('镜像线程 40s 内没出帧')
            _done()
    elif state['phase'] == 'shown':
        if now - state['t'] >= 8:
            state.update(f1=mir.frames, phase='hide', t=now)
            print(f'[pause] 可见 8s 内新增 {state["f1"] - state["f0"]} 帧，现在隐藏窗口')
            win.hide()
    elif state['phase'] == 'hide':
        # 先给 3s 让"正在飞行中的那一帧"落地：_capture() 单帧约 1s，
        # hide() 时线程可能已经进了 subprocess.run，它会照常返回并 emit 一次。
        if state.get('settled') is None and now - state['t'] >= 3:
            state['settled'] = mir.frames
            print(f'[pause] 隐藏 3s 后（在飞帧已落地）frames={mir.frames}，开始计时')
        elif state.get('settled') is not None and now - state['t'] >= 11:
            grew = mir.frames - state['settled']
            state.update(f2=mir.frames, phase='show', t=now)
            print(f'[pause] 隐藏期间（稳定后 8s）新增 {grew} 帧（期望 0）')
            if grew != 0:
                fails.append(f'隐藏期间仍在抓帧：新增 {grew} 帧')
            if not mir._pause_event.is_set():
                fails.append('隐藏后 _pause_event 未置位')
            win.show()
    elif state['phase'] == 'show':
        if now - state['t'] >= 8:
            grew = mir.frames - state['f2']
            print(f'[pause] 恢复显示 8s 内新增 {grew} 帧（期望 >0）')
            if grew <= 0:
                fails.append('恢复显示后没有继续抓帧')
            if mir._pause_event.is_set():
                fails.append('恢复显示后 _pause_event 仍置位')
            _done()


def _done() -> None:
    print('[pause] 结果:', 'PASS' if not fails else f'FAIL {fails}')
    win.close()
    app.quit()


timer = QTimer()
timer.timeout.connect(_tick)
timer.start(200)
app.exec()
sys.exit(1 if fails else 0)
