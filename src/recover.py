"""异常恢复：重新进入 QQ 宠物页面。

调度/场景抛异常（设备卡死、u2 连接断开、游戏界面卡死等）时的恢复链路，
按配置 recover.method 二选一：
- 重启设备（默认）：adb reboot -> 等开机完成 -> 亮屏上滑解锁（仅滑动锁屏）-> 启动 QQ；
- 重启游戏：只强停 QQ 再重开（设备不重启，快），适合游戏界面卡死；
之后统一：等待并紧凑双击 Q宠-* 入口进宠物页面（间隔 0.05s，真机验证 0.3s
会被识别成两次单击）-> 返回新的 U2Device 连接（旧连接随重启失效），
由调用方刷新各场景的 dev 后继续后续任务。

QQ 宠物入口的 content-desc 形如 "Q宠-1000004"，后缀数字随账号/宠物不固定，
按 descriptionStartsWith 前缀匹配。
"""
from __future__ import annotations

import time

from .adb.device import Device
from .progress import log
from .opener import OpenPetPageError, open_pet_page
from .u2dev import U2Device

QQ_PACKAGE = 'com.tencent.mobileqq'
# QQ 宠物入口的 content-desc 前缀（完整值形如 "Q宠-1000004"，后缀数字不固定）
PET_ENTRY_DESC_PREFIX = 'Q宠-'

BOOT_TIMEOUT = 180.0        # adb reboot 后等开机完成的超时（秒）
BOOT_POLL_INTERVAL = 5.0
U2_CONNECT_TIMEOUT = 60.0   # 开机后等 atx-agent 就绪、u2 可连的超时（秒）
U2_CONNECT_INTERVAL = 5.0
PET_ENTRY_TIMEOUT = 120.0   # 启动 QQ 后等 Q宠-* 入口出现的超时（秒）
PET_ENTRY_POLL_INTERVAL = 3.0
PET_ENTRY_CLICK_TRIES = 3   # 点入口后宠物页没出来时的重试点击次数
PET_PAGE_TIMEOUT = 15.0    # 每次点击后等宠物主页加载的超时（秒，冷启动可能要十几秒）
PET_PAGE_POLL_INTERVAL = 3.0


def reenter_pet(adb: Device, method: str = "重启设备",
                use_opener: bool = False, opener_serial: str | None = None) -> U2Device:
    """按 recover.method 恢复：重启设备 或 重启游戏，再进宠物页面，返回新 U2Device。

    模拟器模式（use_opener=True）：QQ 搜索卡片的宠物入口是空的（点不到 Q宠-*），
    改用 qqpet-module-opener（frida 注入）打开宠物主页，由 opener 负责启动 QQ。
    其余场景点完入口会等宠物主页（"宠物状态"容器）真的加载出来；没出来重新点，
    最多 PET_ENTRY_CLICK_TRIES 次。失败抛异常，由调用方决定再次恢复或放弃。
    """
    if method == "重启游戏":
        # 只重开 QQ，不重启设备（快；设备级卡死/u2 挂掉时治不了）
        log('异常恢复：重启 QQ 游戏（不重启设备）...')
        adb.force_stop_app(QQ_PACKAGE)
        dev = _connect_u2(adb)
    else:
        log('异常恢复：adb reboot 重启设备...')
        adb.reboot_and_wait(BOOT_TIMEOUT, BOOT_POLL_INTERVAL)
        dev = _connect_u2(adb)
        _unlock(dev)
    if use_opener:
        # 模拟器：不点 Q宠-* 入口（搜索卡片空入口），frida 注入直接打开宠物主页
        log('异常恢复：模拟器模式，用 qqpet-module-opener 打开 QQ 宠物主页...')
        try:
            open_pet_page(serial=opener_serial or adb.serial, adb_path=adb.adb)
        except OpenPetPageError as e:
            raise RuntimeError(f'opener 打开宠物主页失败: {e}') from e
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
    raise RuntimeError(f'点击 {PET_ENTRY_CLICK_TRIES} 次宠物入口仍未进入宠物页面')


def _click_pet_entry(dev: U2Device) -> None:
    """等 Q宠-* 入口出现并紧凑双击进入。

    真机调试结论：单击只选中不跳转；双击间隔 0.3s 会被识别成两次单击，
    间隔必须压到 0.05s（应用的双击判定窗口很短）。
    """
    log(f'等待 QQ 宠物入口（{PET_ENTRY_DESC_PREFIX}*）出现...')
    deadline = time.monotonic() + PET_ENTRY_TIMEOUT
    while True:
        ui = dev.d(descriptionStartsWith=PET_ENTRY_DESC_PREFIX)
        if ui.exists:
            x, y = ui.center()
            log(f'找到 QQ 宠物入口 ({int(x)}, {int(y)})，2s 后紧凑双击进入宠物页面')
            time.sleep(2)  # 入口刚渲染出来时点击无效，等页面稳定再点
            dev.click(int(x), int(y))
            time.sleep(0.05)
            dev.click(int(x), int(y))
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f'启动 QQ 后 {PET_ENTRY_TIMEOUT:.0f}s 内未出现宠物入口'
                f'（{PET_ENTRY_DESC_PREFIX}*）')
        time.sleep(PET_ENTRY_POLL_INTERVAL)


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
