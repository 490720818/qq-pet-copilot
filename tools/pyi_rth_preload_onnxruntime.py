"""Preload onnxruntime before PyQt6 runtime hook.

参考 qq-farm-copilot：PyInstaller 内置的 pyi_rth_pyqt6 hook 会先 import
PyQt6.QtCore，某些 Windows 环境下可能导致之后 onnxruntime 初始化失败。
这里先加载 ORT，锁定兼容的 DLL 初始化顺序（打包时经 --runtime-hook 注入）。
"""

import sys


def _preload_onnxruntime() -> None:
    if sys.platform != 'win32':
        return
    import onnxruntime  # noqa: F401


_preload_onnxruntime()
del _preload_onnxruntime
