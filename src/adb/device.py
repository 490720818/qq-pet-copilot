"""adb 设备封装：设备检测、截图、点击/滑动/输入。"""
from __future__ import annotations

import subprocess
import sys
import time

from ..config import APP_ROOT

# Windows 下隐藏 adb 子进程的命令行窗口（exe 无控制台模式下每次调用都会闪窗）
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class AdbError(RuntimeError):
    pass


class Device:
    def __init__(self, adb_path: str, serial: str = ""):
        self.adb = adb_path
        self.serial = serial

    # ---- 基础命令 ----

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = [self.adb]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += list(args)
        proc = subprocess.run(cmd, capture_output=True, timeout=60,
                              creationflags=_NO_WINDOW)
        if check and proc.returncode != 0:
            raise AdbError(
                f"adb 命令失败: {' '.join(cmd)}\n{proc.stderr.decode('utf-8', 'replace')}"
            )
        return proc

    # ---- 设备状态 ----

    def online_devices(self) -> list[str]:
        """返回在线设备序列号列表（不依赖 self.serial）。"""
        proc = subprocess.run(
            [self.adb, "devices"], capture_output=True, timeout=30, check=True,
            creationflags=_NO_WINDOW,
        )
        serials = []
        for line in proc.stdout.decode("utf-8", "replace").splitlines()[1:]:
            parts = line.split()
            if len(parts) == 2 and parts[1] == "device":
                serials.append(parts[0])
        return serials

    def ensure_connected(self) -> str:
        """确认有设备在线；未指定序列号时选中第一台。返回序列号。"""
        devices = self.online_devices()
        if self.serial:
            if self.serial not in devices:
                raise AdbError(f"指定设备 {self.serial} 不在线，当前在线: {devices or '无'}")
            return self.serial
        if not devices:
            raise AdbError("没有在线的 adb 设备，请检查 USB 连接与调试授权。")
        self.serial = devices[0]
        return self.serial

    def screen_size(self) -> tuple[int, int]:
        out = self._run("shell", "wm", "size").stdout.decode("utf-8", "replace")
        # 形如 "Physical size: 1080x2400"
        size = out.strip().split(":")[-1].strip().split("x")
        return int(size[0]), int(size[1])

    def set_resolution(self, width: int, height: int, density: int | None = None) -> None:
        """修改分辨率（wm size），可同时设置密度（wm density）保持界面比例。"""
        self._run("shell", "wm", "size", f"{width}x{height}")
        if density:
            self._run("shell", "wm", "density", str(density))

    def reset_resolution(self) -> None:
        """恢复默认物理分辨率和密度。

        注意：部分 ROM 上 wm density reset 清不掉 override density，
        所以先 reset 再显式把密度设回物理值（解析失败也有 reset 兜底）。
        """
        self._run("shell", "wm", "size", "reset")
        self._run("shell", "wm", "density", "reset")
        try:
            physical = self.physical_density()
            self._run("shell", "wm", "density", str(physical))
        except AdbError:
            pass

    def physical_density(self) -> int:
        """物理密度（解析 'Physical density: 480' 行）。"""
        out = self._run("shell", "wm", "density").stdout.decode("utf-8", "replace")
        for line in out.splitlines():
            if "Physical density" in line:
                return int(line.split(":")[-1].strip())
        raise AdbError(f"无法解析物理密度: {out!r}")

    def density(self) -> int:
        """当前生效密度（有 override 取 override，否则物理值）。"""
        out = self._run("shell", "wm", "density").stdout.decode("utf-8", "replace")
        for line in out.splitlines():
            if "Override density" in line:
                return int(line.split(":")[-1].strip())
        return self.physical_density()

    def is_emulator(self) -> bool:
        """根据 getprop 关键字判断是否为模拟器。"""
        out = self._run("shell", "getprop").stdout.decode("utf-8", "replace").lower()
        return any(k in out for k in ("qemu", "emulator", "x86", "vbox", "goldfish", "nox"))

    # ---- 感知 ----

    def screenshot(self) -> bytes:
        """截图，直接返回 PNG 字节。"""
        return self._run("exec-out", "screencap", "-p").stdout

    # ---- 操控 ----

    def tap(self, x: int, y: int) -> None:
        self._validate_coords(x, y)
        self._run("shell", "input", "tap", str(x), str(y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 500) -> None:
        self._validate_coords(x1, y1)
        self._validate_coords(x2, y2)
        self._run(
            "shell", "input", "swipe",
            str(x1), str(y1), str(x2), str(y2), str(duration_ms),
        )

    def motion_event(self, action: str, x: int, y: int) -> None:
        """注入单个触摸事件（DOWN / MOVE / UP）。

        多次调用共用同一输入流，可实现"按住不松手"的连续拖动；
        注意 DOWN 之后必须 UP，否则触摸会一直挂着。
        """
        if action not in ("DOWN", "MOVE", "UP"):
            raise AdbError(f"非法 motionevent 动作: {action}")
        self._validate_coords(x, y)
        self._run("shell", "input", "motionevent", action, str(x), str(y))

    def motion_path(self, points: list[tuple[int, int]], step_sleep: float = 0.0) -> None:
        """一次 adb 调用连续注入一串 MOVE 事件（按住状态下的拖动路径）。

        逐个发 motionevent 每次都有 adb 进程开销，合并成一条 shell 命令后
        拖动会快很多；step_sleep 为每个 MOVE 之间的间隔（秒，设备端 sleep）。
        """
        for x, y in points:
            self._validate_coords(x, y)
        joiner = f"; sleep {step_sleep}; " if step_sleep else "; "
        cmd = joiner.join(f"input motionevent MOVE {x} {y}" for x, y in points)
        self._run("shell", cmd)

    def input_text(self, text: str) -> None:
        # adb shell input text 不支持空格和大部分 unicode，空格转 %s
        escaped = text.replace(" ", "%s")
        self._run("shell", "input", "text", escaped)

    def key_event(self, keycode: str) -> None:
        """如 BACK / HOME / ENTER。"""
        self._run("shell", "input", "keyevent", keycode)

    def _validate_coords(self, x: int, y: int) -> None:
        if x < 0 or y < 0 or x > 10000 or y > 10000:
            raise AdbError(f"非法坐标 ({x}, {y})")

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)


# 模板/坐标校准的目标分辨率（竖屏）
TARGET_SIZE = (720, 1280)

# 本程序修改过分辨率的标记文件（主进程/调度子进程共享，
# 只有本程序改过才恢复，避免清掉用户自己的 wm 覆盖）
_OVERRIDE_MARKER = APP_ROOT / 'runs' / 'resolution_override.json'


def setup_resolution(dev: Device) -> bool:
    """实机且分辨率不等于目标值时调整为目标分辨率，密度按比例一起调。返回是否做了修改。

    模拟器直接跳过（模拟器分辨率由窗口决定，改了也没意义）。
    """
    from ..progress import log

    if dev.is_emulator():
        log('检测到模拟器，跳过分辨率调整')
        return False
    w, h = dev.screen_size()
    if (w, h) == TARGET_SIZE:
        return False
    # 密度按物理值等比缩放（如 1080p@480 -> 720p@320），保持界面元素物理大小一致
    density = round(dev.physical_density() * TARGET_SIZE[0] / w)
    log(f'实机分辨率 {w}x{h}，调整为目标 {TARGET_SIZE[0]}x{TARGET_SIZE[1]}@{density}dpi'
        f'（退出时恢复）')
    dev.set_resolution(*TARGET_SIZE, density)
    _OVERRIDE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    _OVERRIDE_MARKER.write_text('{"changed": true}', encoding='utf-8')
    return True


def restore_resolution(dev: Device) -> None:
    """恢复默认物理分辨率（模拟器跳过；只有本程序改过才恢复）。"""
    from ..progress import log

    if dev.is_emulator():
        return
    if not _OVERRIDE_MARKER.is_file():
        return
    dev.reset_resolution()
    try:
        _OVERRIDE_MARKER.unlink()
    except OSError:
        pass
    log('已恢复手机分辨率')
