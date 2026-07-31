"""截图/模板采集工具（adb 版）。

参考 qq-farm-copilot 的 template_collector，截图来源改为物理机 adb screencap。

用法：
    python tools/screenshot_tool.py            # 交互模式
    python tools/screenshot_tool.py --once     # 只截一张保存到 screenshots/ 后退出

交互操作：
    鼠标左键拖拽        框选区域（显示原图坐标与尺寸）
    r                   重新截图
    s                   保存当前全屏截图到 screenshots/
    c                   把框选区域裁剪保存到 templates/（终端输入模板名）
    q / Esc             退出
"""

import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from src.adb.device import Device
from src.config import find_adb, load_config

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCREENSHOTS_DIR = os.path.join(PROJECT_ROOT, 'screenshots')
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, 'templates')

# 显示窗口的最大尺寸（适配桌面）
MAX_DISPLAY_WIDTH = 720
MAX_DISPLAY_HEIGHT = 1400

WINDOW_NAME = 'ADB Screenshot Tool'


class ScreenshotTool:
    """adb 截图 + 交互式框选裁剪工具。"""

    def __init__(self):
        cfg = load_config()
        self.device = Device(find_adb(cfg.adb.path), cfg.adb.device_serial)
        serial = self.device.ensure_connected()
        print(f'设备在线: {serial}  分辨率: {self.device.screen_size()}')

        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        os.makedirs(TEMPLATES_DIR, exist_ok=True)

        self._original: np.ndarray | None = None  # 原始截图（全分辨率 BGR）
        self._display: np.ndarray | None = None   # 缩放后的显示图
        self._scale = 1.0
        self._drawing = False
        self._start: tuple[int, int] | None = None  # 显示坐标
        self._end: tuple[int, int] | None = None

    # ---- 截图 ----

    def capture(self) -> np.ndarray | None:
        """adb 截图并转为 OpenCV BGR 图。"""
        png = self.device.screenshot()
        data = np.frombuffer(png, dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if image is None:
            print('截图解码失败')
        return image

    def capture_once(self) -> str:
        """截一张保存到 screenshots/，返回文件路径。"""
        image = self.capture()
        if image is None:
            raise RuntimeError('截图失败')
        path = self._make_path(SCREENSHOTS_DIR, 'screen')
        self._imwrite(path, image)
        print(f'已保存: {path}  ({image.shape[1]}x{image.shape[0]})')
        return path

    # ---- 显示与坐标换算 ----

    def _resize_for_display(self, image: np.ndarray) -> np.ndarray:
        h, w = image.shape[:2]
        scale = min(MAX_DISPLAY_WIDTH / w, MAX_DISPLAY_HEIGHT / h, 1.0)
        self._scale = scale
        if scale < 1.0:
            return cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        return image.copy()

    def _to_original(self, x: int, y: int) -> tuple[int, int]:
        """显示坐标 -> 原图坐标（带边界夹紧）。"""
        h, w = self._original.shape[:2]
        return (
            max(0, min(int(x / self._scale), w - 1)),
            max(0, min(int(y / self._scale), h - 1)),
        )

    def _selection_rect(self) -> tuple[int, int, int, int] | None:
        """当前框选区域的原图坐标 (x1, y1, x2, y2)，无框选返回 None。"""
        if not (self._start and self._end):
            return None
        ox1, oy1 = self._to_original(*self._start)
        ox2, oy2 = self._to_original(*self._end)
        x1, y1, x2, y2 = min(ox1, ox2), min(oy1, oy2), max(ox1, ox2), max(oy1, oy2)
        if x2 - x1 < 3 or y2 - y1 < 3:
            return None
        return x1, y1, x2, y2

    def _render(self) -> np.ndarray:
        img = self._display.copy()
        if self._start and self._end:
            cv2.rectangle(img, self._start, self._end, (0, 255, 0), 1)
            rect = self._selection_rect()
            if rect:
                x1, y1, x2, y2 = rect
                label = f'({x1},{y1})->({x2},{y2}) {x2 - x1}x{y2 - y1}'
                lx = max(4, min(self._start[0], self._end[0]))
                ly = max(14, min(self._start[1], self._end[1]) - 6)
                cv2.putText(img, label, (lx, ly), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1, cv2.LINE_AA)
        return img

    def _mouse(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            self._drawing = True
            self._start = self._end = (x, y)
        elif event == cv2.EVENT_MOUSEMOVE and self._drawing:
            h, w = self._display.shape[:2]
            self._end = (max(0, min(x, w - 1)), max(0, min(y, h - 1)))
        elif event == cv2.EVENT_LBUTTONUP:
            self._drawing = False
            h, w = self._display.shape[:2]
            self._end = (max(0, min(x, w - 1)), max(0, min(y, h - 1)))

    # ---- 保存 ----

    @staticmethod
    def _make_path(directory: str, prefix: str) -> str:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        ms = int((time.time() % 1) * 1000)
        return os.path.join(directory, f'{prefix}_{ts}_{ms:03d}.png')

    @staticmethod
    def _imwrite(path: str, image: np.ndarray) -> None:
        """cv2.imwrite 不支持中文路径，统一走 imencode。"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ok, buf = cv2.imencode('.png', image)
        if not ok:
            raise RuntimeError('图像编码失败')
        buf.tofile(path)

    def _refresh(self):
        image = self.capture()
        if image is not None:
            self._original = image
            self._display = self._resize_for_display(image)
            self._start = self._end = None
            print(f'截屏完成 ({image.shape[1]}x{image.shape[0]})')

    # ---- 主循环 ----

    def run(self):
        print('=' * 46)
        print('  adb 截图/模板采集工具')
        print('=' * 46)
        print('  拖拽框选 | r 重截 | s 存全屏 | c 存框选模板 | q 退出')
        print()

        self._refresh()
        if self._original is None:
            return

        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(WINDOW_NAME, self._display.shape[1], self._display.shape[0])
        cv2.setMouseCallback(WINDOW_NAME, self._mouse)

        while True:
            cv2.imshow(WINDOW_NAME, self._render())
            key = cv2.waitKey(30) & 0xFF
            if cv2.getWindowProperty(WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
                break
            if key in (ord('q'), 27):
                break
            if key == ord('r'):
                print('重新截屏...')
                self._refresh()
            elif key == ord('s'):
                path = self._make_path(SCREENSHOTS_DIR, 'screen')
                self._imwrite(path, self._original)
                print(f'已保存截图: {path}')
            elif key == ord('c'):
                rect = self._selection_rect()
                if not rect:
                    print('请先框选一个区域')
                    continue
                x1, y1, x2, y2 = rect
                name = input(f'模板名（区域 {rect}，直接回车用时间戳）: ').strip()
                if not name:
                    name = f'tpl_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
                path = os.path.join(TEMPLATES_DIR, f'{name}.png')
                self._imwrite(path, self._original[y1:y2, x1:x2])
                print(f'已保存模板: {path}  ({x2 - x1}x{y2 - y1})')

        cv2.destroyAllWindows()


if __name__ == '__main__':
    tool = ScreenshotTool()
    if '--once' in sys.argv:
        tool.capture_once()
    else:
        tool.run()
