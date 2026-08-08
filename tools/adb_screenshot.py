r"""adb 截图工具：一键抓取 Android 设备屏幕截图，保存到 screenshots/。

复用项目配置（config.yaml 的 adb.path / adb.device_serial），
adb 路径自动定位（配置路径 -> PATH -> 常见安装目录），设备未指定时选在线第一台。

用法：
    .venv/Scripts/python tools/adb_screenshot.py                  # 抓一张到 screenshots/screen_YYYYmmdd_HHMMSS_mmm.png
    .venv/Scripts/python tools/adb_screenshot.py --name main      # 自定义文件名（自动补时间戳后缀）
    .venv/Scripts/python tools/adb_screenshot.py --count 5 --interval 1   # 连拍 5 张，间隔 1 秒（抓动画/多帧状态）
    .venv/Scripts/python tools/adb_screenshot.py --out tmp --serial ba286ada  # 指定目录/设备
    .venv/Scripts/python tools/adb_screenshot.py --list           # 列出在线设备

说明：走 `adb exec-out screencap -p` 直接拿 PNG 字节（比 shell 重定向 + pull 稳），
屏幕关闭或设备无显示时 adb 可能返回空/损坏数据，工具会报错提示。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Windows 下隐藏 adb 子进程的命令行窗口（与 src/adb/device.py 一致）
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _stdout_utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass  # 个别重定向场景不支持 reconfigure，忽略


def resolve_adb(configured: str = "") -> str:
    """按 配置路径 -> PATH -> 常见目录 定位 adb（复用 src.config.find_adb）。"""
    from src.config import find_adb

    path = find_adb(configured)
    if not path:
        raise SystemExit("找不到 adb，请配置 config.yaml 的 adb.path 或安装 platform-tools")
    return path


def list_devices(adb: str) -> list[str]:
    """返回在线设备序列号列表。"""
    proc = subprocess.run([adb, "devices"], capture_output=True, timeout=30,
                          creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        raise SystemExit(f"adb devices 失败: {proc.stderr.decode('utf-8', 'replace')}")
    serials = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines()[1:]:
        parts = line.split()
        if len(parts) == 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def resolve_serial(adb: str, serial: str) -> str:
    """确认设备在线：指定序列号必须在线，否则报错；未指定选第一台。"""
    online = list_devices(adb)
    if serial:
        if serial not in online:
            raise SystemExit(f"指定设备 {serial} 不在线，当前在线: {online or '无'}")
        return serial
    if not online:
        raise SystemExit("没有在线的 adb 设备，请检查 USB 连接与调试授权")
    return online[0]


def screenshot(adb: str, serial: str, path: Path) -> None:
    """抓一张截图写到 path；失败（空/损坏数据）抛 RuntimeError。"""
    cmd = [adb, "-s", serial, "exec-out", "screencap", "-p"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30,
                              creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"adb screencap 超时: {e}") from None
    if proc.returncode != 0:
        raise RuntimeError(f"adb screencap 失败: {proc.stderr.decode('utf-8', 'replace')}")
    if not proc.stdout.startswith(PNG_MAGIC):
        raise RuntimeError(
            f"截图数据异常（{len(proc.stdout)} 字节，非 PNG）——屏幕可能已关闭，"
            "先点亮屏幕再试（adb shell input keyevent KEYCODE_WAKEUP）")
    path.write_bytes(proc.stdout)


def main() -> None:
    _stdout_utf8()
    ap = argparse.ArgumentParser(description="adb 截图工具")
    ap.add_argument("--out", default=str(PROJECT_ROOT / "screenshots"),
                    help="保存目录（默认 screenshots/）")
    ap.add_argument("--name", default="", help="文件名前缀（默认 screen；自动追加时间戳）")
    ap.add_argument("--count", type=int, default=1, help="连拍张数（默认 1）")
    ap.add_argument("--interval", type=float, default=1.0, help="连拍间隔秒数（默认 1.0）")
    ap.add_argument("--serial", default="", help="设备序列号（默认取 config.yaml 或在线第一台）")
    ap.add_argument("--list", action="store_true", help="只列出在线设备并退出")
    args = ap.parse_args()

    from src.config import load_config

    cfg = load_config()
    adb = resolve_adb(cfg.adb.path)
    serial = resolve_serial(adb, args.serial or cfg.adb.device_serial)

    if args.list:
        print("在线设备: " + (", ".join(list_devices(adb)) or "无"))
        return

    if args.count < 1:
        raise SystemExit("--count 至少为 1")
    if args.count > 1 and args.interval < 0.2:
        raise SystemExit("--interval 太短（至少 0.2 秒）")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = args.name or "screen"
    import time as _time

    print(f"设备 {serial}，共 {args.count} 张 -> {out_dir}")
    for i in range(args.count):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = out_dir / f"{prefix}_{ts}.png"
        try:
            screenshot(adb, serial, path)
        except RuntimeError as e:
            raise SystemExit(f"截图失败: {e}") from None
        print(f"  [{i + 1}/{args.count}] {path} ({path.stat().st_size / 1024:.0f} KB)")
        if i < args.count - 1:
            _time.sleep(args.interval)


if __name__ == "__main__":
    main()
