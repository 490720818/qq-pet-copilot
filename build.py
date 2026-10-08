"""PyInstaller 打包脚本。

用法：python build.py                 单文件模式（默认）：dist/QQPetCopilot.exe
      python build.py --onedir       目录模式：dist/QQPetCopilot/QQPetCopilot.exe
      python build.py --emulator     模拟器版（内置 frida 客户端，frida-server xz 不随包——
                                     注入兜底触发时按提示自行放置）：
                                     dist/QQPetCopilotEmulator.exe
      python build.py --all          普通版 + 模拟器版一起打包

平台：Windows 产物带 .exe；Linux 产物为 dist/QQPetCopilot（无后缀，CI 见
.github/workflows/release.yml 的 build-linux job）。--emulator 只对 Windows 有意义
（MuMu/雷电/夜神/蓝叠只有 Windows 版），Linux 上仍可打但产物无用。

目录约定（打包后）：
- exe 所在目录：可写数据（config.yaml 首次运行自动复制出来、runs/ 进度与日志）
- exe 同级的 resources/scrcpy-win64/ 若存在则优先于包内资源（方便替换）
- 模拟器版：frida-server xz 不打包（省 ~32MB），兜底需要时放到 exe 旁
  runs/resources/frida-server/（注入脚本内置在 src/opener.py）
"""
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

IS_WIN = sys.platform == 'win32'
EXE_SUFFIX = '.exe' if IS_WIN else ''

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

def fetch_common() -> None:
    """打包前下载公共依赖（OCR 模型 / scrcpy）；失败不阻塞，exe 缺资源时另行处理。"""
    fetch = PROJECT_ROOT / 'tools' / 'fetch_ocr_models.py'
    if fetch.exists():
        subprocess.run([sys.executable, str(fetch)], check=False)
    fetch_scrcpy = PROJECT_ROOT / 'tools' / 'fetch_scrcpy.py'
    if fetch_scrcpy.exists():
        subprocess.run([sys.executable, str(fetch_scrcpy)], check=False)
    # minitouch 控制方案二进制（resources/minitouch/，不入库）：x86_64 只对 Windows 版
    # 模拟器有意义，Linux 上只有真机（arm64-v8a），少拉一个
    arches = ['x86_64', 'arm64-v8a'] if IS_WIN else ['arm64-v8a']
    fetch_minitouch = PROJECT_ROOT / 'tools' / 'fetch_minitouch.py'
    if fetch_minitouch.exists():
        subprocess.run([sys.executable, str(fetch_minitouch),
                        '--arch', *arches], check=False)


def build(emulator: bool) -> None:
    env = dict(os.environ)
    if ONEDIR:
        env['QQ_PET_ONEDIR'] = '1'
    else:
        env.pop('QQ_PET_ONEDIR', None)
    if emulator:
        env['QQ_PET_EMULATOR'] = '1'
    else:
        env.pop('QQ_PET_EMULATOR', None)
    name = 'QQPetCopilotEmulator' if emulator else 'QQPetCopilot'
    mode = 'onedir 目录模式' if ONEDIR else 'onefile 单文件模式'
    if emulator and not IS_WIN:
        print('提示：模拟器版只对 Windows 模拟器（MuMu/雷电/夜神/蓝叠）有意义，'
              'Linux 上只能跑真机；这里仍然照常打包，但产物无用。')
    print('开始打包（' + ('模拟器版，' if emulator else '普通版，') + mode + '）...')
    subprocess.run(ARGS, check=True, cwd=PROJECT_ROOT, env=env)
    out = PROJECT_ROOT / 'dist' / (f'{name}/{name}{EXE_SUFFIX}' if ONEDIR
                                   else f'{name}{EXE_SUFFIX}')
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
