"""异常恢复：重新进入 QQ 宠物页面。

调度/场景抛异常（设备卡死、u2 连接断开、游戏界面卡死等）时的恢复链路，
按配置 recover.method 二选一：
- 重启设备（默认）：adb reboot -> 等开机完成 -> 亮屏上滑解锁（仅滑动锁屏）-> 启动 QQ；
- 重启游戏：只强停 QQ 再重开（设备不重启，快），适合游戏界面卡死；
之后统一：等待并紧凑双击 Q宠-* 入口进宠物页面（minitouch 两连击，间隔
~0.03s；真机验证 0.3s 会被识别成两次单击，d.click 走 JSON-RPC 单次往返就
可能超窗，必须用 minitouch 压间隔）-> 返回新的 U2Device 连接（旧连接随
重启失效），由调用方刷新各场景的 dev 后继续后续任务。

QQ 宠物入口的 content-desc 形如 "Q宠-1000004"，后缀数字随账号/宠物不固定，
按 descriptionStartsWith 前缀匹配。
"""
from __future__ import annotations

import subprocess
import sys
import time

from .adb.device import Device
from .config import EmulatorConfig
from .emulator import find_instance, launch_instance, restart_instance
from .progress import log
from .opener import OpenPetPageError, open_pet_page
from .u2dev import U2Device

# Windows 下隐藏子进程的命令行窗口
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

QQ_PACKAGE = 'com.tencent.mobileqq'
# QQ 宠物入口的 content-desc 前缀（完整值形如 "Q宠-1000004"，后缀数字不固定）
PET_ENTRY_DESC_PREFIX = 'Q宠-'

BOOT_TIMEOUT = 180.0        # adb reboot 后等开机完成的超时（秒）
BOOT_POLL_INTERVAL = 5.0
EMULATOR_CMD_TIMEOUT = 180.0   # 模拟器重启命令的执行超时（秒）
# adb connect 单次超时（connect_remote 内置 10s）连续多少次才判定 adb server 卡死：
# 模拟器开机慢时偶发超时是正常的，连续多次才需要 kill-server 兜底
CONNECT_TIMEOUT_KILL_AFTER = 3
EMULATOR_BOOT_TIMEOUT = 300.0  # 模拟器重启后等开机完成的超时（秒，冷启动比手机慢）
U2_CONNECT_TIMEOUT = 60.0   # 开机后等 atx-agent 就绪、u2 可连的超时（秒）
U2_CONNECT_INTERVAL = 5.0
PET_ENTRY_TIMEOUT = 120.0   # 启动 QQ 后等 Q宠-* 入口出现的超时（秒）
PET_ENTRY_POLL_INTERVAL = 3.0
PET_ENTRY_CLICK_TRIES = 3   # 点入口后宠物页没出来时的重试点击次数
PET_ENTRY_SETTLE_SECONDS = 0.5  # 找到入口后等页面稳定再点的时间（原 2s，太慢；
                                # 点不进主页有 back 退回重试兜底，不用等那么久）
PET_ENTRY_DOUBLE_CLICK_INTERVAL = 0.03  # 紧凑双击两击间隔（秒）：minitouch 事件
                                        # 本地直发，0.03s 足够压进应用双击窗口
PET_PAGE_TIMEOUT = 15.0    # 每次点击后等宠物主页加载的超时（秒，冷启动可能要十几秒）
PET_PAGE_POLL_INTERVAL = 3.0


def reenter_pet(adb: Device, method: str = "重启设备",
                use_opener: bool = False, opener_serial: str | None = None,
                emulator_restart_cmd: str = "",
                emulator_cfg: "EmulatorConfig | None" = None) -> U2Device:
    """按 recover.method 恢复：重启设备 或 重启游戏，再进宠物页面，返回新 U2Device。

    模拟器模式（use_opener=True）：QQ 搜索卡片的宠物入口是空的（点不到 Q宠-*），
    改用 opener（一次性 SDK 初始化 + root am start 直开）打开宠物主页，由 opener 负责启动 QQ。
    模拟器不支持 adb reboot（MuMu 会把 adb 服务卡死）："重启设备"分支按优先级——
    配置的 emulator_restart_cmd > 自动探测模拟器实例分步停/启（src/emulator.py，
    serial 匹配到多个实例时用 emulator_cfg 的 类型/实例名称/安装路径 消歧）>
    回退 adb reboot（MuMu 会卡死 adb 服务，仅兜底）。
    其余场景点完入口会等宠物主页（"宠物状态"容器）真的加载出来；没出来重新点，
    最多 PET_ENTRY_CLICK_TRIES 次。失败抛异常，由调用方决定再次恢复或放弃。
    """
    if method == "重启游戏":
        # 只重开 QQ，不重启设备（快；设备级卡死/u2 挂掉时治不了）
        log('异常恢复：重启 QQ 游戏（不重启设备）...')
        adb.force_stop_app(QQ_PACKAGE)
        dev = _connect_u2(adb)
    elif use_opener and emulator_restart_cmd.strip():
        # 模拟器不支持 adb reboot：执行配置的重启命令重启模拟器整机
        _restart_emulator(emulator_restart_cmd.strip(), adb)
        dev = _connect_u2(adb)
    elif use_opener and _restart_emulator_auto(adb, emulator_cfg):
        # 自动探测到 MuMu 实例：分步停/启（shutdown -> launch）
        dev = _connect_u2(adb)
    else:
        log('异常恢复：adb reboot 重启设备...')
        adb.reboot_and_wait(BOOT_TIMEOUT, BOOT_POLL_INTERVAL)
        dev = _connect_u2(adb)
        _unlock(dev)
    if use_opener:
        # 模拟器：不点 Q宠-* 入口（搜索卡片空入口），opener 一次性初始化 + am start 直开
        log('异常恢复：模拟器模式，正在打开 QQ 宠物主页...')
        try:
            open_pet_page(serial=opener_serial or adb.serial, adb_path=adb.adb)
        except OpenPetPageError as e:
            # 设备在线但 QQ 没起来（冷启动首启失败等）：先试一次"重启 QQ"（强停再开），
            # 比再整机重启快得多；模拟器重启后 adb 会抖动，设备离线先等回线再重试
            if not _wait_adb_online(adb):
                raise RuntimeError(f'opener 打开宠物主页失败: {e}') from e
            log(f'opener 失败（{e}），设备已在线，改试重启 QQ 一次')
            adb.force_stop_app(QQ_PACKAGE)
            try:
                open_pet_page(serial=opener_serial or adb.serial, adb_path=adb.adb)
            except OpenPetPageError as e2:
                raise RuntimeError(f'opener 打开宠物主页失败: {e2}') from e2
        if _wait_main_page(dev):
            return dev
        raise RuntimeError('opener 打开宠物主页后未检测到主页标志（"宠物状态"容器）')
    log('启动 QQ...')
    adb.launch_app(QQ_PACKAGE)
    for attempt in range(1, PET_ENTRY_CLICK_TRIES + 1):
        _click_pet_entry(dev)
        if _wait_main_page(dev):
            return dev
        log(f'点击入口后宠物主页未出现，重试点击 ({attempt}/{PET_ENTRY_CLICK_TRIES})')
        # 双击可能落在"单击页"（入口已不在当前页）：back 退回 QQ 入口页再重试，
        # 避免在错误页面空等入口出现直到超时；入口还在（点击被吞）则直接重试
        try:
            if not dev.d(descriptionStartsWith=PET_ENTRY_DESC_PREFIX).exists:
                log('未检测到宠物入口（可能进了单击页），按 back 退回')
                dev.d.press('back')
                time.sleep(PET_ENTRY_POLL_INTERVAL)
        except Exception as e:
            log(f'退回单击页失败: {e}')
    raise RuntimeError(f'点击 {PET_ENTRY_CLICK_TRIES} 次宠物入口仍未进入宠物页面')


def _restart_emulator(command: str, adb: Device) -> None:
    """执行配置的模拟器重启命令（MuMu 等模拟器不支持 adb reboot——会把 adb 服务
    卡死），然后等 adb 重新认出设备并开机完成。

    命令由用户在 config.yaml 的 recover.emulator_restart_cmd 配置（如 MuMu 12：
    MuMuManager.exe control -v 0 restart）。
    """
    log(f'异常恢复：重启模拟器（{command}）...')
    try:
        proc = subprocess.run(command, shell=True, capture_output=True,
                              timeout=EMULATOR_CMD_TIMEOUT,
                              creationflags=_NO_WINDOW)
        if proc.returncode != 0:
            log(f'模拟器重启命令返回码 {proc.returncode}: '
                f'{proc.stderr.decode("utf-8", "replace").strip()}')
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f'模拟器重启命令超时（{EMULATOR_CMD_TIMEOUT:.0f}s）: {command}') from None
    _adb_back_online(adb)


def _restart_emulator_auto(adb: Device,
                           emulator_cfg: "EmulatorConfig | None" = None) -> bool:
    """自动探测当前设备所属的模拟器实例并分步停/启（src/emulator.py：
    stop -> 等进程退出 -> start），探测不到返回 False（调用方回退 adb reboot）。"""
    try:
        inst = find_instance(
            adb.serial,
            emulator=emulator_cfg.type if emulator_cfg else '',
            name=emulator_cfg.name if emulator_cfg else '',
            path=emulator_cfg.path if emulator_cfg else '')
    except Exception as e:
        log(f'扫描模拟器实例失败: {e}')
        return False
    if inst is None:
        log(f'未探测到 {adb.serial} 对应的模拟器实例')
        return False
    log(f'异常恢复：重启模拟器实例 {inst.type} {inst.name}（分步停/启）...')
    restart_instance(inst)
    _adb_back_online(adb)
    return True


def launch_emulator_if_offline(adb: Device,
                               emulator_cfg: "EmulatorConfig | None" = None) -> bool:
    """目标 adb 设备不在线时，自动探测所属模拟器实例并启动（只启动，不重启）。

    返回 True = 设备在线（本来就在线或启动成功）；探测不到实例返回 False
    （调用方按原逻辑继续，连接失败再报错）。启动失败（开机超时等）抛异常。
    """
    try:
        if adb.serial in adb.online_devices():
            return True
    except Exception:  # noqa: BLE001 - adb 服务未起时查询失败，按不在线处理
        pass
    if ':' in (adb.serial or ''):
        adb.connect_remote()
        try:
            if adb.serial in adb.online_devices():
                return True
        except Exception:  # noqa: BLE001
            pass
    try:
        inst = find_instance(
            adb.serial,
            emulator=emulator_cfg.type if emulator_cfg else '',
            name=emulator_cfg.name if emulator_cfg else '',
            path=emulator_cfg.path if emulator_cfg else '')
    except Exception as e:
        log(f'扫描模拟器实例失败: {e}')
        return False
    if inst is None:
        log(f'设备 {adb.serial} 不在线，也未探测到所属模拟器实例')
        return False
    log(f'设备 {adb.serial} 不在线，启动模拟器实例 {inst.type} {inst.name}...')
    launch_instance(inst)
    _adb_back_online(adb)
    log(f'模拟器 {inst.name} 启动完成，设备已在线')
    return True


def _wait_adb_online(adb: Device, timeout: float = 45.0) -> bool:
    """等 adb 设备回线（模拟器重启后 adb 会抖动几秒~几十秒）。

    远程串口（host:port）每次轮询前先 adb connect；超时仍未回线返回 False，
    由调用方决定是否整机重启/判失败。"""
    try:
        if adb.serial in adb.online_devices():
            return True
    except Exception:
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if ':' in adb.serial:
                adb.connect_remote()
            if adb.serial in adb.online_devices():
                log(f'设备 {adb.serial} 已回线')
                return True
        except Exception:  # noqa: BLE001 - 抖动期间 connect/devices 失败很正常
            pass
        time.sleep(2)
    log(f'设备 {adb.serial} 在 {timeout:.0f}s 内未回线')
    return False


def _adb_back_online(adb: Device) -> None:
    """模拟器重启后恢复 adb：连接远程端口、轮询等开机完成。

    不默认 adb kill-server：kill-server 会把本机 adb server 上的**所有**设备
    （其它模拟器实例、USB 真机）全部踢下线，而 host:port 是手动 adb connect
    的条目，server 重启不会自动恢复（只有 emulator-* 别名会）——多开时重启
    一个实例会把其它实例的 adb 也弄丢。默认 start-server + adb connect 轮询；
    仅当 adb 服务本身卡死（connect 持续超时）才 kill-server 兜底，且兜底后
    把之前在线过的所有远程设备全部重新 connect。
    """
    subprocess.run([adb.adb, 'start-server'], capture_output=True, timeout=30,
                   creationflags=_NO_WINDOW, check=False)
    # 记录当前在线的远程设备（host:port），kill-server 兜底后要恢复它们
    try:
        known_remote = [s for s in adb.online_devices()
                        if ':' in s and s != adb.serial]
    except Exception:  # noqa: BLE001 - 记录失败不阻塞恢复
        known_remote = []
    deadline = time.monotonic() + EMULATOR_BOOT_TIMEOUT
    killed = False
    timeouts = 0
    while True:
        try:
            adb.connect_remote()
            break
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f'模拟器重启后 adb connect {adb.serial} 持续超时') from None
            timeouts += 1
            if not killed and timeouts >= CONNECT_TIMEOUT_KILL_AFTER:
                # 连续多次超时，疑似 adb server 卡死：kill-server 兜底，并恢复其它远程设备
                log(f'adb connect 连续 {CONNECT_TIMEOUT_KILL_AFTER} 次超时，'
                    f'疑似 adb 服务卡死，kill-server 后重试，并恢复其它远程设备连接')
                subprocess.run([adb.adb, 'kill-server'], capture_output=True, timeout=30,
                               creationflags=_NO_WINDOW, check=False)
                subprocess.run([adb.adb, 'start-server'], capture_output=True, timeout=30,
                               creationflags=_NO_WINDOW, check=False)
                killed = True
                for s in known_remote:
                    subprocess.run([adb.adb, 'connect', s], capture_output=True,
                                   timeout=10, creationflags=_NO_WINDOW, check=False)
            log(f'adb connect {adb.serial} 超时（第 {timeouts} 次），'
                f'模拟器可能还在开机，重试')
            time.sleep(BOOT_POLL_INTERVAL)
    adb.wait_boot_completed(EMULATOR_BOOT_TIMEOUT, BOOT_POLL_INTERVAL)
    log('模拟器重启完成，已开机')


def _click_pet_entry(dev: U2Device) -> None:
    """等 Q宠-* 入口出现并紧凑双击进入。

    真机调试结论：单击只选中不跳转；双击间隔 0.3s 会被识别成两次单击（进单击页），
    间隔必须压进应用很短的双击判定窗口。d.click 走 JSON-RPC（单次往返几十~几百
    ms），两次 click 的物理间隔不可控；改用 minitouch 两连击（本地直发，间隔
    ~0.03s）。点完若主页没出，外层 reenter_pet 会 back 退回入口页重试。
    """
    log(f'等待 QQ 宠物入口（{PET_ENTRY_DESC_PREFIX}*）出现...')
    deadline = time.monotonic() + PET_ENTRY_TIMEOUT
    while True:
        ui = dev.d(descriptionStartsWith=PET_ENTRY_DESC_PREFIX)
        if ui.exists:
            x, y = ui.center()
            log(f'找到 QQ 宠物入口 ({int(x)}, {int(y)})，紧凑双击进入宠物页面')
            time.sleep(PET_ENTRY_SETTLE_SECONDS)  # 入口刚渲染出来时点击无效，短等页面稳定
            _compact_double_click(dev, int(x), int(y))
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f'启动 QQ 后 {PET_ENTRY_TIMEOUT:.0f}s 内未出现宠物入口'
                f'（{PET_ENTRY_DESC_PREFIX}*）')
        time.sleep(PET_ENTRY_POLL_INTERVAL)


def _compact_double_click(dev: U2Device, x: int, y: int) -> None:
    """minitouch 两连击进入宠物入口（间隔 ~0.03s，压进应用双击窗口）。

    用 minitouch 事件（d.touch.down/up）而不是 d.click：d.click 是 JSON-RPC 调用，
    单次往返几十~几百 ms，两次 click 的物理间隔容易超 0.3s，被应用识别成
    两次单击，落在"单击页"而不是宠物主页。
    """
    dev.touch_down(x, y)
    dev.touch_up(x, y)
    time.sleep(PET_ENTRY_DOUBLE_CLICK_INTERVAL)
    dev.touch_down(x, y)
    dev.touch_up(x, y)


def _wait_main_page(dev: U2Device) -> bool:
    """等宠物主页（"宠物状态"容器）出现；QQ/游戏冷启动加载可能要十几秒。"""
    deadline = time.monotonic() + PET_PAGE_TIMEOUT
    while time.monotonic() < deadline:
        if dev.d(description='宠物状态').exists:
            log('已进入宠物页面')
            return True
        time.sleep(PET_PAGE_POLL_INTERVAL)
    return False


def _unlock(dev: U2Device) -> None:
    """开机后若停在待解锁页面：亮屏并上滑解开滑动锁屏。

    只对无密码的滑动锁屏有效；密码/图案锁 adb 层解不开，
    后续等 Q宠-* 入口会超时，恢复失败（日志会体现）。
    """
    dev.d.screen_on()
    w, h = dev.window_size()
    dev.swipe(w // 2, int(h * 0.8), w // 2, int(h * 0.2), duration=0.3)
    time.sleep(1)


def _connect_u2(adb: Device) -> U2Device:
    """开机后 atx-agent 就绪需要几秒，重试直到 u2 可连。"""
    deadline = time.monotonic() + U2_CONNECT_TIMEOUT
    while True:
        try:
            return U2Device(adb.adb, adb.serial)
        except Exception as e:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f'开机后 {U2_CONNECT_TIMEOUT:.0f}s 内 u2 连接失败: {e}') from None
            log(f'u2 暂不可连，{U2_CONNECT_INTERVAL:.0f}s 后重试: {e}')
            time.sleep(U2_CONNECT_INTERVAL)
