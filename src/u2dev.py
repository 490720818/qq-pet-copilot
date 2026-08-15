"""uiautomator2 设备封装：连接、截图、点击/滑动、不抬手拖动。

取代原 adb screencap + input tap/swipe/motionevent 的操控层：
- 截图：d.screenshot()（PIL 图 -> numpy RGB，供 OCR）
- 点击：d.touch.down/up（injectInputEvent 触摸事件，坐标为当前分辨率物理像素；
  不用 d.click——UiDevice.click 走 JSON-RPC，模拟器上偶发失效）
- 滑动/拖动：d.swipe（坐标为当前分辨率物理像素）
- 按住不松手拖动（洗澡搓洗）：d.touch.down/move/up
- 控件定位：d(**selector)（原生控件树可及的范围）

连接约定：先用配置的 adb 路径确保 adb server 已启动（adbutils 走 5037 端口
的服务器协议，不会再去下载自己的 adb），再按序列号 u2.connect()。
首次连接 u2 会自动把 u2.jar 部署到 /data/local/tmp 并用 app_process 启动服务
（无界面守护进程，不安装 APK，应用列表里看不到入口是正常的）。
"""
from __future__ import annotations

import functools
import random
import socket
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from .adb.device import Device
from .config import APP_ROOT, load_config, resource_path
from .progress import log

# 相对坐标换算的参考分辨率（原 720x1280 模板时代的固定坐标以此为基准）
REF_SIZE = (720, 1280)

# 截图偶发失败（设备休眠/adb 抖动）时的重试参数，截图是只读操作可安全重试
SCREENSHOT_RETRIES = 3
SCREENSHOT_RETRY_INTERVAL = 3

# 点击按压时长（秒）：d.touch.down/up 之间保持按压，避免瞬时 tap 不被
# 部分控件/游戏识别（原 d.click 内部自带按压时长）
CLICK_PRESS_SECONDS = 0.05

# u2 连接自愈：u2 server（/data/local/tmp/u2.jar 的 app_process）被系统杀/断开时，
# 连接类异常会先重连一次（u2.connect() 会自动重新拉起 u2.jar），不再直接升级到
# 整机重启。重连冷却（秒）：设备真离线时避免连续操作里反复重连刷屏。
RECONNECT_COOLDOWN = 5.0
# minitouch 会话断开后的重建冷却（秒）：socket 断了别在每次事件里反复重建
MINITOUCH_RECONNECT_COOLDOWN = 5.0

# minitouch（openstf）控制方案的资源与端口：
# 二进制放 resources/minitouch/minitouch-<abi>（不入库，tools/fetch_minitouch.py 拉取）
MINITOUCH_REL = Path('resources') / 'minitouch'
# minitouch 监听 Android abstract unix socket（默认名 minitouch，见 minitouch -n），
# 不是 TCP 端口：adb forward 用 localabstract:<名> 转发到本地端口再 socket 直连
MINITOUCH_SOCKET_NAME = 'minitouch'
# 本地 adb forward 端口：1111 等低端口常被系统占用（Windows bind 10013）。
# 学习 ALAS：优先复用设备上已有的 localabstract:minitouch forward（forward --list），
# 没有才在高端口区间随机新建（bind 失败换端口重试），避免端口冲突与 forward 堆积。
MINITOUCH_FORWARD_PORT_RANGE = (20000, 21000)
MINITOUCH_FORWARD_ATTEMPTS = 8
# 设备 ABI -> minitouch 二进制文件名后缀（与 fetch 脚本一致）
MINITOUCH_ABI_MAP = {'arm64-v8a': 'arm64-v8a', 'x86_64': 'x86_64'}


class MinitouchUnavailableError(RuntimeError):
    """minitouch 在当前设备上不可用（非 root / SELinux 拒绝打开 /dev/input/event* 等）。

    调用方（U2Device 各操控方法）捕获后自动回退到 injectInputEvent 控制方式，
    并把 config.yaml 的 control.method 写回 injectInputEvent。
    """


class MinitouchOccupiedError(RuntimeError):
    """minitouch 已被其他连接占用（如 atx-agent /minitouch WebSocket），单连接限制。"""


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


def _u2_retry(fn):
    """装饰器：u2 依赖操作连接类异常时重连一次并整体重试一次。

    只处理连接层故障（u2 server 进程没了/socket 断开/超时/未就绪）；其余异常
    原样抛给上层分级重试/恢复链路。重连带 RECONNECT_COOLDOWN 冷却，设备真离线
    时不会在连续操作里反复重连刷屏。
    """

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception as e:
            if not self._is_conn_error(e):
                raise
            if not self._reconnect():
                raise
            log(f'u2 已重连，重试 {fn.__name__}()...')
            return fn(self, *args, **kwargs)

    return wrapper


class U2Device:
    """各场景共享的 u2 连接，同时保留 adb Device 做连接管理/属性读取。"""

    def __init__(self, adb_path: str, serial: str = ""):
        self.adb = Device(adb_path, serial)
        resolved = self.adb.ensure_connected()
        log(f'设备在线: {resolved}，正在连接 uiautomator2（首次会自动部署 u2.jar 到 /data/local/tmp）...')
        import uiautomator2 as u2  # 重依赖，用到才加载
        _patch_u2_resource_lookup(u2)  # frozen 下随包资源（u2.jar）读取回退

        self.d = u2.connect(resolved)
        w, h = self.d.window_size()
        log(f'uiautomator2 已连接，当前分辨率 {w}x{h}')
        # 控制方案：injectInputEvent（默认）/ minitouch（openstf socket 直发）。
        # 运行中可在设置页改，调度器 reload_config 会更新本属性（见 runner.reload_config）
        self.control_method = getattr(load_config().control, 'method', 'injectInputEvent')
        self._mt = None  # minitouch 会话懒加载（首次 minitouch 点击时建立）
        self._last_reconnect_at = 0.0  # 上次 u2 重连时间（monotonic），配合 RECONNECT_COOLDOWN

    # ---- 感知 ----

    def screenshot(self) -> np.ndarray:
        """截图，返回 numpy RGB 数组（供 OCR）。失败自动重试。"""
        for attempt in range(1, SCREENSHOT_RETRIES + 1):
            try:
                img = self._u2_op(self.d.screenshot)
                return np.asarray(img.convert('RGB'))
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

    # ---- u2 连接自愈 ----

    def _is_conn_error(self, e: Exception) -> bool:
        """判断异常是否属于 u2 连接层故障（服务进程没了/断开/超时/未就绪）。

        命中才值得重连自愈；其余异常（页面找不到、OCR 识别失败等）原样抛给
        上层重试/恢复链路。
        """
        # minitouch 自己的故障（不可用/被占用/会话断开）由 minitouch 会话自愈或
        # 自动回退处理，不应误判为 u2 连接故障去重连 u2 server
        if isinstance(e, (MinitouchUnavailableError, MinitouchOccupiedError)):
            return False
        try:
            from uiautomator2 import exceptions as u2exc
            if isinstance(e, (u2exc.HTTPError, u2exc.ConnectError,
                              u2exc.UiAutomationNotConnectedError,
                              u2exc.LaunchUiAutomationError)):
                return True
        except Exception:
            pass
        msg = str(e).lower()
        return any(k in msg for k in (
            'unable to connect', 'connection', 'broken pipe',
            'reset by peer', 'refused', 'timeout', 'not ready',
            'socket',
        ))

    def _reconnect(self) -> bool:
        """重连 uiautomator2：重新 u2.connect()（会自动重新拉起 u2.jar），
        并重置 minitouch 会话（下次用时重建）。

        设备/模拟器真离线时重连会失败，返回 False，由调用方走既有恢复链路
        （场景重试 -> recover() 重启设备）。带冷却防止反复重连刷屏。
        """
        now = time.monotonic()
        if now - self._last_reconnect_at < RECONNECT_COOLDOWN:
            return False
        self._last_reconnect_at = now
        try:
            import uiautomator2 as u2
            _patch_u2_resource_lookup(u2)  # frozen 下随包资源读取回退同样要生效
            resolved = self.adb.ensure_connected()
            log(f'u2 连接异常，尝试重新连接 {resolved}...')
            self.d = u2.connect(resolved)
            self._mt = None  # minitouch 会话下次用时重建
            log('u2 重新连接成功')
            return True
        except Exception as e:
            log(f'u2 重新连接失败: {e}')
            return False

    def _u2_op(self, fn, *args, **kwargs):
        """执行依赖 u2 server 的调用；连接类异常时重连一次并重试一次。"""
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if not self._is_conn_error(e):
                raise
            if not self._reconnect():
                raise
            return fn(*args, **kwargs)

    # ---- 控件定位 ----

    @_u2_retry
    def find_ui(self, selector: dict) -> tuple[int, int] | None:
        """按 u2 选择器找控件，命中返回中心坐标 (x, y)，否则 None。"""
        ui = self.d(**selector)
        if not ui.exists:
            return None
        x, y = ui.center()
        return int(x), int(y)

    @_u2_retry
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

    @_u2_retry
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

    @_u2_retry
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

            return PageSource.parse(self._u2_op(self.d.dump_hierarchy))
        except Exception as e:
            log(f'控件树快照失败，xpath 将逐个实时查询: {e}')
            return None

    # ---- 操控 ----

    # ---- 控制方案分派：injectInputEvent（d.touch）/ minitouch（socket 直发） ----

    def _fallback_control_method(self, reason: Exception) -> None:
        """minitouch 当前设备不可用（非 root/SELinux 权限拒绝等）时回退 injectInputEvent。

        同时把 config.yaml 的 control.method 写回 injectInputEvent，下次启动/热加载
        也走默认方案；写回失败只影响下次启动，本次已生效。
        """
        log(f'minitouch 不可用（{reason}），自动回退到 injectInputEvent 控制方式')
        self.control_method = 'injectInputEvent'
        self._mt = None
        try:
            from .settings import load_raw, save_raw, set_value
            data = load_raw()
            set_value(data, 'control.method', 'injectInputEvent')
            save_raw(data)
            log('已把控制方案写回 config.yaml: control.method = injectInputEvent')
        except Exception as e:  # noqa: BLE001 - 写配置失败不阻断本次回退
            log(f'写回 config.yaml 失败（{e}），本次运行已回退，'
                f'下次启动请手动把控制方案改为 injectInputEvent')

    def _minitouch(self) -> "MiniTouchSession":
        """懒加载 minitouch 会话（首次 minitouch 操作时部署并连接）。"""
        if self._mt is None:
            self._mt = MiniTouchSession(self)
        return self._mt

    @_u2_retry
    def click(self, x: int, y: int) -> None:
        """点击：按 control.method 分派。

        injectInputEvent（默认）：d.touch.down/up，直接注入 DOWN/UP 触摸事件，
        不用 d.click（UiDevice.click 走 JSON-RPC，模拟器上偶发失效——日志表现为
        坐标正确但页面无反应）；minitouch：openstf minitouch socket 本地直发。
        down 与 up 之间保持 CLICK_PRESS_SECONDS 按压时长。
        """
        if self.control_method == 'minitouch':
            try:
                self._minitouch().click(x, y)
                return
            except MinitouchUnavailableError as e:
                self._fallback_control_method(e)
        self.d.touch.down(x, y)
        time.sleep(CLICK_PRESS_SECONDS)
        self.d.touch.up(x, y)

    @_u2_retry
    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> None:
        self.d.swipe(x1, y1, x2, y2, duration)

    @_u2_retry
    def drag(self, x1: int, y1: int, x2: int, y2: int,
             duration: float = 0.1) -> None:
        """慢速拖动（选择框归位用）。

        0.1-1.2 秒均可翻页，0.1 秒墙钟约 0.8s 最快；取 0.1 秒，若偶发不翻页再调大）。
        """
        self.d.swipe(x1, y1, x2, y2, duration)

    @_u2_retry
    def touch_down(self, x: int, y: int) -> None:
        if self.control_method == 'minitouch':
            try:
                self._minitouch().down(x, y)
                return
            except MinitouchUnavailableError as e:
                self._fallback_control_method(e)
        self.d.touch.down(x, y)

    @_u2_retry
    def touch_move(self, x: int, y: int) -> None:
        if self.control_method == 'minitouch':
            try:
                self._minitouch().move(x, y)
                return
            except MinitouchUnavailableError as e:
                self._fallback_control_method(e)
        self.d.touch.move(x, y)

    @_u2_retry
    def touch_up(self, x: int, y: int) -> None:
        if self.control_method == 'minitouch':
            try:
                self._minitouch().up()
                return
            except MinitouchUnavailableError as e:
                self._fallback_control_method(e)
        self.d.touch.up(x, y)


class MiniTouchSession:
    """openstf minitouch 触摸会话：部署二进制 + adb forward + socket 直发事件。

    minitouch 是 Android 底层触摸注入工具，socket 收命令（d/m/u + c commit 生效），
    事件在设备侧本地直发，比 uiautomator2 d.click（JSON-RPC）在模拟器上更可靠。
    坐标按设备屏幕分辨率等比换算（minitouch 的 max_x/max_y 是物理像素）。
    """

    def __init__(self, dev: U2Device):
        self.dev = dev
        self._sock: socket.socket | None = None
        self.max_x = 0
        self.max_y = 0
        self._last_connect_at = 0.0  # 上次建连时间（monotonic），配合重建冷却
        self._connect()

    # ---- 部署与连接 ----

    def _adb(self, *args: str, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
        adb = self.dev.adb
        cmd = [adb.adb]
        if adb.serial:
            cmd += ['-s', adb.serial]
        cmd += list(args)
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=timeout,
                              creationflags=subprocess.CREATE_NO_WINDOW)
        if check and proc.returncode != 0:
            raise RuntimeError(f'adb 命令失败: {" ".join(cmd)}: {(proc.stderr or proc.stdout).strip()}')
        return proc

    def _device_abi(self) -> str:
        abi = self._adb('shell', 'getprop', 'ro.product.cpu.abi').stdout.strip()
        if abi not in MINITOUCH_ABI_MAP:
            raise RuntimeError(f'控制方案 minitouch 暂不支持设备架构: {abi or "未知"}'
                               f'（支持: {"/".join(MINITOUCH_ABI_MAP)}）')
        return abi

    def _ensure_binary(self, abi: str) -> Path:
        rel = MINITOUCH_REL / f'minitouch-{abi}'
        path = resource_path(rel)
        if not path.is_file():
            # 缺失时自动调用 fetch 脚本下载（失败不阻塞，给出明确提示）
            fetch = APP_ROOT / 'tools' / 'fetch_minitouch.py'
            if fetch.is_file():
                log(f'未找到 minitouch 二进制，正在下载（tools/fetch_minitouch.py --arch {abi}）...')
                subprocess.run([sys.executable, str(fetch), '--arch', abi], check=False, timeout=300)
            path = resource_path(rel)
        if not path.is_file():
            raise RuntimeError(
                f'缺少 minitouch 二进制: {path}。请手动运行 '
                f'python tools/fetch_minitouch.py --arch {abi} 或放到 resources/minitouch/')
        return path

    def _start_server(self, adb_bin: str, serial: str) -> None:
        """启动 minitouch（普通 shell 优先，失败用 su）；重复调用无副作用。

        启动失败时读启动日志分类：SELinux/权限拒绝（打不开 /dev/input/event*，
        常见于非 root 真机）抛 MinitouchUnavailableError，其余抛 RuntimeError。
        """
        proc = self._adb('shell', 'pidof', 'minitouch', check=False)
        if proc.stdout.strip():
            return
        details = []
        for prefix in ('', "su -c '"):
            cmd = "nohup /data/local/tmp/minitouch >/data/local/tmp/minitouch.log 2>&1 &"
            full = f"{prefix}{cmd}{"'" if prefix else ''}"
            self._adb('shell', full, check=False, timeout=15)
            time.sleep(0.5)
            proc = self._adb('shell', 'pidof', 'minitouch', check=False)
            if proc.stdout.strip():
                return
            out = self._adb('shell', 'cat', '/data/local/tmp/minitouch.log', check=False)
            details.append(f'[{prefix or "shell"}] {(out.stdout or out.stderr).strip()}')
        detail = '\n'.join(details)
        lower = detail.lower()
        if any(mark in lower for mark in (
                'permission denied', 'unable to open device', 'no suitable touch device')):
            raise MinitouchUnavailableError(
                '无法打开 /dev/input/event* 触摸设备（非 root / SELinux 权限拒绝），'
                f'minitouch 不可用。日志: {detail[:300]}')
        raise RuntimeError(f'minitouch 启动失败（普通 shell 与 su 均未检测到进程）: {detail[:300]}')

    def _adb_forward_list(self) -> list[tuple[str, str]]:
        """解析 adb forward --list，返回本设备的 [(local, remote)]。"""
        proc = self._adb('forward', '--list', check=False, timeout=15)
        out = []
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and (not self.dev.adb.serial or parts[0] == self.dev.adb.serial):
                out.append((parts[1], parts[2]))
        return out

    def _reuse_or_create_forward(self) -> int | None:
        """复用本设备已有的 localabstract:minitouch forward，否则随机高端口新建。

        学习 ALAS：先 forward_list() 复用（避免 forward 堆积和 Windows bind 10013），
        冗余的转发顺带清掉；新建在 MINITOUCH_FORWARD_PORT_RANGE 随机选端口，
        adb forward 失败（端口被占）换端口重试。返回本地端口，全部失败返回 None。
        """
        target_remote = f'localabstract:{MINITOUCH_SOCKET_NAME}'
        existing = [local for local, remote in self._adb_forward_list() if remote == target_remote]
        if existing:
            for local in existing[1:]:
                self._adb('forward', '--remove', local, check=False, timeout=15)
            port = int(existing[0].split(':')[1])
            log(f'复用 minitouch forward: {existing[0]}')
            return port
        last_err: Exception | None = None
        for _ in range(MINITOUCH_FORWARD_ATTEMPTS):
            port = random.randint(*MINITOUCH_FORWARD_PORT_RANGE)
            local = f'tcp:{port}'
            proc = self._adb('forward', local, target_remote, check=False, timeout=15)
            if proc.returncode != 0:
                last_err = RuntimeError(
                    f'{local} forward 失败: {(proc.stderr or proc.stdout).strip()}')
                continue
            return port
        if last_err is not None:
            log(f'minitouch forward 新建失败: {last_err}')
        return None

    def _connect(self) -> None:
        self._last_connect_at = time.monotonic()
        dev = self.dev
        abi = self._device_abi()
        binary = self._ensure_binary(abi)
        # 推送并加可执行权限
        self._adb('push', str(binary), '/data/local/tmp/minitouch')
        self._adb('shell', 'chmod', '755', '/data/local/tmp/minitouch')
        self._start_server(dev.adb.adb, dev.adb.serial)
        # adb forward minitouch 的 abstract socket 到本地端口，socket 直连。
        # 学习 ALAS：优先复用已有 forward（forward --list），新建用随机高端口，
        # 避免 Windows 低端口 bind 10013；连接不上说明是残留 forward，删掉重建。
        last_err: Exception | None = None
        sock = None
        for _ in range(MINITOUCH_FORWARD_ATTEMPTS):
            local_port = self._reuse_or_create_forward()
            if local_port is None:
                break
            try:
                sock = socket.create_connection(('127.0.0.1', local_port), timeout=5)
                break
            except socket.timeout:
                raise MinitouchOccupiedError(
                    '连接 minitouch 超时，可能已被其他连接占用（如 atx-agent /minitouch '
                    'WebSocket，minitouch 只允许一个客户端）') from None
            except OSError as e:  # noqa: BLE001 - 残留 forward，删掉重建
                last_err = e
                self._adb('forward', '--remove', f'tcp:{local_port}', check=False, timeout=15)
                sock = None
        if sock is None:
            raise RuntimeError(f'minitouch forward/socket 连接失败: {last_err}')
        try:
            # minitouch 连接后发三行：v <版本> / ^ <max_contacts> <max_x> <max_y> <max_pressure> / $ <pid>
            f = sock.makefile('rb')
            f.readline()  # v 1
            line = f.readline()  # ^ 10 720 1280 0
            parts = line.decode('utf-8', 'replace').strip().split()
            if len(parts) >= 5 and parts[0] == '^':
                self.max_x = int(parts[2])
                self.max_y = int(parts[3])
            self._sock = sock
            log(f'minitouch 已连接（{abi}，max {self.max_x}x{self.max_y}）')
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f'minitouch 握手失败: {e}') from e

    # ---- 事件 ----

    def _send(self, line: str) -> None:
        if self._sock is None:
            # 会话断开后自动重建（重新 push/启动/forward/socket）；带冷却防反复
            now = time.monotonic()
            if now - self._last_connect_at < MINITOUCH_RECONNECT_COOLDOWN:
                raise RuntimeError('minitouch 未连接')
            log('minitouch 会话已断开，重新建立...')
            self._connect()
        try:
            self._sock.sendall((line + '\n').encode('utf-8'))
        except OSError as e:
            self._sock = None
            raise RuntimeError(f'minitouch 连接已断开: {e}') from e

    def _scale(self, x: int, y: int) -> tuple[int, int]:
        w, h = self.dev.window_size()
        sx = int(x * self.max_x / w) if self.max_x else int(x)
        sy = int(y * self.max_y / h) if self.max_y else int(y)
        return sx, sy

    def click(self, x: int, y: int) -> None:
        sx, sy = self._scale(x, y)
        self._send(f'd 0 {sx} {sy} 50')
        self._send('c')
        time.sleep(CLICK_PRESS_SECONDS)
        self._send('u 0')
        self._send('c')

    def down(self, x: int, y: int) -> None:
        sx, sy = self._scale(x, y)
        self._send(f'd 0 {sx} {sy} 50')
        self._send('c')

    def move(self, x: int, y: int) -> None:
        sx, sy = self._scale(x, y)
        self._send(f'm 0 {sx} {sy} 50')
        self._send('c')

    def up(self) -> None:
        self._send('u 0')
        self._send('c')
