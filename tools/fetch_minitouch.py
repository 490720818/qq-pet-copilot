"""拉取 minitouch 预编译二进制到 resources/minitouch/。

minitouch（openstf）是 Android 底层触摸注入工具，通过 socket 本地直发触摸事件，
比 uiautomator2 的 d.click（JSON-RPC）在模拟器上更可靠；作为"控制方案"配置项
control.method=minitouch 时由 src/u2dev.py 使用。二进制不入库（同 scrcpy/frida-server），
缺失时 src/u2dev.py 自动调用本脚本下载。

用法：
    python tools/fetch_minitouch.py                     # 默认 x86_64（Windows 模拟器）
    python tools/fetch_minitouch.py --arch arm64-v8a    # 真机
    python tools/fetch_minitouch.py --arch x86_64 arm64-v8a
    python tools/fetch_minitouch.py --force             # 已存在也重新下载
"""
from __future__ import annotations

import argparse
import shutil
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TARGET_DIR = PROJECT_ROOT / 'resources' / 'minitouch'
# 默认架构：Windows 模拟器（MuMu/雷电等）基本是 x86_64；真机用 --arch arm64-v8a
DEFAULT_ARCH = 'x86_64'
# devicefarmer/minitouch 预编译 npm 包版本（含各 abi 的 minitouch 二进制）
PINNED_VERSION = '1.3.0'

# 预编译二进制下载源：jsDelivr（npm CDN）优先，unpkg / GitHub openatx/stf-binaries 兜底
BASE_URLS = (
    'https://cdn.jsdelivr.net/npm/@devicefarmer/minitouch-prebuilt@{ver}/prebuilt/{abi}/bin/minitouch',
    'https://unpkg.com/@devicefarmer/minitouch-prebuilt@{ver}/prebuilt/{abi}/bin/minitouch',
    'https://raw.githubusercontent.com/openatx/stf-binaries/0.3.0/node_modules/'
    '@devicefarmer/minitouch-prebuilt/prebuilt/{abi}/bin/minitouch',
)
DOWNLOAD_TIMEOUT = 180  # 秒


def _download(url: str, target: Path) -> None:
    """下载到 target；源失败时依次换下一个源。"""
    last_err: Exception | None = None
    for u in url:
        try:
            req = urllib.request.Request(u, headers={'User-Agent': 'qq-pet-copilot-fetch'})
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, \
                    open(target, 'wb') as f:
                shutil.copyfileobj(resp, f, length=1024 * 1024)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            target.unlink(missing_ok=True)  # 清掉半截文件
            print(f'下载失败（{e}），换下一个源...', file=sys.stderr)
    assert last_err is not None
    raise last_err


def ensure_minitouch(arch: str = DEFAULT_ARCH, force: bool = False) -> bool:
    """确保 resources/minitouch/minitouch-<arch> 就位；返回是否就绪。"""
    target = TARGET_DIR / f'minitouch-{arch}'
    if target.is_file() and not force:
        print(f'minitouch 已就绪: {target}')
        return True
    urls = [u.format(ver=PINNED_VERSION, abi=arch) for u in BASE_URLS]
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    print(f'下载 minitouch ({arch}): {urls[0]}')
    try:
        _download(urls, target)
    except Exception as e:  # noqa: BLE001 - 失败不中断，交由调用方决定
        print(f'minitouch 下载失败（离线？）: {e}', file=sys.stderr)
        target.unlink(missing_ok=True)
        return False
    # 可执行位（Windows 上无意义，但保证语义）
    try:
        target.chmod(0o755)
    except OSError:
        pass
    if not target.is_file():
        print(f'下载后未找到 {target}', file=sys.stderr)
        return False
    print(f'minitouch 就绪: {target}（{target.stat().st_size} 字节）')
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description='下载 minitouch 预编译二进制到 resources/minitouch/')
    ap.add_argument('--arch', nargs='+', default=[DEFAULT_ARCH],
                    help=f'设备 ABI（可多个），默认 {DEFAULT_ARCH}；可选 arm64-v8a / x86_64')
    ap.add_argument('--force', action='store_true', help='已存在也重新下载')
    args = ap.parse_args()
    ok = True
    for arch in args.arch:
        ok = ensure_minitouch(arch, force=args.force) and ok
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
