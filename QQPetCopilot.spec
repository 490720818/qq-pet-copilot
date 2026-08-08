# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_all

datas = [('config.example.yaml', '.')]
# scrcpy-win64/ 不入库（tools/fetch_scrcpy.py 拉取），存在才随包带上
if Path('scrcpy-win64/scrcpy.exe').is_file():
    datas.append(('scrcpy-win64', 'scrcpy-win64'))
binaries = []
hiddenimports = []
# conda Python 3.12 的 C 扩展依赖 base 环境 Library/bin 下的 DLL，PyInstaller 在
# conda venv 下搜不到它们，导致冻结环境里相关模块加载失败：
# - _ctypes.pyd 依赖 ffi.dll → import ctypes 失败（onepush→Crypto 报“未安装 onepush”）
# - _ssl.pyd 依赖 libcrypto/libssl → import ssl 失败（smtplib 缺 SMTP_SSL、HTTPS 不可用）
# 这里把 base 环境的这几个 DLL 手动带进包根目录。
import sys as _sys
_conda_bin = Path(_sys.base_prefix) / 'Library' / 'bin'
for _dll in ('ffi.dll', 'libcrypto-3-x64.dll', 'libssl-3-x64.dll'):
    _p = _conda_bin / _dll
    if _p.is_file():
        binaries.append((str(_p), '.'))
datas += collect_data_files('uiautomator2')
tmp_ret = collect_all('rapidocr')
# rapidocr 包自带的 v4/v5 onnx 模型不打包（本项目只用 PP-OCRv6 tiny，见 runs/models/rapidocr）
datas += [d for d in tmp_ret[0] if not str(d[0]).lower().endswith('.onnx')]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]
# OCR 模型（PP-OCRv6 tiny，runs/models/ 下）：存在才随包带上
if Path('runs/models/rapidocr').exists():
    datas += [('runs/models/rapidocr', 'models/rapidocr')]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    runtime_hooks=['tools/pyi_rth_preload_onnxruntime.py'],
    hookspath=[],
    hooksconfig={},
    excludes=[],
    noarchive=False,
    optimize=0,
)
# 本项目只用截图 OCR，不读视频流：排除 opencv 的 videoio ffmpeg dll
# （opencv_videoio_ffmpeg*.dll 约 30.9MB，打包体积大头；去掉后 cv2.VideoCapture 不可用，其余功能不受影响）
a.binaries = [t for t in a.binaries if 'opencv_videoio_ffmpeg' not in t[0].lower()]

# 注意：datas 里的 PE 文件会被 PyInstaller 自动提升为 binaries，所以 scrcpy-win64 下的
# exe/DLL 实际由 BINARY 条目提供（目标仍是 scrcpy-win64\xxx）；二进制依赖分析额外把其中
# 6 个 DLL（avcodec/SDL3/avutil/avformat/swresample/libusb）以根目录为目标又收集了一份，
# 约 15MB 未压缩 / 5.8MB 压缩后。这里只去掉目标在根目录的重复项，保留 scrcpy-win64\ 下的。
_scrcpy_root = Path('scrcpy-win64').resolve()
a.binaries = [t for t in a.binaries
              if not (Path(t[1]).resolve().is_relative_to(_scrcpy_root)
                      and '\\' not in t[0] and '/' not in t[0])]

pyz = PYZ(a.pure)

if os.environ.get('QQ_PET_ONEDIR'):
    # 目录模式
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='QQPetCopilot',
              debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
              upx_exclude=[], console=False, disable_windowed_traceback=False,
              argv_emulation=False, target_arch=None, codesign_identity=None,
              entitlements_file=None)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True,
                   upx_exclude=[], name='QQPetCopilot')
else:
    # 单文件模式
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
              name='QQPetCopilot', debug=False, bootloader_ignore_signals=False,
              strip=False, upx=True, upx_exclude=[], runtime_tmpdir=None,
              console=False, disable_windowed_traceback=False,
              argv_emulation=False, target_arch=None, codesign_identity=None,
              entitlements_file=None)
