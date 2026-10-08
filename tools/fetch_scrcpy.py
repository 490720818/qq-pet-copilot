"""拉取 scrcpy 到项目根 resources/ 下的对应目录。

scrcpy 二进制不入库（体积大、更新频繁、易产生合并冲突），由本脚本从官方
GitHub Release 下载解压。本地首次使用 GUI 前跑一次；build.py 和 CI 打包前也会自动调用。

按平台选资产：
    Windows -> resources/scrcpy-win64/  （scrcpy-win64-v<版本>.zip，含 scrcpy.exe）
    Linux   -> resources/scrcpy-linux/  （scrcpy-linux-x86_64-v<版本>.tar.gz，官方包
               已静态内置 SDL/FFmpeg，免安装即可运行）

用法：
    python tools/fetch_scrcpy.py                  # 按默认版本拉取（已存在则跳过）
    python tools/fetch_scrcpy.py --version 5.0    # 指定版本（默认见 DEFAULT_VERSION）
    python tools/fetch_scrcpy.py --force          # 已存在也强制重新下载覆盖

下载地址形如：
    https://github.com/Genymobile/scrcpy/releases/download/v<版本>/scrcpy-win64-v<版本>.zip
    https://github.com/Genymobile/scrcpy/releases/download/v<版本>/scrcpy-linux-x86_64-v<版本>.tar.gz
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

IS_WIN = sys.platform == 'win32'
if IS_WIN:
    TARGET_DIR = PROJECT_ROOT / 'resources' / 'scrcpy-win64'
    SCRCPY_EXE = TARGET_DIR / 'scrcpy.exe'
else:
    TARGET_DIR = PROJECT_ROOT / 'resources' / 'scrcpy-linux'
    SCRCPY_EXE = TARGET_DIR / 'scrcpy'

# 默认版本：与官方 Release 资产名 scrcpy-win64-v<版本>.zip /
# scrcpy-linux-x86_64-v<版本>.tar.gz 对应。需要换版本时用 --version 指定，或直接改这里。
DEFAULT_VERSION = '4.1'

RELEASE_URL = (
    'https://github.com/Genymobile/scrcpy/releases/download/'
    'v{ver}/scrcpy-win64-v{ver}.zip'
) if IS_WIN else (
    'https://github.com/Genymobile/scrcpy/releases/download/'
    'v{ver}/scrcpy-linux-x86_64-v{ver}.tar.gz'
)
DOWNLOAD_TIMEOUT = 120  # 秒


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """解压并防止 zip-slip（成员名带绝对路径或 .. 时拒绝）。"""
    dest = dest.resolve()
    for info in zf.infolist():
        name = Path(info.filename)
        if name.is_absolute() or '..' in name.parts:
            raise ValueError(f'压缩包内出现不安全路径: {info.filename}')
        target = (dest / name).resolve()
        if dest not in target.parents and target != dest:
            raise ValueError(f'压缩包成员越界: {info.filename}')
    zf.extractall(dest)


def _flatten_into(src: Path, dest: Path) -> None:
    """把 src 的内容平铺进 dest（覆盖同名），兼容官方包顶层带 scrcpy-<平台>-vX/ 目录的结构。"""
    # 顶层只有一个目录时取其内容，否则直接取根
    children = list(src.iterdir())
    if len(children) == 1 and children[0].is_dir():
        src = children[0]
    for item in src.iterdir():
        target = dest / item.name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        shutil.move(str(item), str(target))


def _extract_zip(zip_path: Path, dest: Path) -> None:
    """解压 zip 到临时目录后平铺进 dest。"""
    tmp = Path(tempfile.mkdtemp(dir=str(dest), prefix='.scrcpy_fetch_'))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extract(zf, tmp)
        _flatten_into(tmp, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _extract_tar(tar_path: Path, dest: Path) -> None:
    """解压 tar.gz 到临时目录后平铺进 dest。

    Python 3.12+ 的 tarfile 有 filter 参数可防目录穿越；3.11 及以下没有，
    退回到先逐成员校验路径（同 _safe_extract 的思路）。
    """
    tmp = Path(tempfile.mkdtemp(dir=str(dest), prefix='.scrcpy_fetch_'))
    try:
        with tarfile.open(tar_path) as tf:
            try:
                tf.extractall(tmp, filter='data')  # 3.12+
            except TypeError:
                base = tmp.resolve()
                for member in tf.getmembers():
                    name = Path(member.name)
                    if name.is_absolute() or '..' in name.parts:
                        raise ValueError(f'压缩包内出现不安全路径: {member.name}')
                    target = (base / name).resolve()
                    if base not in target.parents and target != base:
                        raise ValueError(f'压缩包成员越界: {member.name}')
                tf.extractall(tmp)
        _flatten_into(tmp, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _download(url: str, zip_path: Path, attempts: int = 3) -> None:
    """下载 zip 到 zip_path；GitHub Release 偶发超时/抖动，失败自动重试。"""
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'qq-pet-copilot-fetch'})
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, \
                    open(zip_path, 'wb') as f:
                shutil.copyfileobj(resp, f, length=1024 * 1024)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            zip_path.unlink(missing_ok=True)  # 清掉半截文件，避免下次解压坏包
            if i < attempts - 1:
                wait = 2 * (i + 1)
                print(f'下载失败（{e}），{wait}s 后重试（{i + 1}/{attempts}）', file=sys.stderr)
                time.sleep(wait)
    assert last_err is not None
    raise last_err


def ensure_scrcpy(version: str = DEFAULT_VERSION, force: bool = False) -> bool:
    """确保 resources/scrcpy-*/ 下有可用的 scrcpy 可执行文件；返回是否就绪。"""
    if SCRCPY_EXE.is_file() and not force:
        print(f'scrcpy 已就绪: {SCRCPY_EXE}')
        return True

    url = RELEASE_URL.format(ver=version)
    suffix = '.zip' if IS_WIN else '.tar.gz'
    archive = TARGET_DIR / f'scrcpy-{"win64" if IS_WIN else "linux-x86_64"}-v{version}{suffix}'
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    print(f'下载 scrcpy v{version}: {url}')
    try:
        _download(url, archive)
        print(f'解压: {archive}')
        if IS_WIN:
            _extract_zip(archive, TARGET_DIR)
        else:
            _extract_tar(archive, TARGET_DIR)
    except Exception as e:  # noqa: BLE001 - 失败不中断，交由调用方决定
        print(f'下载/解压失败: {e}', file=sys.stderr)
        archive.unlink(missing_ok=True)
        return False
    finally:
        archive.unlink(missing_ok=True)

    if not SCRCPY_EXE.is_file():
        print(f'解压后未找到 {SCRCPY_EXE}，可能 Release 包结构不符', file=sys.stderr)
        return False
    if not IS_WIN:
        # tar 包里的可执行位通常保留，但解压工具/权限异常时会丢，这里兜底
        try:
            for name in ('scrcpy', 'adb'):
                f = TARGET_DIR / name
                if f.is_file():
                    f.chmod(f.stat().st_mode | 0o755)
        except OSError:
            pass
    print(f'scrcpy 就绪: {SCRCPY_EXE}')
    return True


if __name__ == '__main__':
    # CI（GitHub Actions Windows runner）默认 stdout 是 cp1252，直接打印中文会崩；
    # 显式切成 UTF-8，本地/打包/CI 都能正常输出
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=f'下载 scrcpy 到 {TARGET_DIR.name}/')
    ap.add_argument('--version', default=DEFAULT_VERSION,
                    help=f'scrcpy 版本（默认 {DEFAULT_VERSION}）')
    ap.add_argument('--force', action='store_true',
                    help='已存在也强制重新下载覆盖')
    args = ap.parse_args()
    ok = ensure_scrcpy(args.version, force=args.force)
    sys.exit(0 if ok else 1)
