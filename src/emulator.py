"""模拟器实例自动探测：serial 扫描 + 按类型分步停/启（参考 ALAS module/device/platform）。

支持的模拟器（path_to_type 按 exe 文件名 + 上级目录名识别）：
- MuMu Player 12（MuMuPlayer.exe / MuMuNxMain.exe / MuMuManager.exe）
- MuMu 6（NemuPlayer.exe，nemu 目录）/ MuMu X（nemu9 目录）
- 雷电 LDPlayer 3/4/9（dnplayer.exe，目录名区分版本）
- 夜神 Nox 32/64 位（Nox.exe）
- 蓝叠 BlueStacks 4（Bluestacks.exe）/ 5（HD-Player.exe，bluestacks_nxt 目录）
- 逍遥 MEmu（MEmu.exe）

安装目录来源（取并集去重，均不依赖第三方库）：
1. 卸载项注册表（HKLM，按子键名精确匹配，ALAS 的 known_emulator_registry_name）
2. MuiCache / UserAssist（HKCU，记录最近运行过的 exe；UserAssist 值名需 ROT13 解码）
3. 雷电专用注册表 InstallDir（SOFTWARE\\leidian\\ldplayer*）

实例 serial 算法（ALAS iter_instances 的魔法数字，均有实测依据）：
- vbox/nemu/memu 配置通用的 adb 转发正则：hostport="X" ... guestport="5555"
- MuMu12 优先 MuMuManager info 的 adb_port，v4.0.4 默认实例 vbox 无转发记录，
  兜底 16384 + 32*实例序号；同一实例可能有多个转发端口（16416/7555 并存），全部收集
- 雷电 vbox 里没有转发配置，按规律 5555 + 2*index
- 蓝叠 4 端口不固定（每次启动递增），硬编码 127.0.0.1:5555
- 蓝叠 5 读 bluestacks.conf 的 bst.instance.<name>.status.adb_port

停/启统一分步（stop -> 等进程退出 -> start，比 restart 可控，见 ALAS）：
有控制台 exe 的走控制台（MuMuManager / ldconsole / bsconsole / memuc），
没有的（蓝叠 5、MuMu 6/X）按 exe 路径杀进程再用主 exe 拉起；
MuMu12 启动必须走 MuMuManager（MuMuNxMain.exe 是 GUI 单例，并发启动请求会被静默丢弃）。
"""
from __future__ import annotations

import codecs
import json
import os
import re
import subprocess
import sys
import time
import winreg
from dataclasses import dataclass, field
from pathlib import Path

# Windows 下隐藏子进程的命令行窗口
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# ---- 模拟器类型常量（与 ALAS EmulatorBase 对齐，GUI 下拉用） ----
NOX = 'NoxPlayer'
NOX64 = 'NoxPlayer64'
BS4 = 'BlueStacks4'
BS5 = 'BlueStacks5'
LD3 = 'LDPlayer3'
LD4 = 'LDPlayer4'
LD9 = 'LDPlayer9'
MUMU6 = 'MuMuPlayer'
MUMUX = 'MuMuPlayerX'
MUMU12 = 'MuMuPlayer12'
MEMU = 'MEmuPlayer'
EMULATOR_TYPES = [MUMU12, MUMU6, MUMUX, LD3, LD4, LD9, NOX, NOX64, BS4, BS5, MEMU]
LD_FAMILY = (LD3, LD4, LD9)
NOX_FAMILY = (NOX, NOX64)
MUMU_FAMILY = (MUMU6, MUMUX, MUMU12)

# 卸载项注册表子键名（精确匹配，ALAS known_emulator_registry_name）
_KNOWN_UNINSTALL_KEYS = {
    'Nox', 'Nox64', 'BlueStacks', 'BlueStacks_nxt', 'BlueStacks_cn', 'BlueStacks_nxt_cn',
    'LDPlayer', 'LDPlayer4', 'LDPlayer9', 'leidian', 'leidian4', 'leidian9',
    'Nemu', 'Nemu9', 'MuMuPlayer', 'MuMuPlayer-12.0', 'MuMu Player 12.0', 'MEmu',
}
_UNINSTALL_ROOTS = (
    r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
    r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
)
# 雷电专用注册表（InstallDir 值，HKCU 优先再 HKLM）
_LD_REG_PATHS = (r'SOFTWARE\leidian\ldplayer', r'SOFTWARE\leidian\ldplayer9')

# vbox/nemu/memu 配置里的 adb 转发记录：
# <Forwarding name="port2" proto="1" hostip="127.0.0.1" hostport="62026" guestport="5555"/>
_FORWARD_RE = re.compile(r'hostport="(\d+)"[^>]*guestport="5555"')
# 蓝叠 5 bluestacks.conf：bst.instance.Pie64.status.adb_port="5555"
_BS5_PORT_RE = re.compile(r'bst\.instance\.(\w+)\.status\.adb_port="(\d+)"')
# MuMu12 实例目录/ID：MuMuPlayer-12.0-0 / MuMuPlayerGlobal-12.0-1 / YXArkNights-12.0-2
_MUMU12_ID_RE = re.compile(r'(?:MuMuPlayer(?:Global)?|YXArkNights)-1[25]\.0-(\d+)')
# 雷电实例目录：vms/leidian0
_LD_ID_RE = re.compile(r'^leidian(\d+)$')

_MUMU12_PORT_BASE = 16384   # MuMu12 默认 adb 端口规律：16384 + 32*实例序号
_MUMU6_SERIAL = '127.0.0.1:7555'
_BS4_SERIAL = '127.0.0.1:5555'

_MANAGER_TIMEOUT = 60.0    # MuMuManager info 查询超时（秒）
_CONTROL_TIMEOUT = 180.0   # 停/启命令超时（秒）
SHUTDOWN_WAIT = 10.0       # 停止后等模拟器进程退出的时间（秒）


@dataclass
class EmulatorInstance:
    """一个模拟器实例。"""
    type: str            # 模拟器类型（见上方常量）
    name: str            # 实例名称（vms 目录名 / leidianN / 蓝叠实例名）
    index: int           # 实例序号（无序号概念的为 -1，停/启用 name）
    serials: list[str]   # 该实例全部可连 serial
    path: Path           # 模拟器安装目录
    exe: Path            # 主程序 exe（Nox.exe / dnplayer.exe / HD-Player.exe ...）
    console: Path | None = field(default=None)  # 控制台 exe（MuMuManager / ldconsole ...）

    @property
    def serial(self) -> str:
        """首选 serial。"""
        return self.serials[0] if self.serials else ''


# ---------------------------------------------------------------- 类型识别

def path_to_type(exe: Path) -> str | None:
    """exe 路径 -> 模拟器类型（ALAS Emulator.path_to_type：exe 名 + 上级两级目录名）。"""
    name = exe.name.lower()
    parts = [p.lower() for p in exe.parts]
    dir1 = parts[-2] if len(parts) >= 2 else ''
    dir2 = parts[-3] if len(parts) >= 3 else ''
    if name == 'nox.exe':
        return NOX64 if '64' in dir2 else NOX
    if name in ('bluestacks.exe', 'bluestacksgp.exe', 'hd-player.exe'):
        return BS5 if dir1.startswith('bluestacks_nxt') else BS4
    if name == 'dnplayer.exe':
        if dir1 == 'ldplayer9':
            return LD9
        if dir1 == 'ldplayer4':
            return LD4
        return LD3
    if name == 'nemuplayer.exe':
        if dir2 == 'nemu9':
            return MUMUX
        return MUMU6
    if name in ('mumuplayer.exe', 'mumunxmain.exe', 'mumumanager.exe'):
        return MUMU12
    if name == 'memu.exe':
        return MEMU
    return None


# ---------------------------------------------------------------- 安装目录来源

def _reg_open(root, sub):
    try:
        return winreg.OpenKey(root, sub)
    except OSError:
        return None


# 主 exe 可能不在安装根目录而在子目录（MuMu12 的 nx_main/shell、夜神的 bin、
# MuMu6 的 EmulatorShell，ALAS 同样在 EmulatorShell 子目录里找）
_EXE_SUBDIRS = ('nx_main', 'shell', 'bin', 'EmulatorShell')


def _find_emulator_exe(folder: Path) -> list[Path]:
    """在一个目录及其常见子目录里找可识别的模拟器 exe。"""
    exes: list[Path] = []
    if not folder.is_dir():
        return exes
    dirs = [folder] + [folder / sub for sub in _EXE_SUBDIRS]
    for d in dirs:
        if not d.is_dir():
            continue
        for exe in d.glob('*.exe'):
            if path_to_type(exe):
                exes.append(exe)
    return exes


def _iter_uninstall_exes() -> list[Path]:
    """卸载项注册表（HKLM，子键名精确匹配）：取 UninstallString/InstallLocation 路径，
    在同目录/父目录/常见子目录找可识别的模拟器 exe（ALAS iter_uninstall_registry）。"""
    exes: list[Path] = []
    for sub in _UNINSTALL_ROOTS:
        key = _reg_open(winreg.HKEY_LOCAL_MACHINE, sub)
        if key is None:
            continue
        i = 0
        while True:
            try:
                name = winreg.EnumKey(key, i)
                i += 1
            except OSError:
                break
            if name not in _KNOWN_UNINSTALL_KEYS:
                continue
            entry = _reg_open(key, name)
            if entry is None:
                continue
            dirs: list[Path] = []
            try:
                uninstall = winreg.QueryValueEx(entry, 'UninstallString')[0]
                m = re.search(r'"(.*?)"', uninstall)
                exe_dir = Path(m.group(1)).parent if m else Path(uninstall).parent
                dirs += [exe_dir, exe_dir.parent]
            except OSError:
                pass
            try:
                loc = winreg.QueryValueEx(entry, 'InstallLocation')[0]
                if loc:
                    dirs.append(Path(loc))
            except OSError:
                pass
            for folder in dirs:
                exes.extend(_find_emulator_exe(folder))
    return exes


def _iter_mui_cache_exes() -> list[Path]:
    """MuiCache（HKCU）：最近运行过的 exe 路径（ALAS iter_mui_cache）。"""
    exes: list[Path] = []
    key = _reg_open(winreg.HKEY_CURRENT_USER,
                    r'Software\Classes\Local Settings\Software\Microsoft'
                    r'\Windows\Shell\MuiCache')
    if key is None:
        return exes
    i = 0
    while True:
        try:
            name, _val, _type = winreg.EnumValue(key, i)
            i += 1
        except OSError:
            break
        m = re.match(r'(^.*\.exe)\.', name, re.IGNORECASE)
        if m:
            exe = Path(m.group(1))
            if path_to_type(exe):
                exes.append(exe)
    return exes


def _iter_user_assist_exes() -> list[Path]:
    """UserAssist（HKCU，值名 ROT13 编码）：最近运行过的程序（ALAS iter_user_assist）。"""
    exes: list[Path] = []
    ua = _reg_open(winreg.HKEY_CURRENT_USER,
                   r'Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist')
    if ua is None:
        return exes
    i = 0
    while True:
        try:
            guid = winreg.EnumKey(ua, i)
            i += 1
        except OSError:
            break
        key = _reg_open(ua, guid + r'\Count')
        if key is None:
            continue
        j = 0
        while True:
            try:
                name, _val, _type = winreg.EnumValue(key, j)
                j += 1
            except OSError:
                break
            decoded = codecs.decode(name, 'rot-13')
            if decoded.lower().endswith('.exe'):
                exe = Path(decoded)
                if path_to_type(exe):
                    exes.append(exe)
    return exes


def _iter_ldplayer_reg_exes() -> list[Path]:
    """雷电专用注册表 InstallDir（HKCU 优先再 HKLM，ALAS 同）。"""
    exes: list[Path] = []
    for sub in _LD_REG_PATHS:
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            key = _reg_open(root, sub)
            if key is None:
                continue
            try:
                loc = winreg.QueryValueEx(key, 'InstallDir')[0]
            except OSError:
                continue
            exe = Path(loc) / 'dnplayer.exe'
            if exe.is_file():
                exes.append(exe)
    return exes


def find_emulator_exes() -> list[Path]:
    """全机扫描可识别的模拟器 exe（卸载项 + MuiCache + UserAssist + 雷电注册表），去重。"""
    exes: list[Path] = []
    seen: set[str] = set()
    for exe in (_iter_uninstall_exes() + _iter_mui_cache_exes()
                + _iter_user_assist_exes() + _iter_ldplayer_reg_exes()):
        try:
            key = str(exe.resolve()).lower()
        except OSError:
            key = str(exe).lower()
        if key not in seen and exe.is_file():
            seen.add(key)
            exes.append(exe)
    return exes


def _console_exe(emu_type: str, exe: Path) -> Path | None:
    """主 exe -> 控制台 exe（ALAS single_to_console），找不到返回 None。"""
    if emu_type == MUMU12:
        # 新版（5.x）在 nx_main/，旧版在 shell/
        for rel in ('nx_main/MuMuManager.exe', 'shell/MuMuManager.exe'):
            for base in (exe.parent, exe.parent.parent):
                p = base / rel
                if p.is_file():
                    return p
        return None
    if emu_type in LD_FAMILY:
        p = exe.parent / 'ldconsole.exe'
        return p if p.is_file() else None
    if emu_type == BS4:
        p = exe.parent / 'bsconsole.exe'
        return p if p.is_file() else None
    if emu_type == MEMU:
        p = exe.parent / 'memuc.exe'
        return p if p.is_file() else None
    return None


def _install_dir(emu_type: str, exe: Path) -> Path:
    """模拟器安装根目录（vms/BignoxVMS 等实例目录的父级）。"""
    if emu_type == MUMU12:
        # exe 在 nx_main/ 或 shell/，根目录是其上级
        return exe.parent.parent
    if emu_type in (MUMU6, MUMUX):
        return exe.parent.parent  # shell/ 的上级
    if emu_type in NOX_FAMILY:
        return exe.parent.parent if exe.parent.name.lower() == 'bin' else exe.parent
    return exe.parent


# ---------------------------------------------------------------- 实例枚举

def _forward_ports(file: Path) -> list[int]:
    """vbox/nemu/memu 配置里 hostport->guestport 5555 的转发端口（可能多条）。"""
    ports: list[int] = []
    try:
        text = file.read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return ports
    for port in _FORWARD_RE.findall(text):
        if int(port) not in ports:
            ports.append(int(port))
    return ports


def _mumu12_instances(exe: Path, root: Path, console: Path | None) -> list[EmulatorInstance]:
    """MuMu12：MuMuManager info -v all 的 adb_port + vms/*.nemu 转发端口补全。"""
    info: dict = {}
    if console:
        try:
            proc = subprocess.run([str(console), 'info', '-v', 'all'],
                                  capture_output=True, timeout=_MANAGER_TIMEOUT,
                                  creationflags=_NO_WINDOW)
            out = proc.stdout.decode('utf-8', 'replace')
            # 部分版本会在 JSON 前打印提示行，截取第一个 {
            start = out.find('{')
            if start >= 0:
                data = json.loads(out[start:])
                if isinstance(data, dict):
                    info = data
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    vms_dir = root / 'vms'
    instances: list[EmulatorInstance] = []
    indices = sorted({int(k) for k in info if str(k).isdigit()})
    if not indices and vms_dir.is_dir():
        # info 不可用：扫 vms 目录名拿实例序号
        indices = sorted(int(m.group(1)) for f in vms_dir.iterdir()
                         if f.is_dir() and (m := _MUMU12_ID_RE.match(f.name)))
    for index in indices:
        data = info.get(str(index))
        port = data.get('adb_port') if isinstance(data, dict) else None
        # 没启动过的实例 info 里没有 adb_port：按默认端口规律补上
        ports = [int(port) if port else _MUMU12_PORT_BASE + 32 * index]
        name = f'MuMuPlayer-12.0-{index}'
        for folder in vms_dir.glob(f'*-{index}'):
            if folder.is_dir():
                name = folder.name
                for file in folder.glob('*.nemu'):
                    for p in _forward_ports(file):
                        if p not in ports:
                            ports.append(p)
        instances.append(EmulatorInstance(
            type=MUMU12, name=name, index=index,
            serials=[f'127.0.0.1:{p}' for p in ports],
            path=root, exe=exe, console=console))
    return instances


def _nox_instances(exe: Path, root: Path, emu_type: str) -> list[EmulatorInstance]:
    """夜神：BignoxVMS/<name>/<name>.vbox 的 adb 转发端口。"""
    instances = []
    for vms in (root / 'BignoxVMS', exe.parent / 'BignoxVMS'):
        if not vms.is_dir():
            continue
        for folder in vms.iterdir():
            vbox = folder / f'{folder.name}.vbox'
            if not folder.is_dir() or not vbox.is_file():
                continue
            ports = _forward_ports(vbox)
            if not ports:
                continue
            instances.append(EmulatorInstance(
                type=emu_type, name=folder.name, index=-1,
                serials=[f'127.0.0.1:{p}' for p in ports],
                path=root, exe=exe))
        if instances:
            break
    return instances


def _bluestacks5_instances(exe: Path, root: Path) -> list[EmulatorInstance]:
    """蓝叠 5：注册表 UserDefinedDir -> bluestacks.conf 的各实例 adb_port。"""
    conf_dir = None
    for sub in (r'SOFTWARE\BlueStacks_nxt', r'SOFTWARE\BlueStacks_nxt_cn'):
        key = _reg_open(winreg.HKEY_LOCAL_MACHINE, sub)
        if key is None:
            continue
        try:
            conf_dir = winreg.QueryValueEx(key, 'UserDefinedDir')[0]
        except OSError:
            continue
        break
    if not conf_dir:
        # 注册表没有：试 exe 旁的默认数据目录
        for cand in (root, exe.parent):
            if (cand / 'bluestacks.conf').is_file():
                conf_dir = str(cand)
                break
    if not conf_dir:
        return []
    conf = Path(conf_dir) / 'bluestacks.conf'
    try:
        text = conf.read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return []
    instances = []
    for name, port in _BS5_PORT_RE.findall(text):
        instances.append(EmulatorInstance(
            type=BS5, name=name, index=-1,
            serials=[f'127.0.0.1:{port}'],
            path=root, exe=exe))
    return instances


def _ldplayer_instances(exe: Path, root: Path, emu_type: str) -> list[EmulatorInstance]:
    """雷电：vms/leidian<N> 目录枚举；vbox 无转发配置，端口按 5555 + 2*N 算。"""
    instances = []
    vms = root / 'vms'
    if not vms.is_dir():
        return instances
    for folder in vms.iterdir():
        m = _LD_ID_RE.match(folder.name) if folder.is_dir() else None
        if not m:
            continue
        index = int(m.group(1))
        instances.append(EmulatorInstance(
            type=emu_type, name=folder.name, index=index,
            serials=[f'127.0.0.1:{5555 + 2 * index}'],
            path=root, exe=exe,
            console=_console_exe(emu_type, exe)))
    return instances


def _memu_instances(exe: Path, root: Path) -> list[EmulatorInstance]:
    """逍遥：MemuHyperv VMs/<name>/<name>.memu 的 adb 转发端口（同 vbox 格式）。"""
    instances = []
    vms = root / 'MemuHyperv VMs'
    if not vms.is_dir():
        return instances
    for folder in vms.iterdir():
        memu = folder / f'{folder.name}.memu'
        if not folder.is_dir() or not memu.is_file():
            continue
        ports = _forward_ports(memu)
        if not ports:
            continue
        instances.append(EmulatorInstance(
            type=MEMU, name=folder.name, index=-1,
            serials=[f'127.0.0.1:{p}' for p in ports],
            path=root, exe=exe, console=_console_exe(MEMU, exe)))
    return instances


def _mumux_instances(exe: Path, root: Path) -> list[EmulatorInstance]:
    """MuMu X：vms/nemu-12.0-* 下 .nemu 的 adb 转发端口。"""
    instances = []
    vms = root / 'vms'
    if not vms.is_dir():
        return instances
    for folder in vms.iterdir():
        if not folder.is_dir():
            continue
        ports: list[int] = []
        for file in folder.glob('*.nemu'):
            for p in _forward_ports(file):
                if p not in ports:
                    ports.append(p)
        if ports:
            instances.append(EmulatorInstance(
                type=MUMUX, name=folder.name, index=-1,
                serials=[f'127.0.0.1:{p}' for p in ports],
                path=root, exe=exe))
    return instances


def iter_instances(exe: Path) -> list[EmulatorInstance]:
    """枚举一个模拟器 exe 下的全部实例。"""
    emu_type = path_to_type(exe)
    if emu_type is None:
        return []
    root = _install_dir(emu_type, exe)
    if emu_type == MUMU12:
        console = _console_exe(emu_type, exe)
        return _mumu12_instances(exe, root, console)
    if emu_type in NOX_FAMILY:
        return _nox_instances(exe, root, emu_type)
    if emu_type == BS5:
        return _bluestacks5_instances(exe, root)
    if emu_type == BS4:
        # 蓝叠 4 端口不固定（每次启动递增），硬编码 5555
        return [EmulatorInstance(type=BS4, name='Android', index=-1,
                                 serials=[_BS4_SERIAL], path=root, exe=exe,
                                 console=_console_exe(BS4, exe))]
    if emu_type in LD_FAMILY:
        return _ldplayer_instances(exe, root, emu_type)
    if emu_type == MEMU:
        return _memu_instances(exe, root)
    if emu_type == MUMU6:
        return [EmulatorInstance(type=MUMU6, name='nemu', index=0,
                                 serials=[_MUMU6_SERIAL], path=root, exe=exe)]
    if emu_type == MUMUX:
        return _mumux_instances(exe, root)
    return []


# ---------------------------------------------------------------- 扫描与匹配

def scan_instances() -> list[EmulatorInstance]:
    """全机扫描全部模拟器实例（同一安装目录的多个 exe 只枚举一次）。"""
    instances: list[EmulatorInstance] = []
    seen_dirs: set[str] = set()
    for exe in find_emulator_exes():
        emu_type = path_to_type(exe)
        if emu_type is None:
            continue
        try:
            key = str(_install_dir(emu_type, exe).resolve()).lower()
        except OSError:
            key = str(_install_dir(emu_type, exe)).lower()
        if key in seen_dirs:
            continue
        seen_dirs.add(key)
        instances.extend(iter_instances(exe))
    return instances


def scan_serials() -> list[str]:
    """全部实例的可连 serial（GUI 设备下拉用）。"""
    return [s for inst in scan_instances() for s in inst.serials]


def get_serial_pair(serial: str) -> str:
    """serial 对偶形态：127.0.0.1:5555+X <-> emulator-5554+X（0<=X<=32，ALAS 同）。

    雷电/蓝叠的 serial 会在两种形态间跳动，匹配时两种都要试。
    """
    m = re.match(r'127\.0\.0\.1:(\d+)', serial)
    if m:
        port = int(m.group(1))
        if 5555 <= port <= 5555 + 32:
            return f'emulator-{port - 1}'
    m = re.match(r'emulator-(\d+)', serial)
    if m:
        port = int(m.group(1))
        if 5554 <= port <= 5554 + 32:
            return f'127.0.0.1:{port + 1}'
    return serial


def find_instance(serial: str, emulator: str = '', name: str = '',
                  path: str = '') -> EmulatorInstance | None:
    """按 serial 找所属实例；命中多个时用配置的 类型/实例名称/安装路径 逐级消歧
    （ALAS find_emulator_instance：serial 是主键，其余字段只用于消歧）；
    仍多个（如 MuMu12 每个实例的 .nemu 都转发 7555）再按端口实际监听进程反查。"""
    pair = get_serial_pair(serial)
    hits = [inst for inst in scan_instances()
            if serial in inst.serials or pair in inst.serials]
    if not hits:
        return None
    for cond in (lambda i: i.type == emulator if emulator and emulator != 'auto' else True,
                 lambda i: i.name == name if name else True,
                 lambda i: str(i.path).lower() == str(path).lower().rstrip('\\/')
                 if path else True):
        filtered = [i for i in hits if cond(i)]
        if len(filtered) == 1:
            return filtered[0]
        if filtered:
            hits = filtered
    if len(hits) == 1:
        return hits[0]
    return _match_by_listener(serial, hits)


def _listener_cmdlines(port: int) -> list[tuple[str, str]]:
    """监听指定端口的进程 [(监听地址, 命令行)]（多实例消歧用，查询失败返回空）。"""
    ps = (
        f'Get-NetTCPConnection -LocalPort {port} -State Listen'
        ' -ErrorAction SilentlyContinue | ForEach-Object {'
        ' "{0}`t{1}" -f $_.LocalAddress,'
        ' (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)").CommandLine }'
    )
    try:
        proc = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
            capture_output=True, timeout=15, creationflags=_NO_WINDOW)
        out = proc.stdout.decode('utf-8', 'replace')
    except (OSError, subprocess.TimeoutExpired):
        return []
    entries = []
    for line in out.splitlines():
        addr, _, cmd = line.partition('\t')
        if cmd.strip():
            entries.append((addr.strip(), cmd.strip()))
    return entries


def _match_by_listener(serial: str,
                       hits: list[EmulatorInstance]) -> EmulatorInstance | None:
    """多个实例声称同一 serial 时的最终消歧：查该端口实际监听的进程命令行，
    MuMu12 的 MuMuVMMHeadless.exe --comment 参数就是实例名，据此反查真正
    持有该 serial 的实例。同一端口同时有 0.0.0.0 通配和 127.0.0.1 精确绑定时，
    连接到 127.0.0.1 的流量落在精确绑定上，优先用精确绑定的进程。"""
    m = re.match(r'127\.0\.0\.1:(\d+)', serial)
    if not m:
        return None
    entries = _listener_cmdlines(int(m.group(1)))
    if not entries:
        return None
    exact = [cmd for addr, cmd in entries if addr == '127.0.0.1']
    cmdlines = exact or [cmd for _, cmd in entries]
    matched = [inst for inst in hits
               if any(f'--comment {inst.name}' in cmd for cmd in cmdlines)]
    return matched[0] if len(matched) == 1 else None


# ---------------------------------------------------------------- 停/启

def _run(args: list[str]) -> None:
    subprocess.run(args, capture_output=True, timeout=_CONTROL_TIMEOUT,
                   creationflags=_NO_WINDOW, check=False)


def _kill_process_by_path(exe: Path) -> None:
    """按可执行文件路径杀进程（蓝叠 5 / MuMu 6/X 没有控制台 stop 命令）。"""
    ps = (
        "Get-CimInstance Win32_Process "
        f"| Where-Object {{ $_.ExecutablePath -eq $env:QQPET_EMU_EXE }} "
        "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    env = dict(os.environ, QQPET_EMU_EXE=str(exe))
    subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
                   capture_output=True, timeout=30, creationflags=_NO_WINDOW, env=env)


def _launch(args: list[str]) -> None:
    """拉起模拟器主程序（GUI 程序，不等待；Popen 防止父进程被杀时连带树杀）。"""
    subprocess.Popen(args, close_fds=True)


def launch_instance(inst: EmulatorInstance) -> None:
    """仅启动实例（设备未运行时拉起，不做 stop）。restart_instance 的启动半边。"""
    t = inst.type
    if t == MUMU12:
        # 启动必须走 MuMuManager（MuMuNxMain.exe 是 GUI 单例，并发启动请求会被丢弃）
        manager = str(inst.console or inst.exe)
        _run([manager, 'control', '-v', str(inst.index), 'launch'])
    elif t in LD_FAMILY:
        console = str(inst.console or inst.exe.parent / 'ldconsole.exe')
        _run([console, 'launch', '--index', str(inst.index)])
    elif t in NOX_FAMILY:
        _launch([str(inst.exe), f'-clone:{inst.name}'])
    elif t == BS5:
        _launch([str(inst.exe), '--instance', inst.name])
    elif t == BS4:
        _launch([str(inst.exe), '-vmname', inst.name])
    elif t == MEMU:
        _launch([str(inst.exe), inst.name])
    elif t == MUMU6:
        _launch([str(inst.exe)])
    elif t == MUMUX:
        _launch([str(inst.exe), '-m', inst.name])
    else:
        raise RuntimeError(f'不支持的模拟器类型: {t}')


def restart_instance(inst: EmulatorInstance) -> None:
    """分步停/启实例（stop -> 等进程退出 -> start，比 restart 可控，见 ALAS）。"""
    t = inst.type
    if t == MUMU12:
        manager = str(inst.console or inst.exe)
        _run([manager, 'control', '-v', str(inst.index), 'shutdown'])
    elif t in LD_FAMILY:
        console = str(inst.console or inst.exe.parent / 'ldconsole.exe')
        _run([console, 'quit', '--index', str(inst.index)])
    elif t in NOX_FAMILY:
        _run([str(inst.exe), f'-clone:{inst.name}', '-quit'])
    elif t == BS5:
        _kill_process_by_path(inst.exe)
    elif t == BS4:
        if inst.console:
            _run([str(inst.console), 'quit', '--name', inst.name])
        else:
            _kill_process_by_path(inst.exe)
    elif t == MEMU:
        if inst.console:
            _run([str(inst.console), 'stop', '-n', inst.name])
        else:
            _kill_process_by_path(inst.exe)
    elif t in (MUMU6, MUMUX):
        _kill_process_by_path(inst.exe)
    else:
        raise RuntimeError(f'不支持的模拟器类型: {t}')
    time.sleep(SHUTDOWN_WAIT)
    launch_instance(inst)
