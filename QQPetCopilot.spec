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
datas += collect_data_files('uiautomator2')
tmp_ret = collect_all('rapidocr')
# rapidocr 包自带的 v4 onnx 模型不打包（本项目只用 PP-OCRV5 mobile，见 runs/models/rapidocr）
datas += [d for d in tmp_ret[0] if not str(d[0]).lower().endswith('.onnx')]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]
# OCR 模型（PP-OCRV5 mobile，runs/models/ 下）：存在才随包带上
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
