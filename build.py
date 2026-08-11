"""PyInstaller 打包脚本。

用法：python build.py                 单文件模式（默认）：dist/QQPetCopilot.exe
      python build.py --onedir       目录模式：dist/QQPetCopilot/QQPetCopilot.exe
      python build.py --emulator     模拟器版（内置 hook JS + frida-server 离线包 + frida）：
                                     dist/QQPetCopilotEmulator.exe
      python build.py --all          普通版 + 模拟器版一起打包

目录约定（打包后）：
- exe 所在目录：可写数据（config.yaml 首次运行自动复制出来、runs/ 进度与日志）
- exe 同级的 resources/scrcpy-win64/ 若存在则优先于包内资源（方便替换）
- 模拟器版：assets/qqpet-module-opener/（hook JS，入库）+ resources/frida-server/（xz 离线包，不入库）
"""
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

ONEDIR = '--onedir' in sys.argv
EMULATOR = '--emulator' in sys.argv
BUILD_ALL = '--all' in sys.argv

# config.yaml 不入库（个人配置），打包一律用示例配置；
# exe 首次运行会把它复制为 config.yaml
CONFIG_SRC = 'config.example.yaml'

# 走 QQPetCopilot.spec 打包：spec 里对 rapidocr 包的 v4/v5 onnx 做了过滤（只带 v6 tiny，
# 见 runs/models/rapidocr），并支持 onedir/onefile 两种模式（QQ_PET_ONEDIR 环境变量）
ARGS = [
    sys.executable, '-m', 'PyInstaller',
    '--noconfirm', '--clean',
    'QQPetCopilot.spec',
]

# 模拟器版默认离线的 frida-server 架构（xz 放 resources/frida-server/，tools/fetch_frida_server.py 拉取）
FRIDA_SERVER_REL = Path('resources') / 'frida-server'
FRIDA_SERVER_ARCH = 'x86_64'
# frida 客户端版本与 frida-server 必须一致（见 requirements.txt 的 frida 锁定版本）
FRIDA_VERSION = '17.17.0'


def fetch_common() -> None:
    """打包前下载公共依赖（OCR 模型 / scrcpy）；失败不阻塞，exe 缺资源时另行处理。"""
    fetch = PROJECT_ROOT / 'tools' / 'fetch_ocr_models.py'
    if fetch.exists():
        subprocess.run([sys.executable, str(fetch)], check=False)
    fetch_scrcpy = PROJECT_ROOT / 'tools' / 'fetch_scrcpy.py'
    if fetch_scrcpy.exists():
        subprocess.run([sys.executable, str(fetch_scrcpy)], check=False)


def ensure_frida_server_xz() -> None:
    """确保模拟器版用的 frida-server xz 就位（默认 x86_64，离线打包）。

    统一走 tools/fetch_frida_server.py：本地已有则直接用，缺失时尝试从
    GitHub Release 下载（CI 等有网环境），失败不阻塞——exe 缺它时运行期会给出明确提示。
    """
    fetch = PROJECT_ROOT / 'tools' / 'fetch_frida_server.py'
    subprocess.run(
        [sys.executable, str(fetch), '--version', FRIDA_VERSION, '--arch', FRIDA_SERVER_ARCH],
        check=False,
    )


def build(emulator: bool) -> None:
    env = dict(os.environ)
    if ONEDIR:
        env['QQ_PET_ONEDIR'] = '1'
    else:
        env.pop('QQ_PET_ONEDIR', None)
    if emulator:
        ensure_frida_server_xz()
        env['QQ_PET_EMULATOR'] = '1'
    else:
        env.pop('QQ_PET_EMULATOR', None)
    name = 'QQPetCopilotEmulator' if emulator else 'QQPetCopilot'
    mode = 'onedir 目录模式' if ONEDIR else 'onefile 单文件模式'
    print('开始打包（' + ('模拟器版，' if emulator else '普通版，') + mode + '）...')
    subprocess.run(ARGS, check=True, cwd=PROJECT_ROOT, env=env)
    out = PROJECT_ROOT / 'dist' / (f'{name}/{name}.exe' if ONEDIR else f'{name}.exe')
    print(f'完成: {out}')


def main() -> None:
    # CI（GitHub Actions）控制台是 cp1252，打印中文会 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    fetch_common()
    if BUILD_ALL:
        build(emulator=False)
        build(emulator=True)
    else:
        build(emulator=EMULATOR)


if __name__ == '__main__':
    main()
