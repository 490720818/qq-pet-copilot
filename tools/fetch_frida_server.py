"""拉取 frida-server 离线包到 resources/frida-server/。

frida-server 是模拟器侧二进制（不入库，同 scrcpy 的待遇），按 frida 客户端版本 +
设备架构下载官方 GitHub Release 的 xz 压缩包；源码运行模拟器版缺失时自动调用
（src/opener.py），build.py --emulator 打包前也会调用。

用法：
    python tools/fetch_frida_server.py                    # 默认 frida 版本 + x86_64
    python tools/fetch_frida_server.py --version 17.17.0 --arch arm64
    python tools/fetch_frida_server.py --arch x86_64 arm64   # 多架构
    python tools/fetch_frida_server.py --force            # 已存在也重新下载
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TARGET_DIR = PROJECT_ROOT / 'resources' / 'frida-server'
# 默认架构：Windows 模拟器（MuMu/雷电等）基本是 x86_64；其他用 --arch 指定
DEFAULT_ARCH = 'x86_64'
# frida 客户端版本与 frida-server 必须一致（见 requirements.txt 的 frida 锁定版本）
PINNED_VERSION = '17.17.0'

RELEASE_URL = (
    'https://github.com/frida/frida/releases/download/{ver}/'
    'frida-server-{ver}-android-{arch}.xz'
)
# 国内直连 GitHub 不稳，下载失败时按顺序试这些镜像（github 直连 优先）
MIRROR_PREFIXES = (
    '',
    'https://ghfast.top/',
    'https://ghproxy.net/',
    'https://mirror.ghproxy.com/',
)
DOWNLOAD_TIMEOUT = 300  # 秒


def _default_version() -> str:
    """默认取本机安装的 frida 版本；装不上时回退固定版本。"""
    try:
        import frida
        return frida.__version__
    except Exception:
        return PINNED_VERSION


def _download(url: str, target: Path) -> None:
    """下载到 target；GitHub 直连失败时按顺序试镜像源。"""
    import shutil
    last_err: Exception | None = None
    for prefix in MIRROR_PREFIXES:
        u = prefix + url
        try:
            req = urllib.request.Request(u, headers={'User-Agent': 'qq-pet-copilot-fetch'})
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, \
                    open(target, 'wb') as f:
                shutil.copyfileobj(resp, f, length=1024 * 1024)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            target.unlink(missing_ok=True)  # 清掉半截文件，避免下次解压坏包
            print(f'下载失败（{e}），换下一个源...', file=sys.stderr)
    assert last_err is not None
    raise last_err


def ensure_frida_server(version: str | None = None, arch: str = DEFAULT_ARCH,
                        force: bool = False) -> bool:
    """确保 resources/frida-server/frida-server-<版本>-android-<架构>.xz 就位；返回是否就绪。"""
    version = version or _default_version()
    target = TARGET_DIR / f'frida-server-{version}-android-{arch}.xz'
    if target.is_file() and not force:
        print(f'frida-server xz 已就绪: {target}')
        return True
    url = RELEASE_URL.format(ver=version, arch=arch)
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    print(f'下载 frida-server v{version} ({arch}): {url}')
    try:
        _download(url, target)
    except Exception as e:  # noqa: BLE001 - 失败不中断，交由调用方决定
        print(f'frida-server xz 下载失败（离线？）: {e}', file=sys.stderr)
        target.unlink(missing_ok=True)
        return False
    if not target.is_file():
        print(f'下载后未找到 {target}，可能 Release 包结构不符', file=sys.stderr)
        return False
    print(f'frida-server xz 就绪: {target}')
    return True


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description='下载 frida-server 离线包到 resources/frida-server/')
    ap.add_argument('--version', default=None,
                    help=f'frida 版本（默认取本机 frida，固定兜底 {PINNED_VERSION}）')
    ap.add_argument('--arch', nargs='+', default=[DEFAULT_ARCH],
                    help=f'设备架构（默认 {DEFAULT_ARCH}），可多个：x86_64 arm64 ...')
    ap.add_argument('--force', action='store_true', help='已存在也强制重新下载覆盖')
    args = ap.parse_args()
    ok = all(ensure_frida_server(args.version, a, force=args.force) for a in args.arch)
    sys.exit(0 if ok else 1)
