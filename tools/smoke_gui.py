"""Linux 无头冒烟测试：构造 MainWindow，跑几秒事件循环，再正常退出。

用于在没接设备/没有显示器的情况下验证 GUI 能否起来（离屏渲染）。
    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/smoke_gui.py [秒数]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import main as m  # noqa: E402

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0

app = QApplication(sys.argv)
win = m.MainWindow()
win.show()
print(f'[smoke] MainWindow 构造完成，标题={win.windowTitle()!r}')
print(f'[smoke] IS_WIN={m.IS_WIN}  SCRCPY={m.SCRCPY}  exists={m.SCRCPY.is_file()}')
print(f'[smoke] 镜像开关={win.btn_scrcpy.isChecked()}')

t0 = time.monotonic()


def _report() -> None:
    mir = win._mirror
    print(f'[smoke] 镜像线程={mir!r} alive={mir.is_alive() if mir else None} '
          f'frames={mir.frames if mir else 0} last_error={mir.last_error if mir else ""!r}')
    print(f'[smoke] 画面容器: hwnd={win.scrcpy_view._hwnd} aspect={win.scrcpy_view._aspect} '
          f'有帧={win.scrcpy_view._frame is not None}')
    win.close()
    app.quit()


QTimer.singleShot(int(seconds * 1000), _report)
rc = app.exec()
print(f'[smoke] 事件循环退出 rc={rc} 用时={time.monotonic() - t0:.1f}s')
sys.exit(0)
