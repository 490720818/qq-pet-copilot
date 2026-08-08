"""adb 设备封装：设备检测与连接管理、屏幕属性读取、adb 命令管道。

画面截图与点击/滑动等操控已改由 uiautomator2 负责（见 src/u2dev.py），
这里只保留 u2 连接前的 adb server/设备在线管理，以及 main.py
嵌入 scrcpy 时需要的屏幕宽高比读取。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

# Windows 下隐藏 adb 子进程的命令行窗口（exe 无控制台模式下每次调用都会闪窗）
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _get_wifi_cache_file() -> Path | None:
    try:
        from ..config import APP_ROOT

        p = APP_ROOT / "runs" / "wifi_cache.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:
        return None


def _load_wifi_cache() -> str | None:
    path = _get_wifi_cache_file()
    if path and path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("wifi_ip")
        except Exception:
            pass
    return None


def _save_wifi_cache(ip: str, port: int) -> None:
    path = _get_wifi_cache_file()
    if path:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"wifi_ip": ip, "wifi_port": port, "time": time.time()}, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


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
    def __init__(
        self,
        adb_path: str,
        serial: str = "",
        auto_wifi_failover: bool | None = None,
        wifi_port: int | None = None,
    ):
        self.adb = adb_path
        self.serial = serial
        if auto_wifi_failover is None or wifi_port is None:
            try:
                from ..config import load_config

                cfg = load_config()
                if auto_wifi_failover is None:
                    auto_wifi_failover = cfg.adb.auto_wifi_failover
                if wifi_port is None:
                    wifi_port = cfg.adb.wifi_port
            except Exception:
                pass
        self.auto_wifi_failover = True if auto_wifi_failover is None else auto_wifi_failover
        self.wifi_port = 5555 if wifi_port is None else wifi_port
        self.wifi_ip: str | None = _load_wifi_cache()
        if self.serial and ":" in self.serial and re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+$", self.serial):
            ip = self.serial.split(":")[0]
            self.wifi_ip = ip
            _save_wifi_cache(ip, self.wifi_port)

    # ---- 基础命令 ----

    def _run(self, *args: str, check: bool = True, use_serial: bool = True) -> subprocess.CompletedProcess:
        cmd = [self.adb]
        if self.serial and use_serial and args and args[0] not in ("connect", "disconnect", "devices", "start-server"):
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

    def get_wifi_ip(self) -> str | None:
        """获取当前设备在 WLAN 下的无线 IP 地址。"""
        out = self._run("shell", "getprop", "dhcp.wlan0.ipaddress", check=False).stdout.decode("utf-8", "replace").strip()
        if out and re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", out):
            return out
        out = self._run("shell", "ip", "-f", "inet", "addr", "show", "wlan0", check=False).stdout.decode("utf-8", "replace")
        match = re.search(r"inet\s+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", out)
        if match:
            return match.group(1)
        out = self._run("shell", "ip", "route", check=False).stdout.decode("utf-8", "replace")
        match = re.search(r"src\s+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", out)
        if match:
            return match.group(1)
        return None

    def prepare_wifi_failover(self) -> bool:
        """在 USB 连接模式下准备无线备用：开启 tcpip 端口并记录 IP。"""
        if not self.auto_wifi_failover:
            return False
        if ":" in self.serial and re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+$", self.serial):
            ip = self.serial.split(":")[0]
            self.wifi_ip = ip
            _save_wifi_cache(ip, self.wifi_port)
            return True
        ip = self.wifi_ip or self.get_wifi_ip()
        if not ip:
            return False
        self.wifi_ip = ip
        _save_wifi_cache(ip, self.wifi_port)
        wifi_target = f"{ip}:{self.wifi_port}"
        online = self.online_devices()
        if wifi_target in online:
            return True
        # 尝试使用 connect 连接已有端口，避免频繁给手机发 tcpip 导致 adbd 重启断连
        self._run("connect", wifi_target, check=False)
        if wifi_target in self.online_devices():
            return True
        proc = self._run("tcpip", str(self.wifi_port), check=False)
        if proc.returncode == 0:
            time.sleep(1.5)
            self._run("connect", wifi_target, check=False)
            return True
        return False

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
        """确认 adb server 已启动且有设备在线；支持 USB 断开及程序重启时自动连接记忆的无线设备。"""
        self._run("start-server")
        devices = self.online_devices()

        def is_wifi_serial(s: str) -> bool:
            return bool(":" in s and re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+$", s))

        def try_connect_wifi() -> str | None:
            wifi_ip = self.wifi_ip or _load_wifi_cache()
            if not (self.auto_wifi_failover and wifi_ip):
                return None
            wifi_target = f"{wifi_ip}:{self.wifi_port}"
            if wifi_target in devices:
                sys.stderr.write(f"自动使用记忆的无线设备: {wifi_target}\n")
                self.serial = wifi_target
                return wifi_target
            conn_proc = self._run("connect", wifi_target, check=False)
            conn_out = conn_proc.stdout.decode("utf-8", "replace")
            if "connected" in conn_out.lower():
                updated_devices = self.online_devices()
                if wifi_target in updated_devices:
                    sys.stderr.write(f"成功自动连接并切换至记忆的无线设备: {wifi_target}\n")
                    self.serial = wifi_target
                    return wifi_target
            return None

        if self.serial:
            if self.serial in devices:
                if not is_wifi_serial(self.serial) and self.auto_wifi_failover:
                    self.prepare_wifi_failover()
                elif is_wifi_serial(self.serial):
                    ip = self.serial.split(":")[0]
                    self.wifi_ip = ip
                    _save_wifi_cache(ip, self.wifi_port)
                return self.serial

            connected = try_connect_wifi()
            if connected:
                return connected

            raise AdbError(f"指定设备 {self.serial} 不在线，当前在线: {devices or '无'}")

        if not devices:
            connected = try_connect_wifi()
            if connected:
                return connected
            raise AdbError("没有在线的 adb 设备，请检查 USB 连接与调试授权。")

        self.serial = devices[0]
        if not is_wifi_serial(self.serial) and self.auto_wifi_failover:
            self.prepare_wifi_failover()
        elif is_wifi_serial(self.serial):
            ip = self.serial.split(":")[0]
            self.wifi_ip = ip
            _save_wifi_cache(ip, self.wifi_port)
        return self.serial

    def screen_size(self) -> tuple[int, int]:
        out = self._run("shell", "wm", "size").stdout.decode("utf-8", "replace")
        # 形如 "Physical size: 1080x2400"
        size = out.strip().split(":")[-1].strip().split("x")
        return int(size[0]), int(size[1])

    def reboot_and_wait(self, timeout: float = 180.0, interval: float = 5.0) -> None:
        """重启设备并等待开机完成（sys.boot_completed=1），超时抛 AdbError。

        开机过程中设备反复 offline/online，wait-for-device 容易卡在
        子进程超时上，改为轮询 getprop：未就绪时 adb 直接报错返回，继续等。
        """
        self._run("reboot")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(interval)
            proc = self._run("shell", "getprop", "sys.boot_completed", check=False)
            if proc.returncode == 0 and proc.stdout.decode("utf-8", "replace").strip() == "1":
                return
        raise AdbError(f"设备重启后 {timeout:.0f}s 内未完成开机")

    def force_stop_app(self, package: str) -> None:
        """强停应用（重启游戏恢复用，如 QQ）。"""
        self._run('shell', 'am', 'force-stop', package)

    def launch_app(self, package: str) -> None:
        """用 monkey 启动应用主 Activity（无需知道具体 Activity 名）。"""
        self._run("shell", "monkey", "-p", package,
                  "-c", "android.intent.category.LAUNCHER", "1")

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
