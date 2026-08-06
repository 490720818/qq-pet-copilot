# -*- coding: utf-8 -*-
"""测试 src/locators.py 注册的 xpath 定位在当前页面的命中稳定性。

背景：部分深层 xpath（如 ckj/FL[1]/.../FL[5]）对 uiautomator2 的 xpath 引擎
时有时无（同一份控件树两次解析结果都可能不同，见 status_banner 的排查），
控件树本身也可能随页面状态变化（面板位置/层级漂移）。本工具连设备后在当前
页面连续多轮 dump 控件树，对每个 xpath 同时用两种调用方式测命中：

- live：dev.d.xpath(xp).all()            —— 对应 see() 不带 source 的实时查询
- snapshot：PageSource.find_elements(xp) —— 对应 see() 带 source / see_bounds(source=...) 的快照查询

并统计每轮命中的 bounds，判断是否稳定唯一命中。

运行（手机停在目标页面）：
  python tools/test_locator.py select_box_1 select_box_2 select_box_3
  python tools/test_locator.py status_banner --rounds 20
  python tools/test_locator.py --xpath '//*[@content-desc="map_blank"]/android.widget.FrameLayout[3]/android.widget.FrameLayout[1]'
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from uiautomator2.xpath import PageSource

from src.config import find_adb, load_config
from src.locators import LOCATORS
from src.u2dev import U2Device

DEFAULT_ROUNDS = 10
DEFAULT_INTERVAL = 0.6


def collect_xpaths(names: list[str], raw_xpaths: list[str]) -> list[tuple[str, str]]:
    """把 locator 名展开成 (label, xpath) 列表；未注册的名字直接报错。"""
    items: list[tuple[str, str]] = []
    for name in names:
        entry = LOCATORS.get(name)
        if entry is None:
            raise SystemExit(f'未定义的定位名: {name!r}（请先在 src/locators.py 的 LOCATORS 中登记）')
        paths = entry.get('xpath')
        if not paths:
            print(f'提示: {name!r} 没有 xpath 定位方式（{sorted(entry)}），跳过')
            continue
        for i, xp in enumerate(paths, 1):
            label = name if len(paths) == 1 else f'{name}[{i}]'
            items.append((label, xp))
    for xp in raw_xpaths:
        items.append(('--xpath--', xp))
    return items


def judge(live_hits: int, snap_hits: int, rounds: int,
          live_bounds: list, snap_bounds: list) -> str:
    """按命中率与 bounds 一致性给判定。"""
    if live_hits == 0 and snap_hits == 0:
        return '本页无此元素（稳定 0 命中）'
    if live_hits == rounds and snap_hits == rounds:
        if len(live_bounds) == 1 and len(snap_bounds) == 1 and live_bounds == snap_bounds:
            return '稳定唯一命中'
        return f'每轮都命中但位置漂移（live {len(live_bounds)} 种 / snap {len(snap_bounds)} 种 bounds）'
    return '不稳定（时有时无）'


def main() -> None:
    ap = argparse.ArgumentParser(description='测试 xpath 定位在当前页面的命中稳定性')
    ap.add_argument('names', nargs='*', metavar='LOCATOR_NAME',
                    help='src/locators.py 中登记的定位名，可多个')
    ap.add_argument('--xpath', action='append', default=[], metavar='XPATH',
                    help='直接测试的原始 xpath，可多次传入')
    ap.add_argument('--rounds', type=int, default=DEFAULT_ROUNDS,
                    help=f'连续测试轮数（默认 {DEFAULT_ROUNDS}）')
    ap.add_argument('--interval', type=float, default=DEFAULT_INTERVAL,
                    help=f'每轮间隔秒数（默认 {DEFAULT_INTERVAL}）')
    args = ap.parse_args()

    if not args.names and not args.xpath:
        ap.error('至少提供一个 LOCATOR_NAME 或 --xpath')
    items = collect_xpaths(args.names, args.xpath)
    if not items:
        raise SystemExit('没有可测试的 xpath')

    cfg = load_config()
    dev = U2Device(find_adb(cfg.adb.path), cfg.adb.device_serial)
    time.sleep(1.0)

    # 每轮记录 (live_count, live_bounds, snap_count, snap_bounds)
    history: dict[str, list] = {label: [] for label, _ in items}
    print(f'共 {len(items)} 个 xpath，连续测试 {args.rounds} 轮...\n')

    for r in range(1, args.rounds + 1):
        raw = dev.d.dump_hierarchy()
        src = PageSource.parse(raw)
        parts = []
        for label, xp in items:
            try:
                live = dev.d.xpath(xp).all()
                snap = src.find_elements(xp)
                history[label].append((len(live), [e.bounds for e in live],
                                       len(snap), [e.bounds for e in snap]))
                parts.append(f'{label}: live={len(live)} snap={len(snap)}')
            except Exception as e:
                history[label].append(('ERR', str(e), 'ERR', str(e)))
                parts.append(f'{label}: ERR {e}')
        print(f'round {r:>2}: ' + '   '.join(parts))
        if r < args.rounds:
            time.sleep(args.interval)

    print('\n===== 汇总 =====')
    for label, xp in items:
        rows = history[label]
        if any(rows[i][0] == 'ERR' for i in range(len(rows))):
            print(f'{label}:\n  xpath: {xp}\n  判定: 解析报错\n')
            continue
        live_hits = sum(1 for l, _, _, _ in rows if l)
        snap_hits = sum(1 for _, _, s, _ in rows if s)
        live_bounds = sorted({tuple(b) for l, bs, _, _ in rows for b in bs})
        snap_bounds = sorted({tuple(b) for _, _, s, bs in rows for b in bs})
        print(f'{label}:')
        print(f'  xpath: {xp}')
        print(f'  live    : {live_hits}/{args.rounds} 命中，bounds {live_bounds}')
        print(f'  snapshot: {snap_hits}/{args.rounds} 命中，bounds {snap_bounds}')
        print(f'  判定: {judge(live_hits, snap_hits, args.rounds, live_bounds, snap_bounds)}\n')


if __name__ == '__main__':
    main()
