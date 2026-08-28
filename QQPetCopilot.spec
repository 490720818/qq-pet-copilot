# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_all

# 模拟器版（build.py --emulator 会设置 QQ_PET_EMULATOR=1）：内置 frida 客户端，
# exe 名带 Emulator 后缀，与普通版共存于 dist/
EMULATOR = bool(os.environ.get('QQ_PET_EMULATOR'))
EXE_NAME = 'QQPetCopilotEmulator' if EMULATOR else 'QQPetCopilot'

datas = [('config.example.yaml', '.')]
# resources/scrcpy-win64/ 不入库（tools/fetch_scrcpy.py 拉取），存在才随包带上
if Path('resources/scrcpy-win64/scrcpy.exe').is_file():
    datas.append(('resources/scrcpy-win64', 'resources/scrcpy-win64'))
# resources/minitouch/（minitouch 控制方案二进制，tools/fetch_minitouch.py 拉取，不入库）
_minitouch_dir = Path('resources/minitouch')
if _minitouch_dir.is_dir():
    for _f in sorted(_minitouch_dir.rglob('*')):
        if _f.is_file():
            datas.append((str(_f), str(_f.parent)))
binaries = []
hiddenimports = []

if EMULATOR:
    # 模拟器版：frida-server xz 不随包（省 ~32MB）——注入兜底触发时 opener 给出
    # 明确提示，用户把 xz 放到 exe 旁 runs/resources/frida-server/ 即可
    # （tools/fetch_frida_server.py 可下载；注入脚本内置在 src/opener.py）
    # frida：opener 的 Python 注入依赖（frida 包自带 _frida.pyd / frida-core，体积较大）
    frida_all = collect_all('frida')
    datas += frida_all[0]
    binaries += frida_all[1]
    hiddenimports += frida_all[2]
    # collect_dynamic_libs 的默认模式不含 *.pyd，显式把 _frida.pyd（含 frida-core）带进包
    import frida as _frida_mod
    _frida_pkg = Path(_frida_mod.__file__).resolve().parent
    for _pyd in _frida_pkg.glob('*.pyd'):
        binaries.append((str(_pyd), 'frida'))
    # 标记文件：exe 启动时据此默认开启模拟器模式（src/config.is_emulator_build）。
    # 注意 datas 第二项是目标目录（'.' = 包根目录），写错会把文件放进
    # emulator_mode.txt/ 子目录导致 is_emulator_build() 找不到
    marker = Path('build') / 'emulator_mode.txt'
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('1', encoding='utf-8')
    datas.append((str(marker), '.'))
    # frida-java-bridge：Frida 17 不再内置 Java 桥，注入前由 src/opener.py 补桥；
    # 把 frida-tools 自带的 bridges/java.js 作为数据带上（frozen 下没有真实包路径）
    import frida_tools as _frida_tools
    _java_bridge = Path(_frida_tools.__file__).resolve().parent / 'bridges' / 'java.js'
    if _java_bridge.is_file():
        datas.append((str(_java_bridge), 'frida_tools/bridges'))

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


# 普通版排除 frida（模拟器模式专属，由 EMULATOR 分支显式引入）：src/opener.py
# 里有 import frida，PyInstaller 静态分析会把它带进来（+约 42MB）。普通版不需要，
# 模拟器版在 EMULATOR 分支 collect_all 引入，这里不能排除。
_EXCLUDES = ([] if EMULATOR else
             ['frida', 'frida._frida', 'frida.aio',
              'frida_tools', 'prompt_toolkit', 'pygments', 'websockets', 'wcwidth'])

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    runtime_hooks=['tools/pyi_rth_preload_onnxruntime.py'],
    hookspath=[],
    hooksconfig={},
    excludes=_EXCLUDES,
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
_scrcpy_root = Path('resources/scrcpy-win64').resolve()
a.binaries = [t for t in a.binaries
              if not (Path(t[1]).resolve().is_relative_to(_scrcpy_root)
                      and '\\' not in t[0] and '/' not in t[0])]

pyz = PYZ(a.pure)

if os.environ.get('QQ_PET_ONEDIR'):
    # 目录模式
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=EXE_NAME,
              debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
              upx_exclude=[], console=False, disable_windowed_traceback=False,
              argv_emulation=False, target_arch=None, codesign_identity=None,
              entitlements_file=None)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True,
                   upx_exclude=[], name=EXE_NAME)
else:
    # 单文件模式
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
              name=EXE_NAME, debug=False, bootloader_ignore_signals=False,
              strip=False, upx=True, upx_exclude=[], runtime_tmpdir=None,
              console=False, disable_windowed_traceback=False,
              argv_emulation=False, target_arch=None, codesign_identity=None,
              entitlements_file=None)
