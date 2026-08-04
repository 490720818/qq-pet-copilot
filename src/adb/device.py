"""adb 设备封装：设备检测与连接管理、屏幕属性读取、adb 命令管道。

画面截图与点击/滑动等操控已改由 uiautomator2 负责（见 src/u2dev.py），
这里只保留 u2 连接前的 adb server/设备在线管理，以及 main.py
嵌入 scrcpy 时需要的屏幕宽高比读取。
"""
from __future__ import annotations

import re
import subprocess
import sys

# Windows 下隐藏 adb 子进程的命令行窗口（exe 无控制台模式下每次调用都会闪窗）
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# 模拟器 ro.hardware 的已知取值（真机是 qcom/mtXXXX 等平台名，不会是这些）
_EMU_HARDWARE = ("goldfish", "ranchu", "vbox86", "vbox86p", "nox", "ttvm_hdragon", "houdini")
# 模拟器 ro.product.model 中的特征词
_EMU_MODEL_WORDS = ("sdk", "emulator", "google_sdk", "droid4x", "nox",
                    "mumu", "ldplayer", "bluestacks", "memu")
# 模拟器 ro.product.manufacturer 的已知取值
_EMU_MANUFACTURERS = ("genymotion",)


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
        """确认 adb server 已启动且有设备在线；未指定序列号时选中第一台。返回序列号。"""
        self._run("start-server")
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

    def is_emulator(self) -> bool:
        """根据 getprop 的具体键值判断是否为模拟器。

        不能在整个 getprop 输出里做子串匹配：真机也可能带
        ro.kernel.qemu.gles=0 这类属性（高通内核残留，值为 0 表示非 qemu），
        必须按键解析、按值/已知取值判断。
        """
        out = self._run("shell", "getprop").stdout.decode("utf-8", "replace").lower()
        props = dict(re.findall(r"^\[(.+?)\]: \[(.*)\]$", out, re.M))
        # qemu 标志位必须为 1 才算（值为 0 是"支持但未启用"）
        if props.get("ro.kernel.qemu") == "1" or props.get("qemu.hw.mainkeys") == "1":
            return True
        if "emulator" in props.get("ro.build.characteristics", ""):
            return True
        if props.get("ro.hardware") in _EMU_HARDWARE:
            return True
        if props.get("ro.product.board") == "goldfish":
            return True
        if props.get("ro.product.manufacturer") in _EMU_MANUFACTURERS:
            return True
        model = props.get("ro.product.model", "")
        return any(w in model for w in _EMU_MODEL_WORDS)
