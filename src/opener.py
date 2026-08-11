"""QQ 宠物模块打开器（模拟器）集成：绕过模拟器 QQ 搜索卡片空入口打开宠物主页。

问题背景：模拟器（Root + ADB）里手机 QQ 搜索"QQ宠物"卡片可能不下发跳转地址
（提示"请在手机端使用"），无法手动进入宠物主页。本模块用 Frida 注入已登录的
QQ 进程，初始化 QQ 自带宠物 SDK 并直接打开 PetMainFragment，页面打开后立即解除注入。

与上游 qqpet-module-opener（https://github.com/yikehuang/qqpet-module-opener）的边界：
- 只保留它的 hook JS：assets/qqpet-module-opener/open_qqpet_module.js（QQ 更新导致
  类名/方法变化时从这里手动同步上游最新版本），其余上游代码一律不引入；
- 设备发现 / Root 检查 / frida-server 部署 / 启动 QQ / 注入 全部由本模块实现；
- frida-server 默认离线打包 x86_64（resources/frida-server/*.xz，本地构建时放入，
  打包进模拟器版 exe；其他架构需自行下载同名 xz 放到 exe 旁 runs/ 或重新打包）。
- Frida 17 起 Java/ObjC/Swift bridge 不再内置在运行时里（脚本里没有全局 Java），
  这里在注入前把 frida-tools 自带的 frida-java-bridge（frida_tools/bridges/java.js）
  包一层暴露为全局 Java，再拼接 hook JS，与上游用 frida CLI 的效果一致。
"""
from __future__ import annotations

import lzma
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import APP_ROOT, find_adb, load_config, resource_path
from .progress import log

QQ_PACKAGE = 'com.tencent.mobileqq'
HOOK_REL = Path('assets') / 'qqpet-module-opener' / 'open_qqpet_module.js'
# frida-server 压缩包目录：frida-server-<版本>-android-<架构>.xz
FRIDA_SERVER_REL = Path('resources') / 'frida-server'
# 源码运行时 xz 缺失的自动下载脚本（tools/fetch_frida_server.py）
FETCH_FRIDA_SCRIPT = APP_ROOT / 'tools' / 'fetch_frida_server.py'
# 设备 CPU ABI -> frida-server 的架构名
ARCH_MAP = {'x86_64': 'x86_64', 'x86': 'x86',
            'arm64-v8a': 'arm64', 'armeabi-v7a': 'arm'}

# 注入后等 hook 回报"已打开"的超时（秒）
INJECT_TIMEOUT = 25.0
# 启动 QQ 后等进程出现的轮询次数（每次 1 秒）
START_QQ_TRIES = 20
# QQ 冷启动后 SplashActivity 要花较长时间完成初始化；过早注入打开宠物页会被
# QQ 启动流程顶回主界面（模拟器 + 后台保活下尤其明显），等稳定后再注入。
# 热启动（焦点已离开 SplashActivity）会提前返回，冷启动最久等 QQ_SETTLE_WAIT 秒。
QQ_SETTLE_WAIT = 12.0

# 保持注入的存活引用：frida 的 Script/Session 对象被 GC 会解除注入。
# 模拟器模式需要频繁好友访问（踩踩/PK），doAction 接管必须持续生效，
# 所以打开宠物主页后保持注入，这里持有引用不释放；QQ 重启后由下一次
# open_pet_page 重新注入（_prune_dead_sessions 清理旧会话）。
_KEEPALIVE: list = []

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


def _require_root(adb: str, serial: str) -> None:
    proc = _adb_run(adb, serial, 'shell', 'su', '-c', 'id', check=False, timeout=30)
    if proc.returncode != 0 or 'uid=0' not in proc.stdout:
        raise OpenPetPageError('模拟器未开放 Root 权限。Frida 无法注入 QQ，请先在模拟器设置中开启 Root。')


# ---- frida-server 部署 ----

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


def _ensure_frida_server(adb: str, serial: str) -> None:
    """确保模拟器上 frida-server 在跑：优先用本地（随包/用户放置）的 xz 解压，离线可用。

    当前默认只打包 x86_64；其他架构需自行下载同名 xz 放到 exe 旁
    runs/resources/frida-server/ 或重新打包，找不到时给出明确提示。
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
                    f'缺少 frida-server {version} ({arch})。本包默认只内置 x86_64 离线版；'
                    f'请下载 frida-server-{version}-android-{arch}.xz 放到 exe 旁 runs 目录 '
                    f'{APP_ROOT / "runs" / FRIDA_SERVER_REL}（或重新打包）后重试')
            # 源码运行：联网自动下载（tools/fetch_frida_server.py），失败再给手动提示
            if not _fetch_frida_server(version, arch):
                raise OpenPetPageError(
                    f'自动下载 frida-server {version} ({arch}) 失败。请手动下载 '
                    f'frida-server-{version}-android-{arch}.xz 放到 {APP_ROOT / FRIDA_SERVER_REL} 后重试')
            xz = resource_path(FRIDA_SERVER_REL / f'frida-server-{version}-android-{arch}.xz')
        log(f'解压 frida-server {version} ({arch})...')
        with lzma.open(xz, 'rb') as src, open(local_binary, 'wb') as dst:
            shutil.copyfileobj(src, dst)
    remote = f'/data/local/tmp/frida-server-{version}'
    log(f'推送 frida-server 到模拟器...')
    _adb_run(adb, serial, 'push', str(local_binary), remote, timeout=180)
    _adb_run(adb, serial, 'shell', f"su -c 'chmod 755 {remote}'", timeout=30)
    running = _adb_run(adb, serial, 'shell', f"su -c 'pidof frida-server-{version}'",
                       check=False, timeout=30)
    if not running.stdout.strip():
        log(f'启动 frida-server...')
        _adb_run(adb, serial, 'shell', f"su -c 'nohup {remote} >/dev/null 2>&1 &'",
                 check=False, timeout=30)
        time.sleep(2)


# ---- QQ 启动与注入 ----

def _start_qq(adb: str, serial: str) -> int:
    _adb_run(adb, serial, 'shell', 'am', 'start', '-n',
             f'{QQ_PACKAGE}/.activity.SplashActivity', check=False, timeout=30)
    for _ in range(START_QQ_TRIES):
        proc = _adb_run(adb, serial, 'shell', 'pidof', QQ_PACKAGE, check=False, timeout=30)
        text = proc.stdout.strip()
        if text:
            return int(text.split()[0])
        time.sleep(1)
    raise OpenPetPageError('手机 QQ 没有成功启动。请先在模拟器里登录 QQ 后重试。')


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
    再把返回值挂到 globalThis.Java，之后的 hook JS 就能直接用 Java.perform() 等。
    """
    return (
        "'use strict';\n"
        "(function () {\n"
        + bridge + "\n"
        "  globalThis.Java = bridge;\n"
        "})();\n"
    )


def _wait_qq_settle(adb: str, serial: str) -> None:
    """等 QQ 启动稳定再注入。

    焦点离开 SplashActivity 即认为就绪（热启动很快）；冷启动最久等
    QQ_SETTLE_WAIT 秒。过早注入打开宠物页会被 QQ 启动流程顶回主界面。
    """
    deadline = time.monotonic() + QQ_SETTLE_WAIT
    while time.monotonic() < deadline:
        focus = _adb_run(adb, serial, 'shell', 'dumpsys window | grep -E "mCurrentFocus"',
                         check=False, timeout=30).stdout or ''
        if 'SplashActivity' not in focus:
            return
        time.sleep(1)
    log(f'等待 {QQ_SETTLE_WAIT:.0f}s 后 QQ 仍在启动页，继续尝试注入')


def _hook_source() -> str:
    """组合后的注入脚本 = java bridge 包装 + QQ 宠物 hook JS。"""
    path = resource_path(HOOK_REL)
    if not path.is_file():
        raise OpenPetPageError(f'缺少 hook 脚本: {path}')
    hook = path.read_text(encoding='utf-8')
    return _wrap_java_bridge(_java_bridge_source()) + hook


def _prune_dead_sessions() -> None:
    """清理已断开的注入会话（QQ 被重启/杀进程后旧 session/script 失效）。"""
    keep = []
    for session, script in _KEEPALIVE:
        try:
            if session.is_detached or script.is_destroyed:
                continue
        except Exception:
            continue
        keep.append((session, script))
    _KEEPALIVE[:] = keep


def _frida_device(serial: str):
    """按序列号取 frida 设备；get_device 找不到时枚举 adb 设备兜底。"""
    import frida
    try:
        return frida.get_device(serial, timeout=10)
    except Exception:
        for candidate in frida.enumerate_devices():
            if candidate.id == serial or candidate.name == serial:
                return candidate
        raise OpenPetPageError(
            f'frida 找不到设备 {serial}。请确认模拟器 ADB 已连接、已 Root，'
            f'且 frida-server 已在模拟器上运行')


def _frida_open_module(serial: str, pid: int) -> None:
    """用 frida Python API 把 hook 注入 QQ 进程，打开宠物主页后保持注入。

    hook JS 通过 send({event, detail}) 回报 account / opened / visited / error 事件，
    等待 opened 或 error 决定成败；console.log 由 set_log_handler 转发到项目日志。
    成功后不解除注入（好友访问/踩踩/PK 的 doAction 接管需要持续生效），
    持有引用防 GC；QQ 重启后由下一次 open_pet_page 重新注入。
    """
    import frida
    device = _frida_device(serial)
    session = device.attach(pid)
    script = session.create_script(_hook_source())
    opened = False
    error = ''

    def on_message(message, data):  # noqa: ARG001 - data 不用
        nonlocal opened, error
        mtype = message.get('type')
        if mtype == 'send':
            payload = message.get('payload') or {}
            event = payload.get('event')
            detail = str(payload.get('detail') or '')
            if event == 'account':
                log('已读取当前 ' + detail)
            elif event == 'opened':
                opened = True
            elif event == 'visited':
                # 好友访问（踩踩/PK）跳转被 hook 接管并打开目标宠物页
                log('好友访问: ' + detail)
            elif event == 'error':
                if opened:
                    log(f'hook 运行中报错: {detail}')
                else:
                    error = detail
        elif mtype == 'error':
            msg = message.get('stack') or message.get('description') or str(message)
            if opened:
                log(f'hook 运行中报错: {msg}')
            else:
                error = msg

    def on_log(level: str, text: str) -> None:
        # hook 里 console.log 的 [QQPET_*] 行，转发到项目日志便于排查
        log(f'[frida:{level}] {text}')

    script.on('message', on_message)
    script.set_log_handler(on_log)
    try:
        script.load()
    except Exception as e:
        # 常见原因：frida-server 未运行/版本不匹配、QQ 进程已退出
        raise OpenPetPageError(f'注入 QQ 失败（frida-server 是否已运行、版本是否匹配？）: {e}') from e
    deadline = time.monotonic() + INJECT_TIMEOUT
    while time.monotonic() < deadline and not opened and not error:
        time.sleep(0.2)
    if error or not opened:
        # 失败/超时：解除注入
        try:
            script.unload()
        finally:
            try:
                session.detach()
            except Exception:
                pass
        if error:
            raise OpenPetPageError(error)
        raise OpenPetPageError(
            '等待 QQ 宠物 SDK 初始化超时。请确认 QQ 已登录、版本受支持（当前按 '
            f'QQ9.3.35 验证），并已停留在 QQ 主界面')
    # 成功：保持注入存活（doAction 接管持续生效），持有引用防止被 GC 解除
    _prune_dead_sessions()
    _KEEPALIVE.append((session, script))
    log('hook 已保持注入（好友访问/踩踩/PK 持续可用）')


def open_pet_page(serial: str | None = None, adb_path: str | None = None) -> bool:
    """打开 QQ 宠物主页（模拟器模式）；失败抛 OpenPetPageError。

    adb_path / serial 默认取项目 config.yaml 的 adb.path / adb.device_serial
    （与整个项目一致）；显式传入时以传入为准。
    serial 仍为 None 时自动选第一台装了手机 QQ 的在线设备。
    """
    try:
        import frida
    except ImportError:
        raise OpenPetPageError(
            f'未安装 frida（模拟器模式必需，与 frida-server 同版本）。'
            f'请先执行: .venv/Scripts/pip install -r requirements.txt') from None
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
    try:
        serial = _choose_device(adb, serial)
        log(f'已连接模拟器: {serial}')
        _require_root(adb, serial)
        _ensure_frida_server(adb, serial)
        pid = _start_qq(adb, serial)
        log(f'已找到手机 QQ 进程: {pid}，等待 QQ 启动稳定再注入...')
        _wait_qq_settle(adb, serial)
        _frida_open_module(serial, pid)
        log('成功：QQ 宠物主页已打开，hook 保持注入中')
        return True
    except OpenPetPageError:
        raise
    except Exception as e:
        raise OpenPetPageError(f'打开 QQ 宠物主页失败: {e}') from e
