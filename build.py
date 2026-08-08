"""PyInstaller 打包脚本。

用法：python build.py            单文件模式（默认）：dist/QQPetCopilot.exe
      python build.py --onedir   目录模式：dist/QQPetCopilot/QQPetCopilot.exe

目录约定（打包后）：
- exe 所在目录：可写数据（config.yaml 首次运行自动复制出来、runs/ 进度与日志）
- exe 同级的 scrcpy-win64/ 若存在则优先于包内资源（方便替换）
"""
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

ONEDIR = '--onedir' in sys.argv

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


def main() -> None:
    # CI（GitHub Actions）控制台是 cp1252，打印中文会 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    # 打包前尝试下载 v6 OCR 模型（runs/ 下；失败不阻塞，exe 会缺 v6 需运行时下载）
    fetch = PROJECT_ROOT / 'tools' / 'fetch_ocr_models.py'
    if fetch.exists():
        subprocess.run([sys.executable, str(fetch)], check=False)
    # 打包前尝试下载 scrcpy（scrcpy-win64/ 不入库；失败不阻塞，exe 会缺画面嵌入）
    fetch_scrcpy = PROJECT_ROOT / 'tools' / 'fetch_scrcpy.py'
    if fetch_scrcpy.exists():
        subprocess.run([sys.executable, str(fetch_scrcpy)], check=False)
    env = dict(os.environ)
    if ONEDIR:
        env['QQ_PET_ONEDIR'] = '1'
    print('开始打包（' + ('onedir 目录模式' if ONEDIR else 'onefile 单文件模式') + '）...')
    subprocess.run(ARGS, check=True, cwd=PROJECT_ROOT, env=env)
    out = PROJECT_ROOT / 'dist' / ('QQPetCopilot/QQPetCopilot.exe' if ONEDIR else 'QQPetCopilot.exe')
    print(f'完成: {out}')


if __name__ == '__main__':
    main()
