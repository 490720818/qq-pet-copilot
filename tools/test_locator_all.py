# -*- coding: utf-8 -*-
"""自动导航全场景，测试 src/locators.py 里每个 xpath 是否仍能命中。

与 tools/test_locator.py 的区别：后者只测“当前页面”，本工具会尝试自动导航到
所有不需要真正开始任务的页面（主页、宠物状态、喂食/洗澡面板、好友列表、好友家、
PK 准备页、出门页、学校/打工/冒险准备页），默认用控件树快照直接查询；
需要时可用 --live 额外做 d.xpath 实时查询，避免 see() 的 OCR/rel 兜底掩盖 xpath 失效。

不会点击会触发计数/消费的按钮（school_start / work_start / adventure_start /
pk_start / visit_step / one_click_care 都不会点），因此不会污染 runs/*.json 进度。
只有“进行中/结算/被雇佣/低属性弹窗/一键护理确认”这类必须真正开始任务才能看到的
页面无法自动覆盖，会标成 unverified，可把手机停在对应页面后用 --stage current 补测。

用法：
  python tools/test_locator_all.py                     # 跑全部自动可达阶段
  python tools/test_locator_all.py --stage current     # 只测当前页面（人工停在任意状态）
  python tools/test_locator_all.py --stages main,school,work
  python tools/test_locator_all.py --rounds 3 --json runs/locator_report.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import APP_ROOT, find_adb, load_config
from src.locators import LOCATORS, _bounds_cache, _locate_cache
from src.scenario import CLICK_INTERVAL, NAV_TIMEOUT, DeviceScenario
from src.u2dev import U2Device
from scenarios.care import CareScenario
from scenarios.pk import PKScenario
from scenarios.visit import VisitScenario

DEFAULT_ROUNDS = 2
DEFAULT_INTERVAL = 0.8

# 这些 xpath 只在“真正开始任务 / 等待结算 / 低属性弹窗”等状态出现，自动导航无法到达。
UNVERIFIED_NAMES = {
    'school_in', 'school_end', 'work_in', 'work_end', 'adventure_in', 'adventure_end',
    'pk_in', 'pk_end', 'pk_again', 'employed_in', 'employed_come_back',
    'employed_come_back_confirm', 'employed_end', 'encourage_pet',
    'adventure_detail', 'adventure_recall_confirm', 'pet_low_energy', 'pet_low_clean',
    'one_click_pay', 'go_care',
}

# 这些 xpath 是否出现取决于游戏状态（是否毕业、是否缺饼干/香皂、是否有一键护理按钮等），
# 阶段成功但没命中不能直接判定失效。
CONDITIONAL_NAMES = {
    'school_start', 'school_graduated', 'school_graduate_close', 'work_start',
    'adventure_start', 'pk_start', 'one_click_care', 'care_region',
    'exchange_food', 'buy_soap', 'exchange_pay', 'hire', 'visit',
    'visit_friend_item', 'visit_step', 'visit_stepped',
}

# 每个 locator 预期会出现的自动阶段；用于区分“真的 miss”和“没机会验证”。
EXPECTED_STAGES = {
    'main_sign': ['main'],
    'leave_home': ['main'],
    'pet_status': ['main', 'status', 'feed', 'shower'],
    'back': ['out', 'school', 'work', 'adventure', 'friend_list', 'friend_home', 'pk_ready'],
    'quit': ['out', 'school', 'work', 'adventure'],
    'select_box_container': ['school', 'work'],
    'school': ['out'],
    'school_start': ['school'],
    'school_graduated': ['school'],
    'school_graduate_close': ['school'],
    'town': ['out'],
    'work_start': ['work'],
    'work_outworker': ['work'],
    'adventure': ['out'],
    'adventure_start': ['adventure'],
    'visit_friends': ['main', 'friend_list'],
    'visit': ['friend_list'],
    'visit_friend_item': ['friend_list'],
    'visit_step': ['friend_home', 'pk_ready'],
    'visit_stepped': ['friend_home'],
    'pk': ['friend_home'],
    'pk_start': ['pk_ready'],
    'hire': ['friend_home'],
    'status_region': ['status', 'feed', 'shower'],
    'feed': ['status', 'feed', 'feed_popup'],
    'feed_10': ['feed', 'feed_popup'],
    'exchange_food': ['feed', 'feed_popup'],
    'exchange_pay': ['feed_popup', 'shower_popup'],
    'cookie_5': ['feed_popup'],
    'soap_2': ['shower_popup'],
    'one_click_care': ['main', 'friend_home'],
    'care_region': ['main', 'friend_home'],
    'buy_soap': ['shower', 'shower_popup'],
    'shower': ['status', 'shower', 'shower_popup'],
    'shower_10': ['shower', 'shower_popup'],
}

DERIVED_NAMES = ('select_box_1', 'select_box_2', 'select_box_3')


class StageSkipped(Exception):
    """阶段因当前游戏状态不需要而跳过（不算失败）。"""


def iter_xpath_items() -> list[tuple[str, str, str]]:
    """展开 LOCATORS 为 (label, locator_name, xpath)。"""
    items: list[tuple[str, str, str]] = []
    for name, entry in LOCATORS.items():
        paths = entry.get('xpath') or []
        for idx, xp in enumerate(paths, 1):
            label = name if len(paths) == 1 else f'{name}[{idx}]'
            items.append((label, name, xp))
    return items


def clear_caches() -> None:
    """清空 locators 模块级缓存，避免上一页面命中影响当前页面测试。"""
    _locate_cache.clear()
    _bounds_cache.clear()


class XpathProbe:
    def __init__(self, items: list[tuple[str, str, str]]):
        self.items = items

    def run(self, dev: U2Device, source, force_live: bool = False) -> dict:
        result = {}
        for label, _name, xp in self.items:
            row = {'snapshot_hits': 0, 'live_hits': 0, 'bounds': [], 'error': None}
            try:
                if source is not None:
                    els = source.find_elements(xp)
                    row['snapshot_hits'] = len(els)
                    row['bounds'].extend(tuple(e.bounds) for e in els)
                if force_live or source is None:
                    live_els = dev.d.xpath(xp).all()
                    row['live_hits'] = len(live_els)
                    row['bounds'].extend(tuple(e.bounds) for e in live_els)
            except Exception as e:  # noqa: BLE001 - 单条 xpath 报错不影响其他 xpath
                row['error'] = f'{type(e).__name__}: {e}'
            row['bounds'] = sorted({tuple(int(v) for v in b) for b in row['bounds']})
            result[label] = row
        return result


class LocatorAllTester:
    STAGE_KEYS = (
        'main', 'status', 'feed', 'feed_popup', 'shower', 'shower_popup',
        'friend_list', 'friend_home', 'pk_ready', 'out', 'school', 'work',
        'adventure', 'current',
    )

    STAGE_TITLES = {
        'main': '主页',
        'status': '宠物状态面板',
        'feed': '喂食面板',
        'feed_popup': '兑换食物弹窗（按需）',
        'shower': '洗澡面板',
        'shower_popup': '购买洗澡道具弹窗（按需）',
        'friend_list': '好友列表',
        'friend_home': '好友家',
        'pk_ready': 'PK 准备页',
        'out': '出门页',
        'school': '学校面板',
        'work': '打工面板',
        'adventure': '冒险准备页',
        'current': '当前页面（手动）',
    }

    def __init__(self, dev: U2Device, rounds: int, interval: float, force_live: bool):
        self.dev = dev
        self.rounds = rounds
        self.interval = interval
        self.force_live = force_live
        self.probe = XpathProbe(iter_xpath_items())
        self.scen = DeviceScenario(dev)
        self.care = CareScenario(dev)
        self.visit = VisitScenario(dev)
        self.pk = PKScenario(dev)
        self.stage_state: dict[str, dict] = {}
        self.stage_probe: dict[str, dict] = {}
        self.stage_derived: dict[str, dict] = {}
        self.xpath_seen: dict[str, list[str]] = {label: [] for label, _, _ in self.probe.items}

    # ---- 通用导航辅助 ----

    def _wait_see(self, scen, names: list[str], attempts: int = NAV_TIMEOUT,
                  interval: float = CLICK_INTERVAL):
        for _ in range(attempts):
            source = self.dev.hierarchy()
            for name in names:
                hit = scen.see(name, None, source)
                if hit:
                    return name, hit, source
            time.sleep(interval)
        return None, None, None

    def _click_until_see(self, scen, click_name: str, wait_names: list[str], stage: str,
                         attempts: int = NAV_TIMEOUT) -> str:
        clicked = False
        for _attempt in range(1, attempts + 1):
            source = self.dev.hierarchy()
            for name in wait_names:
                if scen.see(name, None, source):
                    return name
            hit = scen.see(click_name, None, source)
            if hit:
                scen.click(hit[0], hit[1])
                clicked = True
            elif not clicked and _attempt in (1, attempts):
                print(f'  [{stage}] 未找到 {click_name}，等待重试 ({_attempt}/{attempts})')
            time.sleep(CLICK_INTERVAL)
        raise RuntimeError(f'{stage}: 点击 {click_name} 后未出现 {"/".join(wait_names)}')

    # ---- 阶段进入/退出 ----

    def _enter_status_panel(self) -> None:
        self.scen.ensure_main_page()
        self.care.toggle_status()
        found, _, _ = self._wait_see(self.care, ['status_region', 'feed', 'shower'], attempts=5)
        if not found:
            raise RuntimeError('展开宠物状态后未识别到状态面板')

    def _close_care_panels(self, stage: str) -> None:
        if self.stage_state.get(stage, {}).get('status') not in ('ok', 'skipped'):
            return
        for _ in range(6):
            source = self.dev.hierarchy()
            if not any(self.care.see(n, None, source)
                       for n in ('feed_10', 'shower_10', 'exchange_food', 'buy_soap',
                                 'exchange_pay')):
                break
            if not self.scen.go_back(source=source):
                break
            time.sleep(CLICK_INTERVAL)
        source = self.dev.hierarchy()
        if any(self.care.see(n, None, source) for n in ('status_region', 'feed', 'shower')):
            self.care.toggle_status(source)

    def nav_main(self) -> None:
        self.scen.ensure_main_page()

    def nav_status(self) -> None:
        self._enter_status_panel()

    def nav_feed(self) -> None:
        self._enter_status_panel()
        if not self._click_until_see(self.care, 'feed', ['feed_10', 'exchange_food'],
                                     '喂食面板', attempts=8):
            raise RuntimeError('未进入喂食面板')

    def nav_feed_popup(self) -> None:
        self.nav_feed()
        source = self.dev.hierarchy()
        hit = self.care.see('exchange_food', None, source)
        if not hit:
            raise StageSkipped('未看到"兑换食物"按钮（可能饼干库存充足），跳过')
        self.care.click(hit[0], hit[1])
        time.sleep(CLICK_INTERVAL)
        if not self._wait_see(self.care, ['exchange_pay'], attempts=6)[0]:
            raise RuntimeError('点击兑换食物后未出现支付弹窗')

    def nav_shower(self) -> None:
        self._enter_status_panel()
        if not self._click_until_see(self.care, 'shower', ['shower_10', 'buy_soap'],
                                     '洗澡面板', attempts=8):
            raise RuntimeError('未进入洗澡面板')

    def nav_shower_popup(self) -> None:
        self.nav_shower()
        source = self.dev.hierarchy()
        hit = self.care.see('buy_soap', None, source)
        if not hit:
            raise StageSkipped('未看到"购买洗澡道具"按钮（可能香皂库存充足），跳过')
        self.care.click(hit[0], hit[1])
        time.sleep(CLICK_INTERVAL)
        if not self._wait_see(self.care, ['exchange_pay'], attempts=6)[0]:
            raise RuntimeError('点击购买洗澡道具后未出现支付弹窗')

    def _open_friend_list(self) -> None:
        self.scen.ensure_main_page()
        self.visit.click_until_gone_or_see('visit_friends', 'visit', '打开好友列表')

    def _open_first_friend(self) -> None:
        self._open_friend_list()
        self._click_until_see(self.visit, 'visit', ['visit_step', 'visit_stepped'],
                              '访问好友', attempts=4)

    def nav_friend_list(self) -> None:
        self._open_friend_list()

    def nav_friend_home(self) -> None:
        self._open_first_friend()

    def nav_pk_ready(self) -> None:
        self._open_first_friend()
        self._click_until_see(self.pk, 'pk', ['pk_start'], '进入PK', attempts=6)

    def nav_out(self) -> None:
        self.scen.ensure_main_page()
        self.scen.leave_home()

    def nav_school(self) -> None:
        self.scen.ensure_main_page()
        self.scen.leave_home()
        self._click_until_see(self.scen, 'school', ['school_start', 'school_graduated'],
                              '进入学校')

    def nav_work(self) -> None:
        self.scen.ensure_main_page()
        self.scen.leave_home()
        self._click_until_see(self.scen, 'town',
                              ['select_box_container', 'work_start', 'work_outworker'],
                              '进入小镇', attempts=8)

    def nav_adventure(self) -> None:
        self.scen.ensure_main_page()
        self.scen.leave_home()
        self._click_until_see(self.scen, 'adventure', ['adventure_start'], '进入冒险')

    def nav_current(self) -> None:
        pass

    def leave_main(self) -> None:
        pass

    def leave_status(self) -> None:
        self._close_care_panels('status')

    def leave_feed(self) -> None:
        self._close_care_panels('feed')

    def leave_feed_popup(self) -> None:
        self._close_care_panels('feed_popup')

    def leave_shower(self) -> None:
        self._close_care_panels('shower')

    def leave_shower_popup(self) -> None:
        self._close_care_panels('shower_popup')

    def leave_friend_list(self) -> None:
        self.visit.close()
        self.scen.ensure_main_page()

    def leave_friend_home(self) -> None:
        self.visit.close()
        self.scen.ensure_main_page()

    def leave_pk_ready(self) -> None:
        if self.stage_state.get('pk_ready', {}).get('status') == 'ok':
            source = self.dev.hierarchy()
            if self.pk.see('pk_start', None, source):
                self.scen.go_back(source=source)
                time.sleep(CLICK_INTERVAL)
        self.visit.close()
        self.scen.ensure_main_page()

    def leave_out(self) -> None:
        self.scen.ensure_main_page()

    def leave_school(self) -> None:
        self.scen.ensure_main_page()

    def leave_work(self) -> None:
        self.scen.ensure_main_page()

    def leave_adventure(self) -> None:
        self.scen.ensure_main_page()

    def leave_current(self) -> None:
        pass

    def stage_specs(self) -> dict:
        return {
            'main': {'title': self.STAGE_TITLES['main'], 'enter': self.nav_main,
                     'leave': self.leave_main},
            'status': {'title': self.STAGE_TITLES['status'], 'enter': self.nav_status,
                       'leave': self.leave_status},
            'feed': {'title': self.STAGE_TITLES['feed'], 'enter': self.nav_feed,
                     'leave': self.leave_feed},
            'feed_popup': {'title': self.STAGE_TITLES['feed_popup'],
                           'enter': self.nav_feed_popup, 'leave': self.leave_feed_popup},
            'shower': {'title': self.STAGE_TITLES['shower'], 'enter': self.nav_shower,
                       'leave': self.leave_shower},
            'shower_popup': {'title': self.STAGE_TITLES['shower_popup'],
                             'enter': self.nav_shower_popup, 'leave': self.leave_shower_popup},
            'friend_list': {'title': self.STAGE_TITLES['friend_list'],
                            'enter': self.nav_friend_list, 'leave': self.leave_friend_list},
            'friend_home': {'title': self.STAGE_TITLES['friend_home'],
                            'enter': self.nav_friend_home, 'leave': self.leave_friend_home},
            'pk_ready': {'title': self.STAGE_TITLES['pk_ready'],
                         'enter': self.nav_pk_ready, 'leave': self.leave_pk_ready},
            'out': {'title': self.STAGE_TITLES['out'], 'enter': self.nav_out,
                    'leave': self.leave_out},
            'school': {'title': self.STAGE_TITLES['school'], 'enter': self.nav_school,
                       'leave': self.leave_school},
            'work': {'title': self.STAGE_TITLES['work'], 'enter': self.nav_work,
                     'leave': self.leave_work},
            'adventure': {'title': self.STAGE_TITLES['adventure'],
                          'enter': self.nav_adventure, 'leave': self.leave_adventure},
            'current': {'title': self.STAGE_TITLES['current'],
                        'enter': self.nav_current, 'leave': self.leave_current},
        }

    # ---- 运行 ----

    def run_stage(self, key: str) -> None:
        spec = self.stage_specs()[key]
        state = {'status': 'ok', 'note': '', 'rounds': 0}
        self.stage_state[key] = state
        self.stage_probe[key] = {}
        print(f'\n===== {spec["title"]} =====')
        try:
            spec['enter']()
        except StageSkipped as e:
            state['status'] = 'skipped'
            state['note'] = str(e)
            print(f'  跳过: {e}')
        except Exception as e:  # noqa: BLE001 - 阶段失败继续测其他阶段
            state['status'] = 'error'
            state['note'] = f'{type(e).__name__}: {e}'
            print(f'  进入失败: {state["note"]}')

        for r in range(self.rounds):
            clear_caches()
            source = None
            hierarchy_error = None
            try:
                source = self.dev.hierarchy()
            except Exception as e:  # noqa: BLE001
                hierarchy_error = f'{type(e).__name__}: {e}'
            result = self.probe.run(self.dev, source, self.force_live)
            if hierarchy_error:
                for row in result.values():
                    row['error'] = (row['error'] or '') + f' hierarchy={hierarchy_error}'
            self.stage_probe[key][r] = result
            for label, row in result.items():
                if row['snapshot_hits'] or row['live_hits']:
                    self.xpath_seen[label].append(key)
            hit_names = [label for label, row in result.items()
                         if row['snapshot_hits'] or row['live_hits']]
            print(f'  第 {r + 1} 轮命中 {len(hit_names)}/{len(result)}'
                  + (f'：{", ".join(hit_names[:12])}' if hit_names else ''))
            if r < self.rounds - 1:
                time.sleep(self.interval)

        if key in ('school', 'work'):
            derived = {}
            for name in DERIVED_NAMES:
                clear_caches()
                hit = self.scen.see(name, source=None)
                derived[name] = bool(hit)
            self.stage_derived[key] = derived
            print('  推导定位: ' + ', '.join(
                f'{name}={"命中" if ok else "未命中"}' for name, ok in derived.items()))

        try:
            spec['leave']()
        except Exception as e:  # noqa: BLE001
            note = f'{type(e).__name__}: {e}'
            state['note'] = (state['note'] + '；' if state['note'] else '') + f'退出异常: {note}'
            print(f'  退出异常: {note}')
        if key != 'current':
            try:
                self.scen.ensure_main_page()
            except Exception as e:  # noqa: BLE001
                print(f'  提示: 回主页面失败（后续阶段可能受影响）: {e}')
        state['rounds'] = self.rounds

    def run(self, keys: list[str]) -> None:
        for key in keys:
            self.run_stage(key)

    # ---- 汇总 ----

    def summarize(self) -> dict:
        summary = {}
        locator_seen = {name: False for _, name, _ in self.probe.items}
        for label, name, _ in self.probe.items:
            if self.xpath_seen.get(label):
                locator_seen[name] = True
        for label, name, xp in self.probe.items:
            seen_stages = self.xpath_seen.get(label, [])
            expected = EXPECTED_STAGES.get(name, [])
            reached = [k for k in expected
                       if self.stage_state.get(k, {}).get('status') == 'ok']
            attempted = [k for k in expected
                         if self.stage_state.get(k, {}).get('status') in ('ok', 'error')]
            if seen_stages:
                status = 'ok'
            elif locator_seen[name]:
                status = 'unverified'  # 同 locator 的其它 xpath 已命中，这条只是备用路径
            elif name in UNVERIFIED_NAMES:
                status = 'unverified'
            elif not reached:
                status = 'unverified'
            elif name in CONDITIONAL_NAMES:
                status = 'conditional'
            else:
                status = 'miss'
            summary[label] = {
                'locator': name,
                'xpath': xp,
                'status': status,
                'stages_seen': seen_stages,
                'expected_stages': expected,
                'attempted_stages': attempted,
                'reached_stages': reached,
                'bounds': [],
            }
            for stage in seen_stages:
                for round_rows in self.stage_probe.get(stage, {}).values():
                    row = round_rows.get(label)
                    if row:
                        summary[label]['bounds'].extend(row.get('bounds', []))
            summary[label]['bounds'] = sorted({tuple(b) for b in summary[label]['bounds']})
        return summary

    def print_summary(self, summary: dict) -> None:
        by_status: dict[str, list[str]] = {}
        for label, info in summary.items():
            by_status.setdefault(info['status'], []).append(label)
        print('\n===== 汇总 =====')
        for status in ('ok', 'conditional', 'unverified', 'miss'):
            labels = by_status.get(status, [])
            print(f'{status}: {len(labels)}')
            for label in labels:
                info = summary[label]
                print(f'  {label:28s} stages={",".join(info["stages_seen"]) or "-"}'
                      f' expected={",".join(info["expected_stages"]) or "-"}')

    def write_report(self, summary: dict, device_serial: str, json_path: str | None) -> Path:
        runs = APP_ROOT / 'runs'
        runs.mkdir(exist_ok=True)
        path = Path(json_path) if json_path else (
            runs / f'locator_report_all_{datetime.now():%Y%m%d_%H%M%S}.json')
        report = {
            'generated_at': datetime.now().isoformat(timespec='seconds'),
            'device_serial': device_serial,
            'rounds': self.rounds,
            'stages': self.stage_state,
            'stage_derived': self.stage_derived,
            'summary': summary,
        }
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'\n报告已保存: {path}')
        return path


def parse_stages(raw: str) -> list[str]:
    if raw == 'all':
        keys = [k for k in LocatorAllTester.STAGE_KEYS if k != 'current']
        return keys
    keys = [k.strip() for k in raw.split(',') if k.strip()]
    unknown = [k for k in keys if k not in LocatorAllTester.STAGE_KEYS]
    if unknown:
        raise SystemExit(
            f'未知阶段: {", ".join(unknown)}'
            f'（可选: {", ".join(LocatorAllTester.STAGE_KEYS)}）')
    return keys


def main() -> None:
    ap = argparse.ArgumentParser(description='自动导航全场景测试 locators.py 的 xpath')
    ap.add_argument('--stages', default='all', metavar='STAGES',
                    help='逗号分隔阶段（main,status,feed,feed_popup,shower,shower_popup,'
                         'friend_list,friend_home,pk_ready,out,school,work,adventure,current），'
                         '默认 all（不含 current）')
    ap.add_argument('--stage', dest='single_stage', default=None, metavar='STAGE',
                    help='只跑单个阶段（等价 --stages STAGE）')
    ap.add_argument('--rounds', type=int, default=DEFAULT_ROUNDS,
                    help=f'每阶段连续检测轮数（默认 {DEFAULT_ROUNDS}）')
    ap.add_argument('--interval', type=float, default=DEFAULT_INTERVAL,
                    help=f'每轮间隔秒数（默认 {DEFAULT_INTERVAL}）')
    ap.add_argument('--serial', default=None, help='覆盖 config.yaml 的设备序列号')
    ap.add_argument('--live', action='store_true',
                    help='每个 xpath 每轮额外做一次 d.xpath 实时查询（较慢，默认只用控件树快照）')
    ap.add_argument('--json', default=None, metavar='PATH', help='报告输出路径')
    ap.add_argument('--list', action='store_true', help='只列出全部 xpath 与预期阶段，不连设备')
    args = ap.parse_args()

    items = iter_xpath_items()
    if args.list:
        print(f'共 {len(items)} 条 xpath')
        for label, name, xp in items:
            expected = ','.join(EXPECTED_STAGES.get(name, [])) or '-'
            print(f'{label:28s} expected={expected}')
        print('\n非 xpath 的定位方式（u2/ocr/rel/from_bounds）不属于本次 xpath 测试范围；'
              'select_box_1/2/3 由 select_box_container 推导，随 school/work 阶段一起验证。')
        return

    if args.rounds < 1:
        ap.error('--rounds 至少为 1')
    if args.single_stage:
        keys = parse_stages(args.single_stage)
    else:
        keys = parse_stages(args.stages)

    cfg = load_config()
    serial = args.serial or cfg.adb.device_serial
    dev = U2Device(find_adb(cfg.adb.path), serial)
    time.sleep(1.0)

    tester = LocatorAllTester(dev, args.rounds, args.interval, args.live)
    try:
        tester.run(keys)
    except KeyboardInterrupt:
        print('\n手动中断，仍输出已完成的汇总...')
    finally:
        summary = tester.summarize()
        tester.print_summary(summary)
        path = tester.write_report(summary, serial, args.json)
        print(f'\n提示: 想补测进行中/结算/弹窗等状态，把手机停到对应页面后运行 '
              f'python tools/test_locator_all.py --stage current --rounds 5')


if __name__ == '__main__':
    main()
