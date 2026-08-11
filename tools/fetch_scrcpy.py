"""拉取 scrcpy（win64）到项目根 resources/scrcpy-win64/。

scrcpy 二进制不入库（体积大、更新频繁、易产生合并冲突），由本脚本从官方
GitHub Release 下载解压。本地首次使用 GUI 前跑一次；build.py 和 CI 打包前也会自动调用。

用法：
    python tools/fetch_scrcpy.py                  # 按默认版本拉取（已存在则跳过）
    python tools/fetch_scrcpy.py --version 3.3    # 指定版本（默认见 DEFAULT_VERSION）
    python tools/fetch_scrcpy.py --force          # 已存在也强制重新下载覆盖

下载地址形如：
    https://github.com/Genymobile/scrcpy/releases/download/v<版本>/scrcpy-win64-v<版本>.zip
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TARGET_DIR = PROJECT_ROOT / 'resources' / 'scrcpy-win64'
SCRCPY_EXE = TARGET_DIR / 'scrcpy.exe'

# 默认版本：与官方 Release 资产名 scrcpy-win64-v<版本>.zip 对应。
# 需要换版本时用 --version 指定，或直接改这里。
DEFAULT_VERSION = '4.1'

RELEASE_URL = (
    'https://github.com/Genymobile/scrcpy/releases/download/'
    'v{ver}/scrcpy-win64-v{ver}.zip'
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


def _extract_zip(zip_path: Path, dest: Path) -> None:
    """解压到临时目录后平铺进 dest，兼容官方 zip 顶层带 scrcpy-win64-vX/ 目录的结构。"""
    tmp = Path(tempfile.mkdtemp(dir=str(dest), prefix='.scrcpy_fetch_'))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extract(zf, tmp)
        # 顶层只有一个目录（如 scrcpy-win64-v4.1/）时取其内容，否则直接取根
        src = tmp
        children = list(tmp.iterdir())
        if len(children) == 1 and children[0].is_dir():
            src = children[0]
        for item in src.iterdir():
            target = dest / item.name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            shutil.move(str(item), str(target))
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
    """确保 scrcpy-win64/ 下有可用的 scrcpy.exe；返回是否就绪。"""
    if SCRCPY_EXE.is_file() and not force:
        print(f'scrcpy 已就绪: {SCRCPY_EXE}')
        return True

    url = RELEASE_URL.format(ver=version)
    zip_path = TARGET_DIR / f'scrcpy-win64-v{version}.zip'
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    print(f'下载 scrcpy v{version}: {url}')
    try:
        _download(url, zip_path)
        print(f'解压: {zip_path}')
        _extract_zip(zip_path, TARGET_DIR)
    except Exception as e:  # noqa: BLE001 - 失败不中断，交由调用方决定
        print(f'下载/解压失败: {e}', file=sys.stderr)
        zip_path.unlink(missing_ok=True)
        return False
    finally:
        zip_path.unlink(missing_ok=True)

    if not SCRCPY_EXE.is_file():
        print(f'解压后未找到 {SCRCPY_EXE}，可能 Release 包结构不符', file=sys.stderr)
        return False
    print(f'scrcpy 就绪: {SCRCPY_EXE}')
    return True


if __name__ == '__main__':
    # CI（GitHub Actions Windows runner）默认 stdout 是 cp1252，直接打印中文会崩；
    # 显式切成 UTF-8，本地/打包/CI 都能正常输出
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description='下载 scrcpy win64 到 scrcpy-win64/')
    ap.add_argument('--version', default=DEFAULT_VERSION,
                    help=f'scrcpy 版本（默认 {DEFAULT_VERSION}）')
    ap.add_argument('--force', action='store_true',
                    help='已存在也强制重新下载覆盖')
    args = ap.parse_args()
    ok = ensure_scrcpy(args.version, force=args.force)
    sys.exit(0 if ok else 1)
