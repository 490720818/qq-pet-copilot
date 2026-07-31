"""PyInstaller 打包脚本。

用法：python build.py            单文件模式（默认）：dist/QQPetCopilot.exe
      python build.py --onedir   目录模式：dist/QQPetCopilot/QQPetCopilot.exe

目录约定（打包后）：
- exe 所在目录：可写数据（config.yaml 首次运行自动复制出来、runs/ 进度与日志）
- exe 同级的 templates/、scrcpy-win64/ 若存在则优先于包内资源（方便替换模板）
"""
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

ONEDIR = '--onedir' in sys.argv

ARGS = [
    sys.executable, '-m', 'PyInstaller',
    '--noconfirm', '--clean',
    '--windowed',          # 无控制台窗口
    '--onedir' if ONEDIR else '--onefile',
    '--name', 'QQPetCopilot',
    # Windows 下 --add-data 用分号分隔 源;目标
    '--add-data', 'templates;templates',
    '--add-data', 'scrcpy-win64;scrcpy-win64',
    '--add-data', 'config.yaml;.',
    'main.py',
]


def main() -> None:
    print('开始打包（' + ('onedir 目录模式' if ONEDIR else 'onefile 单文件模式') + '）...')
    subprocess.run(ARGS, check=True, cwd=PROJECT_ROOT)
    out = PROJECT_ROOT / 'dist' / ('QQPetCopilot/QQPetCopilot.exe' if ONEDIR else 'QQPetCopilot.exe')
    print(f'完成: {out}')


if __name__ == '__main__':
    main()
