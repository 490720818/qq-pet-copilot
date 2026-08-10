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

# uiautomator2 随包资源（u2.jar 等）的 frozen 回退补丁是否已打
_u2_resource_patched = False


def _patch_u2_resource_lookup(u2) -> None:
    """修复 PyInstaller frozen 下 uiautomator2 读不到 assets/*（u2.jar 等）的问题。

    frozen 时 importlib.resources.files("uiautomator2") 拿不到 _MEI 解压目录里的
    数据文件，导致 uiautomator server 需要重部署时必然报
    "Resource assets/u2.jar not found in uiautomator2 package."。
    这里把 with_package_resource 换成优先从 sys._MEIPASS/uiautomator2/ 与
    包目录查找的版本，并同步到按值引入该函数的子模块
    （uiautomator2.core / uiautomator2._input 等）。
    """
    global _u2_resource_patched
    if _u2_resource_patched:
        return
    _u2_resource_patched = True

    import contextlib
    import sys as _sys
    from pathlib import Path

    import uiautomator2.utils as u2_utils

    original = u2_utils.with_package_resource

    @contextlib.contextmanager
    def patched(filename: str):
        # 1) frozen 解压目录里的随包资源
        meipass = getattr(_sys, '_MEIPASS', None)
        if meipass:
            candidate = Path(meipass) / 'uiautomator2' / filename
            if candidate.is_file():
                yield candidate
                return
        # 2) 包内真实目录（开发环境；frozen 下与 _MEIPASS 同目录）
        pkg_dir = Path(u2.__file__).resolve().parent
        candidate = pkg_dir / filename
        if candidate.is_file():
            yield candidate
            return
        # 3) 兜底：原逻辑（importlib.resources / sys.argv[0] 旁 / cwd）
        with original(filename) as f:
            yield f

    u2_utils.with_package_resource = patched
    # 同步到按值引入该函数的子模块（uiautomator2.core、uiautomator2._input 等）
    for mod_name, mod in list(_sys.modules.items()):
        if (mod_name.startswith('uiautomator2')
                and getattr(mod, 'with_package_resource', None) is original):
            mod.with_package_resource = patched


class U2Device:
    """各场景共享的 u2 连接，同时保留 adb Device 做连接管理/属性读取。"""

    def __init__(
        self,
        adb_path: str,
        serial: str = "",
        auto_wifi_failover: bool | None = None,
        wifi_port: int | None = None,
    ):
        self.adb = Device(adb_path, serial, auto_wifi_failover=auto_wifi_failover, wifi_port=wifi_port)
        resolved = self.adb.ensure_connected()
        log(f'设备在线: {resolved}，正在连接 uiautomator2（首次需安装 atx-agent）...')
        import uiautomator2 as u2  # 重依赖，用到才加载
        _patch_u2_resource_lookup(u2)  # frozen 下随包资源（u2.jar）读取回退

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

    def find_xpath_all(self, path: str, source=None) -> list[tuple[int, int]]:
        """按 XPath 找所有匹配控件，返回中心坐标列表（按从上到下、从左到右排序）。"""
        if source is None:
            els = self.d.xpath(path).all()
        else:
            els = source.find_elements(path)
        centers = []
        for e in els:
            left, top, right, bottom = e.bounds
            centers.append((int((left + right) / 2), int((top + bottom) / 2)))
        centers.sort(key=lambda c: (c[1], c[0]))
        return centers

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
             duration: float = 0.1) -> None:
        """慢速拖动（选择框归位用）。

        0.1-1.2 秒均可翻页，0.1 秒墙钟约 0.8s 最快；取 0.1 秒，若偶发不翻页再调大）。
        """
        self.d.swipe(x1, y1, x2, y2, duration)

    def touch_down(self, x: int, y: int) -> None:
        self.d.touch.down(x, y)

    def touch_move(self, x: int, y: int) -> None:
        self.d.touch.move(x, y)

    def touch_up(self, x: int, y: int) -> None:
        self.d.touch.up(x, y)
