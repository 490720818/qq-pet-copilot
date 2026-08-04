"""uiautomator2 设备封装：连接、截图、点击/滑动、不抬手拖动。

取代原 adb screencap + input tap/swipe/motionevent 的操控层：
- 截图：d.screenshot()（PIL 图 -> numpy RGB，供 OCR）
- 点击/滑动：d.click / d.swipe（坐标为当前分辨率物理像素）
- 按住不松手拖动（洗澡搓洗）：d.touch.down/move/up（minitouch）
- 控件定位：d(**selector)（原生控件树可及的范围）

连接约定：先用配置的 adb 路径确保 adb server 已启动（adbutils 走 5037 端口
的服务器协议，不会再去下载自己的 adb），再按序列号 u2.connect()。
首次连接 u2 会自动往设备安装 atx-agent / uiautomator-server（手机上需允许安装）。
"""
from __future__ import annotations

import time

import numpy as np

from .adb.device import Device
from .progress import log

# 相对坐标换算的参考分辨率（原 720x1280 模板时代的固定坐标以此为基准）
REF_SIZE = (720, 1280)

# 截图偶发失败（设备休眠/adb 抖动）时的重试参数，截图是只读操作可安全重试
SCREENSHOT_RETRIES = 3
SCREENSHOT_RETRY_INTERVAL = 3


class U2Device:
    """各场景共享的 u2 连接，同时保留 adb Device 做连接管理/属性读取。"""

    def __init__(self, adb_path: str, serial: str = ""):
        self.adb = Device(adb_path, serial)
        resolved = self.adb.ensure_connected()
        log(f'设备在线: {resolved}，正在连接 uiautomator2（首次需安装 atx-agent）...')
        import uiautomator2 as u2  # 重依赖，用到才加载

        self.d = u2.connect(resolved)
        w, h = self.d.window_size()
        log(f'uiautomator2 已连接，当前分辨率 {w}x{h}')

    # ---- 感知 ----

    def screenshot(self) -> np.ndarray:
        """截图，返回 numpy RGB 数组（供 OCR）。失败自动重试。"""
        for attempt in range(1, SCREENSHOT_RETRIES + 1):
            try:
                return np.asarray(self.d.screenshot().convert('RGB'))
            except Exception as e:
                if attempt >= SCREENSHOT_RETRIES:
                    raise RuntimeError(
                        f'u2 截图失败（连续 {SCREENSHOT_RETRIES} 次），'
                        f'请检查设备连接: {e}') from None
                log(f'u2 截图失败，{SCREENSHOT_RETRY_INTERVAL}s 后重试 '
                    f'({attempt}/{SCREENSHOT_RETRIES}): {e}')
                time.sleep(SCREENSHOT_RETRY_INTERVAL)

    def window_size(self) -> tuple[int, int]:
        return self.d.window_size()

    def rel(self, x: int, y: int) -> tuple[int, int]:
        """把 720x1280 参考坐标按当前分辨率等比换算为实际像素。"""
        w, h = self.window_size()
        return round(x * w / REF_SIZE[0]), round(y * h / REF_SIZE[1])

    # ---- 控件定位 ----

    def find_ui(self, selector: dict) -> tuple[int, int] | None:
        """按 u2 选择器找控件，命中返回中心坐标 (x, y)，否则 None。"""
        ui = self.d(**selector)
        if not ui.exists:
            return None
        x, y = ui.center()
        return int(x), int(y)

    def find_xpath(self, path: str, source=None) -> tuple[int, int] | None:
        """按 XPath 找控件，命中返回中心坐标 (x, y)，否则 None。

        source: hierarchy() 抓的控件树快照；传入则本地查询（快），
        不传则 d.xpath 实时 dump（每次调用一次全树 dump，慢）。
        """
        bounds = self.find_xpath_bounds(path, source)
        if bounds is None:
            return None
        x1, y1, x2, y2 = bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    def find_xpath_bounds(self, path: str, source=None) -> tuple[int, int, int, int] | None:
        """按 XPath 找控件，命中返回范围 (x1, y1, x2, y2)，否则 None。"""
        if source is None:
            # all() 一次 dump 直接拿全部匹配；先 exists 再 get() 会各 dump 一次
            els = self.d.xpath(path).all()
            if not els:
                return None
            e = els[0]
        else:
            els = source.find_elements(path)
            if not els:
                return None
            e = els[0]
        left, top, right, bottom = e.bounds
        return int(left), int(top), int(right), int(bottom)

    def hierarchy(self):
        """抓一次当前控件树快照（dump 全树较慢，一轮检测应共享一个快照）。

        失败返回 None（调用方回退到逐个 d.xpath 实时查询）。
        """
        try:
            from uiautomator2.xpath import PageSource

            return PageSource.parse(self.d.dump_hierarchy())
        except Exception as e:
            log(f'控件树快照失败，xpath 将逐个实时查询: {e}')
            return None

    # ---- 操控 ----

    def click(self, x: int, y: int) -> None:
        self.d.click(x, y)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> None:
        self.d.swipe(x1, y1, x2, y2, duration)

    def drag(self, x1: int, y1: int, x2: int, y2: int,
             duration: float = 0.6) -> None:
        """慢速拖动（选择框归位用）。

        游戏 canvas 不识别快速拖动：minitouch 分段 move 和 u2 内置 d.drag
        实测都无效，只有带 duration 的慢速 swipe 能触发轮播滚动（真机验证
        0.6-1.2 秒均可，取 1.0 秒留余量）。
        """
        self.d.swipe(x1, y1, x2, y2, duration)

    def touch_down(self, x: int, y: int) -> None:
        self.d.touch.down(x, y)

    def touch_move(self, x: int, y: int) -> None:
        self.d.touch.move(x, y)

    def touch_up(self, x: int, y: int) -> None:
        self.d.touch.up(x, y)
