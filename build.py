"""PyInstaller 打包脚本。

用法：python build.py            单文件模式（默认）：dist/QQPetCopilot.exe
      python build.py --onedir   目录模式：dist/QQPetCopilot/QQPetCopilot.exe

目录约定（打包后）：
- exe 所在目录：可写数据（config.yaml 首次运行自动复制出来、runs/ 进度与日志）
- exe 同级的 scrcpy-win64/ 若存在则优先于包内资源（方便替换）
"""
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

ONEDIR = '--onedir' in sys.argv

# config.yaml 不入库（个人配置），不存在时打包示例配置；
# exe 首次运行会把它复制为 config.yaml
CONFIG_SRC = 'config.yaml' if (PROJECT_ROOT / 'config.yaml').is_file() else 'config.example.yaml'

ARGS = [
    sys.executable, '-m', 'PyInstaller',
    '--noconfirm', '--clean',
    '--windowed',          # 无控制台窗口
    '--onedir' if ONEDIR else '--onefile',
    '--name', 'QQPetCopilot',
    # rapidocr 的 config.yaml 和 onnx 模型是包内数据文件，不会自动收集
    '--collect-all', 'rapidocr_onnxruntime',
    # Windows 下 --add-data 用分号分隔 源;目标
    '--add-data', 'scrcpy-win64;scrcpy-win64',
    '--add-data', f'{CONFIG_SRC};config.yaml',
    'main.py',
]


def main() -> None:
    # CI（GitHub Actions）控制台是 cp1252，打印中文会 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    print('开始打包（' + ('onedir 目录模式' if ONEDIR else 'onefile 单文件模式') + '）...')
    subprocess.run(ARGS, check=True, cwd=PROJECT_ROOT)
    out = PROJECT_ROOT / 'dist' / ('QQPetCopilot/QQPetCopilot.exe' if ONEDIR else 'QQPetCopilot.exe')
    print(f'完成: {out}')


if __name__ == '__main__':
    main()
