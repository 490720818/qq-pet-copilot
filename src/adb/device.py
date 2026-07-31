"""adb 设备封装：设备检测、截图、点击/滑动/输入。"""
from __future__ import annotations

import subprocess
import time


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
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        if check and proc.returncode != 0:
            raise AdbError(
                f"adb 命令失败: {' '.join(cmd)}\n{proc.stderr.decode('utf-8', 'replace')}"
            )
        return proc

    # ---- 设备状态 ----

    def online_devices(self) -> list[str]:
        """返回在线设备序列号列表（不依赖 self.serial）。"""
        proc = subprocess.run(
            [self.adb, "devices"], capture_output=True, timeout=30, check=True
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
