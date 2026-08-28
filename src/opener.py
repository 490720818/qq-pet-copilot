"""QQ 宠物模块打开器（模拟器）：MuMu 机型伪装 / 设备门禁本地翻转 + 官方 scheme 跳转，零注入。

问题背景：模拟器（Root + ADB）里手机 QQ 搜索"QQ宠物"卡片不下发跳转地址
（提示"请在手机端使用"），无法手动进入宠物主页。

方案（按隐身性优先，全程不注入 QQ 进程）：
0. MuMu 机型伪装映射改写（ensure_device_spoof，仅 MuMu 生效）：MuMu 的
   /system/etc/mumu-configs/app-device-prop-*.config 把包名映射到机型
   profile，其中 QQ（com.tencent.mobileqq）被故意喂 yyb.config（完整 caas/Intel/
   AOSP/tablet 属性 dump），QQ 因此识别成平板模拟器、宠物门禁直接关闭。把映射
   改成目录里已有的真实手机 profile（含 ro.build.characteristics=default 的，
   如 honor_magic4pro.config）后，QQ 进程重启即按真机身份运行——门禁原生通过，
   搜索"QQ宠物"卡片出现"宠物"功能入口、双击即进宠物主页，与真机完全一致。
   /system 硬只读（USER 固件无 disable-verity），只能 root mount --bind 覆盖，
   **重启模拟器后失效需重挂**（函数幂等，每次 opener 运行自动检查重挂）；
1. 模拟器若不带映射改写（非 MuMu），被 QQ 判成 TABLET
   （ro.build.characteristics 含 tablet），宠物设备门禁 PetQQMC.e() 读
   UnitedConfig 107805 配置的 enable_tablet（服务端下发默认 0）
   直接返回 false，官方跳转（mqqapi://qpet/open scheme / 游戏内"访问"）全被拦截；
2. ensure_gate_open() 用 root 直接改 UnitedConfig 的本地 MMKV 缓存
   （/data/data/.../files/mmkv/united_config_mmkv_<uin>，append-only 追加一条
   enable_tablet=1 的 107805_key_content 记录并重算 CRC），门禁即本地翻转——
   与 QQ 自身读配置的路径完全一致，进程内无任何第三方代码（伪装失败时的兜底）；
3. 之后 `am start -a VIEW -d "mqqapi://qpet/open?version=1&src_type=app&source=1"`
   由 QQ 自己的 JumpActivity 打开宠物主页并初始化宠物 SDK（官方路径），
   游戏内 好友->访问 等跳转也全部恢复正常（与真机一致）。

调度顺序：先试 scheme 直开（伪装/补丁持久，二次运行零写盘）；失败再做 MMKV
补丁（force-stop QQ -> 追加记录 -> 重启 -> scheme）；仍失败回退 frida 一次性
SDK init + am start fragment 直开（注入几秒窗口，伪装名 frida-server）。

frida 兜底路径（旧方案，门禁补丁失效时启用）：伪装名 frida-server 监听
127.0.0.1:随机端口经 adb forward 连接，注入只做一次 qqpet.sdk init 就
unload/detach/杀 server；页面跳转走 root am start fragment
（好友页需 ensure_friend_entry 一次性捕获的 uin+attrs，缓存 runs/friend_entry.json）。

frida-server xz 不随 exe 打包（省 ~32MB 体积）：兜底触发时按提示把
frida-server-<版本>-android-<架构>.xz 放到 exe 旁 runs/resources/frida-server/
（tools/fetch_frida_server.py 可下载；源码运行缺失时自动联网下载）。
Frida 17 起 Java bridge 不再内置在运行时里，注入前用 frida-tools 自带的
frida-java-bridge（frida_tools/bridges/java.js，打包时随包）包一层暴露全局 Java。
"""
from __future__ import annotations

import base64
import json
import lzma
import os
import random
import re
import shlex
import shutil
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import APP_ROOT, find_adb, load_config, resource_path
from .progress import log

QQ_PACKAGE = 'com.tencent.mobileqq'
# 宠物主页 Fragment 与其宿主（frida 兜底时 root am start 直开）
PET_HOST_ACTIVITY = f'{QQ_PACKAGE}/.activity.QPublicFragmentActivity'
PET_MAIN_FRAGMENT = 'com.tencent.mobileqq.qqpet.main.PetMainFragment'
# 官方 scheme：由 QQ 的 JumpActivity（exported）处理，门禁通过即可打开宠物主页
PET_SCHEME = 'mqqapi://qpet/open?version=1&src_type=app&source=1'
# 宠物主页的宿主 Activity：am start fragment 直开落在 QPublicFragmentActivity，
# 官方 scheme 跳转落在 AdelieFragmentActivity，渲染判定两种都认
PET_PAGE_ACTIVITIES = ('QPublicFragmentActivity', 'AdelieFragmentActivity')
# UnitedConfig 本地 MMKV 缓存（设备门禁配置 107805 所在）
MMKV_DIR = f'/data/data/{QQ_PACKAGE}/files/mmkv'
MMKV_PREFIX = 'united_config_mmkv_'
# 宠物设备门禁配置项（107805_key_content JSON 里的 enable_tablet）
GATE_GROUP = '107805'
GATE_CONTENT_KEY = f'{GATE_GROUP}_key_content'
# MuMu app 级机型伪装：映射表目录 / QQ 被喂的原始模拟器 profile / bind 暂存路径
MUMU_PROP_DIR = '/system/etc/mumu-configs'
MUMU_RAW_PROFILE = 'yyb.config'
MUMU_SPOOF_STAGING = '/data/local/tmp/qqpet_adp.config'
# 优先选用的真实手机 profile（实测完整真机属性集且含 characteristics=default）
MUMU_PREFERRED_PROFILE = 'honor_magic4pro.config'
# frida-server 压缩包目录：frida-server-<版本>-android-<架构>.xz
FRIDA_SERVER_REL = Path('resources') / 'frida-server'
# 源码运行时 xz 缺失的自动下载脚本（tools/fetch_frida_server.py）
FETCH_FRIDA_SCRIPT = APP_ROOT / 'tools' / 'fetch_frida_server.py'
# 设备 CPU ABI -> frida-server 的架构名
ARCH_MAP = {'x86_64': 'x86_64', 'x86': 'x86',
            'arm64-v8a': 'arm64', 'armeabi-v7a': 'arm'}

# 设备上的 frida-server 伪装名（不带 frida 字样/版本号，/data/local/tmp 下中性名）
REMOTE_SERVER_NAME = 'perf_daemon'
REMOTE_SERVER_PATH = f'/data/local/tmp/{REMOTE_SERVER_NAME}'
# frida-server 监听端口区间（每次启动随机，避开默认 27042）
FRIDA_PORT_RANGE = (40000, 60000)
# 本地 adb forward 端口区间（低端口 Windows bind 10013，与 minitouch 同策略走高端口）
FORWARD_PORT_RANGE = (22000, 23000)
FORWARD_ATTEMPTS = 8

# 好友入口缓存：一次性捕获的"访问好友"跳转参数（uin + attrs）
FRIEND_ENTRY_FILE = APP_ROOT / 'runs' / 'friend_entry.json'

# 注入后等 hook 回报"已初始化"的超时（秒）
INJECT_TIMEOUT = 25.0
# 启动 QQ 后等进程出现的轮询次数（每次 1 秒）
START_QQ_TRIES = 20
# 冷启动（模拟器刚开机）时首启可能失败/系统未就绪，am start 最多重试轮数
START_QQ_ATTEMPTS = 3
# 注入失败（QQ 冷启动后首次注入偶发闪退/会话断开）时强停 QQ 重试的轮数
OPEN_PET_ATTEMPTS = 3
# 判定为"可重试"的错误特征：QQ 进程退出/frida 会话断开/等待 SDK 超时/主页未渲染
OPEN_RETRY_MARKS = ('script is destroyed', 'QQ 进程退出', '会话断开', 'SDK 初始化超时',
                    '宠物主页未渲染')
# QQ 冷启动后 SplashActivity 要花较长时间完成初始化；过早注入会被 QQ 启动流程干扰。
# 热启动（焦点已离开 SplashActivity）会提前返回，冷启动最久等 QQ_SETTLE_WAIT 秒。
# 只等 3 秒：等不足导致的首跳失败由后续重试/兜底链路（START_QQ_ATTEMPTS /
# OPEN_PET_ATTEMPTS / MMKV 补丁 / frida 兜底）消化，不在这里久等。
QQ_SETTLE_WAIT = 3.0
# am start 打开宠物主页后的渲染确认：焦点在 QPublicFragmentActivity 且画面非黑屏
# （亮度检测，与 u2 的 UiAutomation 无冲突；uiautomator dump 会被 u2 占用误判）
MAIN_PAGE_WAIT_ROUNDS = 6
MAIN_PAGE_WAIT_INTERVAL = 6.0
# 捕获好友入口：点"访问"后等 doJumpAction 事件的超时（秒）
CAPTURE_TIMEOUT = 10.0

# 运行时模拟器模式标记：调度器（scenarios/runner.py 的 run_scheduler）在
# use_opener=True 时置 True，场景（visit 等）据此走 am start 好友入口分支。
EMULATOR_MODE = False
# 设备门禁是否已本地翻转（open_pet_page 成功路径设置）：
# True = scheme/游戏内"访问"跳转可用（visit 走真机式游戏内导航）；
# False = 走了 frida 兜底（门禁仍关），visit 需 am start fragment 直开好友页。
GATE_OPEN = False

# Windows 下隐藏 adb 子进程的命令行窗口（exe 无控制台模式下每次调用都会闪窗）
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0


class OpenPetPageError(RuntimeError):
    """打开 QQ 宠物主页失败（模拟器模式）。"""


def writable_runtime() -> Path:
    """opener 的运行时目录（frida-server 解压缓存）。"""
    path = APP_ROOT / 'runs' / 'opener'
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---- adb 基础 ----

def _adb_run(adb: str, serial: str | None, *args: str,
             check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
    cmd = [adb]
    if serial:
        cmd += ['-s', serial]
    cmd += list(args)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=timeout, creationflags=_NO_WINDOW)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise OpenPetPageError(detail or f'adb 命令失败: {" ".join(cmd)}')
    return proc


def _online_devices(adb: str) -> list[str]:
    proc = _adb_run(adb, None, 'devices', check=False, timeout=30)
    out = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) == 2 and parts[1] == 'device':
            out.append(parts[0])
    return out


def _has_qq(adb: str, serial: str) -> bool:
    proc = _adb_run(adb, serial, 'shell', 'pm', 'path', QQ_PACKAGE, check=False, timeout=30)
    return proc.returncode == 0 and 'package:' in proc.stdout


def _choose_device(adb: str, serial: str | None) -> str:
    """确定要操作的设备序列号：优先用指定的，否则选第一台装了手机 QQ 的在线设备。"""
    if serial:
        if ':' in serial:
            # 模拟器（127.0.0.1:port）可能还没连进 adb，先 connect 一次（失败不阻塞）
            _adb_run(adb, None, 'connect', serial, check=False, timeout=10)
        state = _adb_run(adb, serial, 'get-state', check=False, timeout=15)
        if state.returncode != 0 or 'device' not in state.stdout:
            raise OpenPetPageError(f'设备 {serial} 不在线，请确认模拟器已启动并开启 ADB')
        if not _has_qq(adb, serial):
            raise OpenPetPageError(f'设备 {serial} 未安装手机 QQ（{QQ_PACKAGE}）')
        return serial
    _adb_run(adb, None, 'start-server', check=False, timeout=30)
    for candidate in _online_devices(adb):
        if _has_qq(adb, candidate):
            return candidate
    raise OpenPetPageError('没有找到已安装手机 QQ 的在线设备')


def _has_root(adb: str, serial: str) -> bool:
    """模拟器是否开放 Root（软检查，不抛异常）。"""
    proc = _adb_run(adb, serial, 'shell', 'su', '-c', 'id', check=False, timeout=30)
    return proc.returncode == 0 and 'uid=0' in proc.stdout


def _qq_pid(adb: str, serial: str) -> int | None:
    proc = _adb_run(adb, serial, 'shell', 'pidof', QQ_PACKAGE, check=False, timeout=30)
    out = proc.stdout.split()
    return int(out[0]) if out else None


# ---- frida-server 隐身部署 ----

def _device_arch(adb: str, serial: str) -> str:
    abi = _adb_run(adb, serial, 'shell', 'getprop', 'ro.product.cpu.abi',
                   timeout=30).stdout.strip()
    if abi not in ARCH_MAP:
        raise OpenPetPageError(f'暂不支持模拟器架构: {abi or "未知"}')
    return ARCH_MAP[abi]


def _fetch_frida_server(version: str, arch: str) -> bool:
    """源码运行：调用 tools/fetch_frida_server.py 下载缺失的 xz（联网兜底）。"""
    if not FETCH_FRIDA_SCRIPT.is_file():
        return False
    log(f'正在下载 frida-server {version} ({arch})...')
    try:
        proc = subprocess.run(
            [sys.executable, str(FETCH_FRIDA_SCRIPT), '--version', version, '--arch', arch],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600,
        )
    except Exception as e:
        log(f'自动下载 frida-server 失败: {e}')
        return False
    if proc.returncode != 0:
        log((proc.stderr or proc.stdout).strip() or f'自动下载 frida-server 失败（rc={proc.returncode}）')
        return False
    return True


def _remote_pid(adb: str, serial: str) -> str:
    """设备上伪装名 server 的 pid（空串=没在跑）。pidof 按可执行名精确匹配，
    不用 pkill -f（pattern 会匹配到 su/sh 自己的命令行误杀自身）。"""
    proc = _adb_run(adb, serial, 'shell', f"su -c 'pidof {REMOTE_SERVER_NAME}'",
                    check=False, timeout=30)
    return proc.stdout.strip()


def _ensure_frida_server(adb: str, serial: str) -> int:
    """确保伪装名 frida-server 在跑，返回其监听的随机端口。

    - 二进制改名（REMOTE_SERVER_NAME，不带 frida 字样），已存在且大小一致则不重推；
    - 每次调用先杀掉残留进程再换新的随机端口启动（-l 127.0.0.1:<port>），
      不监听默认 27042（/proc/net/tcp 端口扫描是最常见的 frida 检测点）；
    - 用完由调用方 _kill_frida_server() 杀进程 + 移除 forward，设备上不常驻。

    xz 不随 exe 打包（减小体积）；兜底触发且本地无缓存时给出明确提示，
    下载同名 xz 放到 exe 旁 runs/resources/frida-server/ 即可（源码运行自动联网下载）。
    """
    import frida
    version = frida.__version__
    arch = _device_arch(adb, serial)
    local_binary = writable_runtime() / f'frida-server-{version}-android-{arch}'
    if not local_binary.is_file():
        xz = resource_path(FRIDA_SERVER_REL / f'frida-server-{version}-android-{arch}.xz')
        if not xz.is_file():
            if getattr(sys, 'frozen', False):
                raise OpenPetPageError(
                    f'缺少 frida-server {version} ({arch})。本包不内置 frida-server 离线包'
                    f'（减小体积）；请下载 frida-server-{version}-android-{arch}.xz 放到 '
                    f'{APP_ROOT / "runs" / FRIDA_SERVER_REL} 后重试')
            # 源码运行：联网自动下载（tools/fetch_frida_server.py），失败再给手动提示
            if not _fetch_frida_server(version, arch):
                raise OpenPetPageError(
                    f'自动下载 frida-server {version} ({arch}) 失败。请手动下载 '
                    f'frida-server-{version}-android-{arch}.xz 放到 {APP_ROOT / FRIDA_SERVER_REL} 后重试')
            xz = resource_path(FRIDA_SERVER_REL / f'frida-server-{version}-android-{arch}.xz')
        log(f'解压 frida-server {version} ({arch})...')
        with lzma.open(xz, 'rb') as src, open(local_binary, 'wb') as dst:
            shutil.copyfileobj(src, dst)
    # 已推送且大小一致则跳过重推（二进制几十 MB，adb push 有几秒开销）
    local_size = local_binary.stat().st_size
    remote_size = _adb_run(
        adb, serial, 'shell', f"su -c 'stat -c %s {REMOTE_SERVER_PATH}'",
        check=False, timeout=30).stdout.strip()
    if remote_size != str(local_size):
        log('推送 frida-server 到模拟器（伪装名部署）...')
        _adb_run(adb, serial, 'push', str(local_binary), REMOTE_SERVER_PATH, timeout=180)
        # 模拟器刚开机时 su 可能还没就绪（重启后第一次执行偶发失败），重试几次
        for _chmod_attempt in range(1, 4):
            proc = _adb_run(adb, serial, 'shell', f"su -c 'chmod 755 {REMOTE_SERVER_PATH}'",
                            check=False, timeout=30)
            if proc.returncode == 0:
                break
            if _chmod_attempt < 3:
                log(f'chmod 失败，重试 ({_chmod_attempt}/3)')
                time.sleep(3)
    # 杀残留（上次异常退出留下的旧进程可能占着旧端口），换随机端口重启
    old_pid = _remote_pid(adb, serial)
    if old_pid:
        _adb_run(adb, serial, 'shell', f"su -c 'kill {old_pid}'", check=False, timeout=30)
        time.sleep(0.5)
    port = random.randint(*FRIDA_PORT_RANGE)
    log(f'启动注入服务（127.0.0.1:{port}）...')
    _adb_run(adb, serial, 'shell',
             f"su -c 'nohup {REMOTE_SERVER_PATH} -l 127.0.0.1:{port} >/dev/null 2>&1 &'",
             check=False, timeout=30)
    time.sleep(2)
    if not _remote_pid(adb, serial):
        raise OpenPetPageError('注入服务启动失败（伪装名 frida-server 未在运行）')
    return port


def _kill_frida_server(adb: str, serial: str, local_port: int | None) -> None:
    """init/捕获完成后收尾：杀设备上的 server 进程 + 移除本地 adb forward。"""
    pid = _remote_pid(adb, serial)
    if pid:
        _adb_run(adb, serial, 'shell', f"su -c 'kill {pid}'", check=False, timeout=30)
    if local_port is not None:
        _adb_run(adb, serial, 'forward', '--remove', f'tcp:{local_port}',
                 check=False, timeout=15)


def _connect_frida(adb: str, serial: str, port: int):
    """adb forward 本地随机高端口 -> 设备 127.0.0.1:port，用 frida remote device 连接。

    不走 frida 的 adb 设备枚举（模拟器刚开机时不稳，且枚举要求 server 监听
    默认端口/能被发现）；forward 成功即一定能连上。返回 (device, local_port)。
    """
    import frida
    _ensure_adb_online(adb, serial)
    last_error = ''
    for _ in range(FORWARD_ATTEMPTS):
        local_port = random.randint(*FORWARD_PORT_RANGE)
        proc = _adb_run(adb, serial, 'forward', f'tcp:{local_port}', f'tcp:{port}',
                        check=False, timeout=15)
        if proc.returncode != 0:
            last_error = (proc.stderr or proc.stdout).strip()
            continue  # 本地端口被占（bind 失败），换端口重试
        try:
            mgr = frida.get_device_manager()
            device = mgr.add_remote_device(f'127.0.0.1:{local_port}')
            # add_remote_device 不立即连接，attach 时才握手；这里先探活一次
            device.enumerate_processes()
            return device, local_port
        except Exception as e:
            last_error = str(e)
            _adb_run(adb, serial, 'forward', '--remove', f'tcp:{local_port}',
                     check=False, timeout=15)
            time.sleep(1)
    raise OpenPetPageError(f'frida 连接注入服务失败（adb forward/remote device）: {last_error}')


def _ensure_adb_online(adb: str, serial: str) -> None:
    """frida 连接前确认 adb 设备在线（远程模拟器连接易掉），必要时重连。"""
    for _ in range(3):
        proc = _adb_run(adb, serial, 'get-state', check=False, timeout=15)
        if proc.returncode == 0 and 'device' in (proc.stdout or ''):
            return
        if ':' in serial:
            _adb_run(adb, None, 'connect', serial, check=False, timeout=10)
        time.sleep(1)


# ---- QQ 启动 ----

def _start_qq(adb: str, serial: str) -> int:
    """启动 QQ 并等进程出现；模拟器刚开机时首启可能失败/系统未就绪，
    重试 am start；每轮内先确认设备在线，避免 adb 抖动把 pidof 失败误判成 QQ 没启动。"""
    for attempt in range(1, START_QQ_ATTEMPTS + 1):
        _adb_run(adb, serial, 'shell', 'am', 'start', '-n',
                 f'{QQ_PACKAGE}/.activity.SplashActivity', check=False, timeout=30)
        for _ in range(START_QQ_TRIES):
            pid = _qq_pid(adb, serial)
            if pid:
                return pid
            # 设备离线（模拟器重启后 adb 偶发抖动）：重连后继续等，别误判成 QQ 没启动
            state = _adb_run(adb, serial, 'get-state', check=False, timeout=15)
            if state.returncode != 0 or 'device' not in state.stdout:
                if ':' in serial:
                    _adb_run(adb, None, 'connect', serial, check=False, timeout=10)
            time.sleep(1)
        log(f'QQ 进程未出现，重试启动 QQ ({attempt}/{START_QQ_ATTEMPTS})')
    raise OpenPetPageError('手机 QQ 没有成功启动。请先在模拟器里登录 QQ 后重试。')


def _wait_qq_settle(adb: str, serial: str) -> None:
    """等 QQ 启动稳定再打开宠物页。

    焦点离开 SplashActivity 即认为就绪（热启动很快）；冷启动最久等
    QQ_SETTLE_WAIT 秒。过早跳转/注入会被 QQ 启动流程干扰。
    """
    deadline = time.monotonic() + QQ_SETTLE_WAIT
    while time.monotonic() < deadline:
        focus = _adb_run(adb, serial, 'shell', 'dumpsys window | grep -E "mCurrentFocus"',
                         check=False, timeout=30).stdout or ''
        if 'SplashActivity' not in focus:
            return
        time.sleep(1)
    log(f'等待 {QQ_SETTLE_WAIT:.0f}s 后 QQ 仍在启动页，继续打开流程')


# ---- MuMu 机型伪装映射改写（QQ 进程看到真实手机身份） ----

def _mumu_prop_map_path(adb: str, serial: str) -> str | None:
    """找 MuMu app 级机型伪装映射表（app-device-prop-*.config，不同 Android
    版本文件名不同；多个匹配时取实际包含 QQ 映射行的那个）。"""
    proc = _adb_run(adb, serial, 'shell',
                    f"su -c 'ls {MUMU_PROP_DIR}/app-device-prop-*.config'",
                    check=False, timeout=30)
    paths = [ln.strip() for ln in proc.stdout.splitlines()
             if ln.strip().startswith(MUMU_PROP_DIR) and ln.strip().endswith('.config')]
    for path in paths:
        hit = _adb_run(adb, serial, 'shell',
                       f"su -c 'grep -l mobileqq {path}'",
                       check=False, timeout=30).stdout or ''
        if hit.strip():
            return path
    return None


def ensure_device_spoof(adb: str, serial: str) -> bool:
    """把 MuMu 映射表里 QQ 的机型 profile 从原始模拟器身份换成真实手机。

    MuMu 故意给 QQ 喂 yyb.config（完整 caas/Intel/AOSP/tablet 属性 dump），
    注入机制是 QQ 进程里的 libnemuinitaidl.so / libjavahelper.so 读映射表。
    改成真实手机 profile 后 QQ 进程重启即按真机身份运行，宠物门禁原生通过。
    /system 硬只读（USER 固件无 disable-verity），只能 root mount --bind 覆盖，
    **重启模拟器后失效需重挂**（本函数幂等，每次 opener 运行都检查）。
    非 MuMu 设备 / 已伪装 / 找不到真实手机 profile 时静默返回 False。
    返回 True 且本次新挂载时若 QQ 在跑会 force-stop（让调用方以新身份重启 QQ）。
    """
    map_path = _mumu_prop_map_path(adb, serial)
    if not map_path:
        return False  # 非 MuMu
    content = _adb_run(adb, serial, 'shell', f"su -c 'cat {map_path}'",
                       check=False, timeout=30).stdout or ''
    lines = content.splitlines()
    qq_idx = next((i for i, ln in enumerate(lines) if 'mobileqq' in ln), None)
    if qq_idx is None:
        return False
    parts = lines[qq_idx].split()
    if len(parts) != 2:
        return False
    if parts[1].rsplit('/', 1)[-1] != MUMU_RAW_PROFILE:
        return True  # 已伪装（bind 还活着或本来就是别的 profile），无需重挂
    # 选一个真实手机 profile：必须含 ro.build.characteristics=default
    candidates = _adb_run(
        adb, serial, 'shell',
        f"su -c 'grep -l ro.build.characteristics=default "
        f"{MUMU_PROP_DIR}/device-prop-configs/*.config'",
        check=False, timeout=30).stdout or ''
    choice = None
    for line in candidates.splitlines():
        name = line.strip().rsplit('/', 1)[-1]
        if not name.endswith('.config') or name == MUMU_RAW_PROFILE:
            continue
        if name == MUMU_PREFERRED_PROFILE:  # 实测可用，优先
            choice = name
            break
        if choice is None:
            choice = name
    if not choice:
        log('MuMu 伪装映射存在但找不到真实手机 profile，跳过机型伪装')
        return False
    # 只替换 QQ 行的 profile 路径 basename，保留原路径风格（/etc/... 前缀）
    lines[qq_idx] = f'{parts[0]}    {parts[1].rsplit("/", 1)[0]}/{choice}'
    staging_local = writable_runtime() / 'qqpet_adp.config'
    staging_local.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    _adb_run(adb, serial, 'push', str(staging_local), MUMU_SPOOF_STAGING, timeout=30)
    proc = _adb_run(adb, serial, 'shell',
                    f"su -c 'mount --bind {MUMU_SPOOF_STAGING} {map_path}'",
                    check=False, timeout=30)
    if proc.returncode != 0:
        log(f'MuMu 机型伪装 bind-mount 失败: {(proc.stderr or proc.stdout).strip()}')
        return False
    # 校验生效（bind 后读到的应是新内容）
    after = _adb_run(adb, serial, 'shell', f"su -c 'cat {map_path}'",
                     check=False, timeout=30).stdout or ''
    if choice not in after:
        log('MuMu 机型伪装 bind-mount 后校验失败（内容未变化）')
        return False
    log(f'MuMu 机型伪装已启用：QQ -> {choice}（重启模拟器需重挂，opener 自动检查）')
    # QQ 在跑则带着旧身份，强停让调用方以新身份重启
    if _qq_pid(adb, serial):
        _adb_run(adb, serial, 'shell', 'am', 'force-stop', QQ_PACKAGE,
                 check=False, timeout=30)
    return True


# ---- 设备门禁本地翻转（UnitedConfig MMKV 补丁） ----

def _read_varint(buf: bytes | bytearray, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _write_varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _mmkv_last_string(payload: bytes, key: str) -> str | None:
    """在 MMKV payload 里找 key 的最后一条 string 值（append-only，最后一条生效）。

    文件布局：主文件 = [uint32 LE actualSize][payload]；item =
    varint(keyLen)+key+varint(valueLen)+valueBuffer，string 的 valueBuffer =
    varint(strLen)+bytes；开头有一个 0xFFFFFF 哨兵（4 字节标记无值）。
    """
    found = None
    pos = 0
    while pos < len(payload):
        klen, pos = _read_varint(payload, pos)
        if klen == 0xFFFFFF:
            continue
        k = payload[pos:pos + klen].decode('utf-8', 'replace')
        pos += klen
        vlen, pos = _read_varint(payload, pos)
        val = payload[pos:pos + vlen]
        pos += vlen
        if k == key and vlen > 4:
            n, vpos = _read_varint(val, 0)
            found = val[vpos:vpos + n].decode('utf-8', 'replace')
    return found


def _mmkv_encode_string_item(key: str, value: str) -> bytes:
    kb = key.encode()
    vb = value.encode()
    buf = _write_varint(len(vb)) + vb
    return _write_varint(len(kb)) + kb + _write_varint(len(buf)) + buf


def _su_read_bytes(adb: str, serial: str, path: str) -> bytes | None:
    """root 读设备私有文件（base64 过 shell 防二进制损坏）。"""
    proc = _adb_run(adb, serial, 'shell', f"su -c 'base64 {shlex.quote(path)}'",
                    check=False, timeout=60)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return base64.b64decode(proc.stdout)
    except Exception:
        return None


def _account_uin(adb: str, serial: str) -> str | None:
    """从 MMKV 文件名发现当前登录账号（united_config_mmkv_<uin>，排除 000）。"""
    proc = _adb_run(adb, serial, 'shell', f"su -c 'ls {MMKV_DIR}'", check=False, timeout=30)
    for m in re.finditer(rf'{MMKV_PREFIX}(\d+)', proc.stdout or ''):
        if m.group(1) != '000':
            return m.group(1)
    return None


def _gate_needs_patch(adb: str, serial: str, uin: str) -> bool:
    """门禁配置里 enable_tablet 是否为 0（需要补丁）。读不出配置时按不需要处理
    （让 scheme 先试试，真不行再走 frida 兜底）。"""
    data = _su_read_bytes(adb, serial, f'{MMKV_DIR}/{MMKV_PREFIX}{uin}')
    if not data or len(data) < 8:
        return False
    actual = struct.unpack_from('<I', data, 0)[0]
    if actual <= 0 or 4 + actual > len(data):
        return False
    content = _mmkv_last_string(bytes(data[4:4 + actual]), GATE_CONTENT_KEY)
    if content is None:
        return False
    return '"enable_tablet": 0' in content or '"enable_tablet":0' in content


def _patch_gate_mmkv(adb: str, serial: str, uin: str) -> bool:
    """把 107805 配置的 enable_tablet 改成 1（追加新记录 + 重算 CRC）。

    调用方必须已 force-stop QQ（MMKV 有 mmap，运行中改会被覆盖/不一致）。
    返回是否补丁成功；任何格式不符都放弃（不冒损坏配置的风险），交给 frida 兜底。
    """
    remote = f'{MMKV_DIR}/{MMKV_PREFIX}{uin}'
    data = _su_read_bytes(adb, serial, remote)
    meta = _su_read_bytes(adb, serial, remote + '.crc')
    if not data or not meta or len(data) < 8 or len(meta) < 32:
        log('门禁补丁：MMKV 文件读取失败，跳过')
        return False
    actual = struct.unpack_from('<I', data, 0)[0]
    if actual <= 0 or 4 + actual > len(data):
        log('门禁补丁：actualSize 非法，跳过')
        return False
    payload = bytes(data[4:4 + actual])
    content = _mmkv_last_string(payload, GATE_CONTENT_KEY)
    if content is None:
        log('门禁补丁：没有 107805 配置，跳过')
        return False
    # CRC 元文件格式自检（crc32(payload) 在 offset 0、actualSize 在 offset 28），
    # 版本不符就不改，避免 QQ 加载时校验失败丢弃全部 UnitedConfig
    crc_now = zlib.crc32(payload) & 0xFFFFFFFF
    if struct.unpack_from('<I', meta, 0)[0] != crc_now \
            or struct.unpack_from('<I', meta, 28)[0] != actual:
        log('门禁补丁：MMKV CRC 元文件格式不符，跳过')
        return False
    new_content = re.sub(r'"enable_tablet"\s*:\s*0', '"enable_tablet": 1', content)
    if new_content == content:
        return True  # 已经是 1
    item = _mmkv_encode_string_item(GATE_CONTENT_KEY, new_content)
    new_actual = actual + len(item)
    if 4 + new_actual > len(data):
        log('门禁补丁：MMKV 文件无剩余空间，跳过')
        return False
    patched = bytearray(data)
    patched[4 + actual:4 + new_actual] = item
    struct.pack_into('<I', patched, 0, new_actual)
    new_meta = bytearray(meta)
    struct.pack_into('<I', new_meta, 0, zlib.crc32(bytes(patched[4:4 + new_actual])) & 0xFFFFFFFF)
    struct.pack_into('<I', new_meta, 28, new_actual)
    # 经 /sdcard 中转写回（adb push 二进制安全，cp 替换后还原属主/权限）
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        local_dat = Path(td) / 'uc.dat'
        local_crc = Path(td) / 'uc.crc'
        local_dat.write_bytes(patched)
        local_crc.write_bytes(new_meta)
        _adb_run(adb, serial, 'push', str(local_dat), '/sdcard/qqpet_uc.dat', timeout=120)
        _adb_run(adb, serial, 'push', str(local_crc), '/sdcard/qqpet_uc.crc', timeout=60)
    proc = _adb_run(
        adb, serial, 'shell',
        f"su -c 'cp /sdcard/qqpet_uc.dat {remote} && cp /sdcard/qqpet_uc.crc {remote}.crc"
        f" && chmod 770 {remote} {remote}.crc"
        f" && chown `stat -c %u:%g {MMKV_DIR}` {remote} {remote}.crc'",
        check=False, timeout=60)
    _adb_run(adb, serial, 'shell', 'rm -f /sdcard/qqpet_uc.dat /sdcard/qqpet_uc.crc',
             check=False, timeout=15)
    if proc.returncode != 0:
        log(f'门禁补丁：写回失败 {proc.stderr or proc.stdout}')
        return False
    log('门禁补丁：UnitedConfig 107805 enable_tablet 已改为 1（本地 MMKV 翻转设备门禁）')
    return True


def ensure_gate_open(adb: str, serial: str) -> bool:
    """确保宠物设备门禁本地翻转（需要时补丁 MMKV 并重启 QQ）。

    返回 True = 门禁已开（scheme/游戏内跳转可用）。补丁需要重启 QQ 生效，
    本函数内部 force-stop 并重新启动 QQ、等到启动稳定。
    """
    uin = _account_uin(adb, serial)
    if not uin:
        log('门禁检查：未找到登录账号的 UnitedConfig 缓存（QQ 未登录？）')
        return False
    if not _gate_needs_patch(adb, serial, uin):
        return True
    log('检测到设备门禁拦截（平板误判，enable_tablet=0），执行本地翻转...')
    _adb_run(adb, serial, 'shell', 'am', 'force-stop', QQ_PACKAGE, check=False, timeout=30)
    time.sleep(1)
    if not _patch_gate_mmkv(adb, serial, uin):
        return False
    _start_qq(adb, serial)
    _wait_qq_settle(adb, serial)
    return True


# ---- 官方 scheme 跳转打开宠物页 ----

def _open_pet_via_scheme(adb: str, serial: str) -> None:
    """官方 scheme 跳转打开宠物主页（JumpActivity，普通 shell 即可，无需 root）。"""
    _adb_run(adb, serial, 'shell', 'am', 'start', '-a', 'android.intent.action.VIEW',
             '-d', PET_SCHEME, check=False, timeout=30)


def _wait_main_page_scheme(adb: str, serial: str, rounds: int = MAIN_PAGE_WAIT_ROUNDS) -> bool:
    """scheme 打开宠物主页并等渲染（焦点在 QPublicFragmentActivity 且非黑屏）。

    与 _wait_main_page 同判定，但每轮重发 scheme 而不是 am start fragment。
    """
    for attempt in range(1, rounds + 1):
        _open_pet_via_scheme(adb, serial)
        time.sleep(MAIN_PAGE_WAIT_INTERVAL)
        if _main_page_rendered(adb, serial):
            return True
        log(f'宠物主页未渲染，重试 scheme 跳转 ({attempt}/{rounds})')
    return False


# ---- 注入脚本（frida 兜底路径） ----

def _java_bridge_source() -> str:
    """读取 frida-java-bridge（frida-tools 自带，Frida 17 不再内置 Java 桥）。

    源码/打包都优先从 frida_tools 包读取（版本随 frida-tools 走）；
    打包（frozen）时由 spec 把 frida_tools/bridges/java.js 作为数据带上。
    """
    try:
        import frida_tools
        path = Path(frida_tools.__file__).resolve().parent / 'bridges' / 'java.js'
    except ImportError:
        path = resource_path(Path('frida_tools') / 'bridges' / 'java.js')
    if not path.is_file():
        raise OpenPetPageError(
            f'找不到 frida-java-bridge（{path}）。请安装 requirements.txt 里的 frida-tools'
            f'（提供 Java 桥），或重新打包模拟器版')
    return path.read_text(encoding='utf-8')


def _wrap_java_bridge(bridge: str) -> str:
    """把 java.js（内容是 var bridge = function(){...}();）包一层，暴露为全局 Java。

    与 frida-tools REPL 的 frida:load-bridge 做法一致：在 IIFE 里求值 bridge 源码，
    再把返回值挂到 globalThis.Java，之后的注入脚本就能直接用 Java.perform() 等。
    """
    return (
        "'use strict';\n"
        "(function () {\n"
        + bridge + "\n"
        "  globalThis.Java = bridge;\n"
        "})();\n"
    )


# 最小化 SDK 初始化脚本：只读 uin + 反射调宠物 SDK init，不 hook 任何 QQ 方法、
# 不开页面（页面由 am start 拉起）。Function1 回调用 JDK Proxy 实现
# （不在 QQ 包名下注册类；registerClass 的 InvocationHandler 用中性名）。
_INIT_JS = r"""
setImmediate(function () {
  Java.perform(function () {
    function report(event, detail) {
      send({ event: event, detail: String(detail || '') });
    }

    function currentUin() {
      try {
        const MobileQQ = Java.use('mqq.app.MobileQQ');
        const app = MobileQQ.sMobileQQ.value.waitAppRuntime(null);
        const value = String(app.getCurrentAccountUin());
        if (/^\d{5,12}$/.test(value)) return value;
      } catch (_) {}

      let found = '';
      try {
        Java.choose('com.tencent.mobileqq.app.QQAppInterface', {
          onMatch: function (app) {
            if (!found) found = String(app.getCurrentAccountUin());
          },
          onComplete: function () {}
        });
      } catch (_) {}
      if (!/^\d{5,12}$/.test(found)) {
        throw new Error('无法读取当前登录 QQ，请确认已进入 QQ 主界面');
      }
      return found;
    }

    try {
      const Function1 = Java.use('kotlin.jvm.functions.Function1');
      const Sdk = Java.use('com.tencent.mobileqq.qqpet.sdk.a');
      const uin = currentUin();
      report('account', uin);

      const sdkClass = Sdk.class;
      const singleton = sdkClass.getDeclaredField('a');
      singleton.setAccessible(true);
      const sdk = singleton.get(null);
      const initMethod = sdkClass.getDeclaredMethod(
        'd', Java.array('java.lang.Class', [Function1.class])
      );
      initMethod.setAccessible(true);

      const Proxy = Java.use('java.lang.reflect.Proxy');
      const InvocationHandler = Java.use('java.lang.reflect.InvocationHandler');
      const Handler = Java.registerClass({
        name: 'qqpet.InitSignal',
        implements: [InvocationHandler],
        methods: {
          invoke: function (proxy, method, args) {  // noqa: ARG001
            if (String(method.getName()) === 'invoke') report('inited', 'ok');
            return null;
          }
        }
      });
      const callback = Proxy.newProxyInstance(
        Function1.class.getClassLoader(),
        Java.array('java.lang.Class', [Function1.class]),
        Handler.$new()
      );
      initMethod.invoke(sdk, Java.array('java.lang.Object', [callback]));
      report('init_called', 'ok');
    } catch (error) {
      report('error', error.stack || error);
    }
  });
});
"""

# 好友入口捕获脚本：只记录 doJumpAction 的 qpet/open URL（不改返回值，
# 模拟器上 doAction 本来就返回 false 不跳页），拿到 uin/attrs 后长期复用。
_CAPTURE_JS = r"""
setImmediate(function () {
  Java.perform(function () {
    try {
      const JumpApi = Java.use('com.tencent.mobileqq.jump.api.impl.JumpApiImpl');
      JumpApi.doJumpAction.overloads.forEach(function (ov) {
        ov.implementation = function () {
          try {
            for (let i = 0; i < arguments.length; i++) {
              const x = arguments[i];
              const s = x === null ? '' : String(x);
              if (s.indexOf('mqqapi://qpet/open') >= 0) {
                send({ event: 'jump', detail: s });
              }
            }
          } catch (_) {}
          return ov.apply(this, arguments);
        };
      });
      send({ event: 'ready' });
    } catch (error) {
      send({ event: 'error', detail: String(error.stack || error) });
    }
  });
});
"""


def _attach_script(device, pid: int, js: str):
    """attach QQ 并加载脚本，返回 (session, script, got)。

    got 是事件字典（on_message 异步写入 {event: detail}），调用方轮询；
    用完必须 _detach_script()。console.log 转发到项目日志。
    """
    session = device.attach(pid)
    try:
        script = session.create_script(_wrap_java_bridge(_java_bridge_source()) + js)
    except Exception:
        try:
            session.detach()
        except Exception:
            pass
        raise
    got: dict[str, str] = {}

    def on_message(message, data):  # noqa: ARG001 - data 不用
        mtype = message.get('type')
        if mtype == 'send':
            payload = message.get('payload') or {}
            event = payload.get('event')
            detail = str(payload.get('detail') or '')
            if event:
                got[event] = detail
        elif mtype == 'error':
            got['error'] = message.get('stack') or message.get('description') or str(message)

    def on_log(level: str, text: str) -> None:
        log(f'[frida:{level}] {text}')

    script.on('message', on_message)
    script.set_log_handler(on_log)
    try:
        script.load()
    except Exception as e:
        try:
            session.detach()
        except Exception:
            pass
        raise OpenPetPageError(f'注入 QQ 失败（注入服务是否已运行、版本是否匹配？）: {e}') from e
    return session, script, got


def _detach_script(session, script) -> None:
    """解除注入（一次性脚本，不常驻）。对 destroyed script 调 unload 会抛
    "script is destroyed" 掩盖真实原因，忽略异常。"""
    try:
        script.unload()
    except Exception:
        pass
    try:
        session.detach()
    except Exception:
        pass


def _inject_script(device, pid: int, js: str, want_events: tuple[str, ...],
                   timeout: float = INJECT_TIMEOUT) -> dict[str, str]:
    """attach QQ 并注入脚本，等到 want_events 之一或 error；返回 {event: detail}。

    无论成败都 unload/detach（一次性注入，不常驻）。返回的 dict 里
    'error' 键由调用方判定失败。
    """
    session, script, got = _attach_script(device, pid, js)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if 'error' in got or any(e in got for e in want_events):
                break
            time.sleep(0.2)
        return got
    finally:
        _detach_script(session, script)


# ---- am start 直开宠物页 ----

def am_start_pet_page(adb: str, serial: str, uin: str, attrs: dict | None = None) -> None:
    """root am start 拉起 QPublicFragmentActivity + PetMainFragment。

    前提：宠物 SDK 已在当前 QQ 进程初始化（open_pet_page 的一次性 init），
    否则页面黑屏。attrs 为捕获的"访问好友"跳转参数（好友页必须带上，
    pageData 传 attrs JSON——真机实测 '{}' 会导致好友页底部好友列表只加载默认几个）。
    """
    args = ['am', 'start', '-n', PET_HOST_ACTIVITY,
            '--es', 'public_fragment_class', PET_MAIN_FRAGMENT,
            '--es', 'petUin', str(uin),
            '--ez', 'from_adopt', 'false',
            '--ei', 'adopt_closing_pose_id', '0']
    if attrs:
        page = {str(k): str(v) for k, v in attrs.items()}
        page['uin'] = str(uin)
        # pageData 不带空格（separators 紧凑），减少 shell 引号嵌套的坑
        page_data = json.dumps(page, ensure_ascii=False, separators=(',', ':'))
        args += ['--es', 'pageData', page_data]
        for k, v in page.items():
            args += ['--es', k, v]
    else:
        args += ['--es', 'pageData', '{}']
    am_cmd = ' '.join(shlex.quote(a) for a in args)
    _adb_run(adb, serial, 'shell', f'su -c {shlex.quote(am_cmd)}', timeout=30)


def _screen_brightness(adb: str, serial: str) -> float | None:
    """全屏平均亮度（0-255）：exec-out screencap 二进制直传，host 侧解码取灰度均值。
    黑屏（Fragment 未渲染）≈ 0-10，渲染后的宠物主页 >100。"""
    cmd = [adb, '-s', serial, 'exec-out', 'screencap', '-p']
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30, creationflags=_NO_WINDOW)
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    import io
    import numpy as np
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(proc.stdout)).convert('L')
    except Exception:
        return None
    return float(np.asarray(img, dtype=np.uint8).mean())


def _main_page_rendered(adb: str, serial: str) -> bool:
    """当前屏幕是否已渲染宠物主页：焦点在宠物宿主 Activity（PET_PAGE_ACTIVITIES）
    且画面非黑屏。

    不用 uiautomator dump 检测（金币胶囊）：调度器顺序下 u2 已持有 UiAutomation，
    另一个 uiautomator dump 注册不上会一直误判黑屏；亮度检测与 u2 无冲突。
    """
    focus = _adb_run(adb, serial, 'shell', 'dumpsys window | grep -E "mCurrentFocus"',
                     check=False, timeout=30).stdout or ''
    if not any(a in focus for a in PET_PAGE_ACTIVITIES):
        return False
    brightness = _screen_brightness(adb, serial)
    return brightness is not None and brightness > 30


def _wait_main_page(adb: str, serial: str, uin: str) -> None:
    """am start 打开自己的宠物主页并等渲染完成（焦点正确且画面非黑屏）。

    首轮黑屏/未渲染会重试 am start（SDK init 收尾需要时间，实测第二轮命中）。
    """
    for attempt in range(1, MAIN_PAGE_WAIT_ROUNDS + 1):
        am_start_pet_page(adb, serial, uin)
        time.sleep(MAIN_PAGE_WAIT_INTERVAL)
        if _main_page_rendered(adb, serial):
            return
        log(f'宠物主页未渲染，重试 am start ({attempt}/{MAIN_PAGE_WAIT_ROUNDS})')
    raise OpenPetPageError('宠物主页未渲染（SDK 初始化后 am start 多次重试仍黑屏）')


# ---- 好友入口捕获与缓存 ----

def _load_friend_entry() -> dict | None:
    try:
        entry = json.loads(FRIEND_ENTRY_FILE.read_text(encoding='utf-8'))
    except Exception:
        return None
    if entry.get('uin') and isinstance(entry.get('attrs'), dict) and entry['attrs']:
        return entry
    return None


def _save_friend_entry(entry: dict) -> None:
    FRIEND_ENTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FRIEND_ENTRY_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, FRIEND_ENTRY_FILE)


def invalidate_friend_entry() -> None:
    """好友入口缓存失效（am start 进好友页超时）时删除，触发下次重新捕获。"""
    try:
        FRIEND_ENTRY_FILE.unlink()
    except FileNotFoundError:
        pass


def _click_desc(dev, desc: str) -> bool:
    """用 u2 点指定 content-desc 的控件（按 bounds 中心，走项目的 click 分派）。"""
    try:
        els = dev.d.xpath(f'//*[@content-desc="{desc}"]').all()
    except Exception:
        return False
    if not els:
        return False
    left, top, right, bottom = els[0].bounds
    dev.click(int((left + right) / 2), int((top + bottom) / 2))
    return True


def _capture_friend_entry(adb: str, serial: str, dev) -> dict:
    """一次性捕获"访问好友"跳转参数：短暂注入捕获脚本 -> 点 好友 -> 访问 -> 拿 URL。"""
    pid = _qq_pid(adb, serial)
    if not pid:
        raise OpenPetPageError('QQ 未在运行，无法捕获好友入口')
    port = _ensure_frida_server(adb, serial)
    device, local_port = _connect_frida(adb, serial, port)
    session = script = None
    try:
        session, script, got = _attach_script(device, pid, _CAPTURE_JS)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and 'ready' not in got and 'error' not in got:
            time.sleep(0.2)
        if 'error' in got:
            raise OpenPetPageError(f'捕获脚本注入失败: {got["error"]}')
        if 'ready' not in got:
            raise OpenPetPageError('捕获脚本注入超时（QQ 版本不受支持？）')
        # 点 好友 打开好友面板，再点 访问 触发 doJumpAction（被门禁拦截但 URL 已捕获）
        if not _click_desc(dev, '好友'):
            raise OpenPetPageError('未找到"好友"按钮（请确认已在宠物主页）')
        time.sleep(3)
        got.clear()
        if not _click_desc(dev, '访问'):
            raise OpenPetPageError('未找到"访问"按钮（好友面板未打开？）')
        deadline = time.monotonic() + CAPTURE_TIMEOUT
        while time.monotonic() < deadline and 'jump' not in got:
            time.sleep(0.3)
        url = got.get('jump', '')
        if not url:
            raise OpenPetPageError('未捕获到好友跳转 URL（点"访问"无反应？）')
        # 收掉好友面板，让随后的 am start 直开在干净的页面上
        try:
            dev.d.press('back')
            time.sleep(1)
        except Exception:
            pass
    finally:
        if script is not None:
            _detach_script(session, script)
        _kill_frida_server(adb, serial, local_port)
    query = parse_qs(urlparse(url).query)
    attrs = {k: v[0] for k, v in query.items()}
    uin = attrs.get('uin', '')
    if not uin:
        raise OpenPetPageError(f'跳转 URL 中没有 uin: {url}')
    entry = {'uin': uin, 'attrs': attrs, 'captured_at': int(time.time())}
    _save_friend_entry(entry)
    log(f'已捕获好友入口: uin={uin}（缓存到 {FRIEND_ENTRY_FILE.name} 长期复用）')
    return entry


def ensure_friend_entry(adb: str, serial: str, dev) -> dict:
    """取好友入口参数（uin + attrs）：优先读 runs/friend_entry.json 缓存，
    缺失时用一次性短暂注入捕获（点一次 好友->访问）。

    dev: U2Device（点 好友/访问 用项目的点击分派，避免 d.click JSON-RPC 偶发失效）。
    """
    entry = _load_friend_entry()
    if entry:
        return entry
    log('好友入口缓存缺失，执行一次性捕获（点 好友->访问 抓跳转参数）...')
    return _capture_friend_entry(adb, serial, dev)


# ---- 入口 ----

def _open_pet_via_injection(adb: str, serial: str) -> bool:
    """frida 兜底：伪装名 server + 一次性 SDK init + root am start fragment 直开。

    注入窗口只有几秒（init 成功立即 unload/detach/杀 server），QQ 进程内不留
    常驻 hook。只在门禁本地翻转失败（MMKV 格式变化等）时启用。
    """
    try:
        import frida  # noqa: F401 - 仅校验依赖存在
    except ImportError:
        raise OpenPetPageError(
            f'未安装 frida（门禁补丁兜底必需，与 frida-server 同版本）。'
            f'请先执行: .venv/Scripts/pip install -r requirements.txt') from None
    for attempt in range(1, OPEN_PET_ATTEMPTS + 1):
        local_port = None
        try:
            port = _ensure_frida_server(adb, serial)
            pid = _start_qq(adb, serial)
            log(f'已找到手机 QQ 进程: {pid}，等待 QQ 启动稳定再注入...')
            _wait_qq_settle(adb, serial)
            device, local_port = _connect_frida(adb, serial, port)
            got = _inject_script(device, pid, _INIT_JS, ('inited',))
            uin = got.get('account', '')
            if 'error' in got:
                raise OpenPetPageError(str(got['error']))
            if 'inited' not in got:
                raise OpenPetPageError(
                    '等待 QQ 宠物 SDK 初始化超时。请确认 QQ 已登录、版本受支持（当前按 '
                    'QQ9.3.25 验证），并已停留在 QQ 主界面')
            if not uin:
                raise OpenPetPageError('SDK 已初始化但未读到当前登录 QQ 号')
            log(f'宠物 SDK 已初始化（当前 QQ: {uin}），解除注入并停止注入服务')
            _kill_frida_server(adb, serial, local_port)
            local_port = None
            _wait_main_page(adb, serial, uin)
            log('成功：QQ 宠物主页已打开（frida 兜底：一次性初始化 + intent 直开）')
            return True
        except OpenPetPageError as e:
            _kill_frida_server(adb, serial, local_port)
            if attempt >= OPEN_PET_ATTEMPTS or not any(m in str(e) for m in OPEN_RETRY_MARKS):
                raise
            log(f'opener 失败（{e}），强停 QQ 后重试 ({attempt}/{OPEN_PET_ATTEMPTS})')
            _adb_run(adb, serial, 'shell', 'am', 'force-stop', QQ_PACKAGE,
                     check=False, timeout=30)
            time.sleep(2)
        except Exception as e:
            _kill_frida_server(adb, serial, local_port)
            raise OpenPetPageError(f'打开 QQ 宠物主页失败: {e}') from e
    raise OpenPetPageError('打开 QQ 宠物主页失败')  # 不可达，重试循环内已 raise


def open_pet_page(serial: str | None = None, adb_path: str | None = None) -> bool:
    """打开 QQ 宠物主页（模拟器模式）；失败抛 OpenPetPageError。

    三级流程（按隐身性优先，**Root 按需**）：
    0. MuMu 机型伪装映射改写（ensure_device_spoof，启动 QQ 前执行，幂等，
       需 Root 且配置项 emulator.device_spoof 开启——默认关闭，门禁已翻转过的
       设备无需开启）：
       QQ 进程按真实手机身份运行，门禁原生通过，搜索卡片"宠物"入口也出现；
    1. scheme 直开（mqqapi://qpet/open，官方 JumpActivity 路径，零写盘零注入
       **零权限**，伪装成功或门禁已翻转过的设备直接成功——MMKV 补丁写在 QQ
       数据盘，重启/关 Root 都不丢，只有 QQ 服务端重推 107805 配置才会覆盖）；
    2. MMKV 门禁补丁（force-stop QQ -> 107805 配置 enable_tablet 改 1 -> 重启 ->
       scheme，需 Root），补丁持久，后续运行都走第 1 级（可永久关闭 Root）；
    3. frida 兜底：一次性 SDK init + root am start fragment 直开（门禁补丁
       失效时，如 QQ 大改版 MMKV 格式变化，需 Root）。

    adb_path / serial 默认取项目 config.yaml 的 adb.path / adb.device_serial
    （与整个项目一致）；显式传入时以传入为准。
    serial 仍为 None 时自动选第一台装了手机 QQ 的在线设备。
    """
    global GATE_OPEN
    GATE_OPEN = False
    cfg = None
    if adb_path is None or serial is None:
        # 至少有一个参数没传时，用项目配置补默认值
        try:
            cfg = load_config()
        except Exception as e:
            raise OpenPetPageError(f'读取 config.yaml 失败: {e}') from e
    if adb_path is None:
        try:
            adb = find_adb(cfg.adb.path)
        except Exception as e:
            raise OpenPetPageError(f'找不到 adb: {e}') from e
    else:
        adb = adb_path
    if serial is None:
        serial = cfg.adb.device_serial or None
    serial = _choose_device(adb, serial)
    log(f'已连接模拟器: {serial}')
    if cfg is None:
        # 两个参数都显式传入时补读一次配置拿机型伪装开关（读失败按关闭处理）
        try:
            cfg = load_config()
        except Exception:
            cfg = None
    spoof = bool(cfg and getattr(cfg.emulator, 'device_spoof', False))
    has_root = _has_root(adb, serial)
    if has_root:
        if spoof:
            ensure_device_spoof(adb, serial)  # MuMu 机型伪装（QQ 启动前，幂等重挂）
        else:
            log('MuMu 机型伪装已关闭（emulator.device_spoof=false），跳过')
    else:
        log('模拟器未开放 Root：跳过机型伪装/门禁补丁，直接 scheme 直开'
            '（门禁已翻转过的设备不受影响）')
    _start_qq(adb, serial)
    _wait_qq_settle(adb, serial)
    # 第 1 级：scheme 直开（少轮探测，失败尽快进补丁流程）
    log('尝试官方 scheme 跳转打开宠物主页（mqqapi://qpet/open）...')
    if _wait_main_page_scheme(adb, serial, rounds=3):
        GATE_OPEN = True
        log('成功：QQ 宠物主页已打开（官方 scheme 跳转，零注入）')
        return True
    if not has_root:
        raise OpenPetPageError(
            'scheme 直开失败且模拟器未开放 Root，无法执行门禁补丁。'
            '请在模拟器设置中开启 Root 后重试一次（补丁持久，之后可永久关闭 Root）。')
    # 第 2 级：MMKV 门禁补丁后重试 scheme
    if ensure_gate_open(adb, serial) and _wait_main_page_scheme(adb, serial):
        GATE_OPEN = True
        log('成功：QQ 宠物主页已打开（门禁本地翻转 + 官方 scheme 跳转，零注入）')
        return True
    # 第 3 级：frida 一次性 init 兜底
    log('scheme 路径失败，回退 frida 一次性初始化...')
    return _open_pet_via_injection(adb, serial)
