"""抓取 QQ 宠物"访问好友"跳转参数（真机/模拟器对比、排查 QQ 更新用）。

用途：QQ 更新导致宠物模块跳转（mqqapi://qpet/open）行为变化、或模拟器版
opener 打开好友页异常时，用它抓取 doJumpAction 的完整 URL 与 doAction 的 attrs，
对比真机/模拟器的差异（例：pageData 必须传 attrs JSON 就是靠它定位的）。

用法：
  python tools/capture_visit_jump.py                       # 用 config.yaml 的 adb/序列号
  python tools/capture_visit_jump.py -s 127.0.0.1:7555     # 指定设备
  python tools/capture_visit_jump.py -c                    # 挂好探针后自动点 好友->访问

前提：目标设备已 Root 并运行 frida-server（版本与本机 frida 一致），QQ 已登录、
已打开宠物主页。脚本 hook 跳转入口后，由你在手机上点 好友 -> 访问
（或 -c 自动点），打印抓到的完整跳转 URL / attrs / doAction 返回值与调用栈。
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import find_adb, load_config
from src.opener import _choose_device, _java_bridge_source, _wrap_java_bridge


def _find_qq_pid(device) -> int | None:
    """找 QQ 主进程（frida 下主进程名可能是 com.tencent.mobileqq 或 QQ）。"""
    for p in device.enumerate_processes():
        if p.name in ('com.tencent.mobileqq', 'QQ'):
            return p.pid
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description='抓取 QQ 宠物访问好友的跳转参数')
    ap.add_argument('-s', '--serial', help='设备序列号（默认 config.yaml 的 adb.device_serial）')
    ap.add_argument('-a', '--adb', help='adb 可执行文件路径（默认 find_adb）')
    ap.add_argument('-c', '--click', action='store_true',
                    help='挂好探针后自动用 u2 点 好友->访问（否则手动在手机上点）')
    args = ap.parse_args()

    cfg = load_config()
    adb = args.adb or find_adb(cfg.adb.path)
    serial = args.serial or cfg.adb.device_serial or None
    serial = _choose_device(adb, serial)
    print(f'设备: {serial}')

    try:
        import frida
    except ImportError:
        print('未安装 frida', file=sys.stderr)
        return 1
    print(f'frida 客户端版本: {frida.__version__}（须与设备上的 frida-server 一致）')

    probe = """
Java.perform(function () {
  var Log = Java.use('android.util.Log');
  var Exception = Java.use('java.lang.Exception');
  function stack() { return Log.getStackTraceString(Exception.$new()); }
  // 跳转入口：完整 URL
  var JumpApi = Java.use('com.tencent.mobileqq.jump.api.impl.JumpApiImpl');
  JumpApi.doJumpAction.overloads.forEach(function(ov){
    ov.implementation = function() {
      var args = [];
      for (var i=0;i<arguments.length;i++) {
        var x = arguments[i];
        args.push(x === null ? 'null' : String(x));
      }
      send({event:'jump', args: args, stack: stack()});
      return ov.apply(this, arguments);
    };
  });
  // 跳转执行：attrs（friendListSource / friendDataKey / LoadMore 等）
  var a = Java.use('com.tencent.mobileqq.qqpet.jump.a');
  a.doAction.implementation = function () {
    try {
      var attrs = Java.cast(this.attrs.value, Java.use('java.util.Map'));
      var keys = attrs.keySet().toArray();
      var list = [];
      for (var i=0;i<keys.length;i++) {
        list.push(String(keys[i]) + '=' + String(attrs.get(keys[i])));
      }
      send({event:'doAction-attrs', list: list, stack: stack()});
    } catch (e) {
      send({event:'err', err: String(e)});
    }
    var ret = this.doAction();
    send({event:'doAction-ret', ret: String(ret)});
    return ret;
  };
  send({event:'ready'});
});
"""
    try:
        device = frida.get_device(serial, timeout=10)
    except Exception as e:
        print(f'frida 连不上设备 {serial}: {e}', file=sys.stderr)
        return 1
    pid = _find_qq_pid(device)
    if pid is None:
        print('未找到 QQ 进程，请先在设备上打开并登录 QQ', file=sys.stderr)
        return 1
    print(f'附加 QQ 进程: {pid}')
    session = device.attach(pid)
    script = session.create_script(_wrap_java_bridge(_java_bridge_source()) + probe)

    def on_message(msg, data):
        if msg.get('type') != 'send':
            return
        p = msg.get('payload', {})
        ev = p.get('event')
        if ev == 'jump':
            print('\n===== 跳转 URL =====')
            for a in p['args']:
                print(' ', a)
            print('----- 调用栈 -----')
            print(p.get('stack'))
        elif ev == 'doAction-attrs':
            print('\n===== doAction attrs =====')
            for kv in p['list']:
                print(' ', kv)
            print('----- 调用栈 -----')
            print(p.get('stack'))
        elif ev == 'doAction-ret':
            print('doAction 返回值:', p['ret'])
        elif ev == 'err':
            print('hook 错误:', p['err'])

    script.on('message', on_message)
    script.load()
    if args.click:
        print('探针已挂载，自动点 好友->访问...')
        from src.u2dev import U2Device
        dev = U2Device(adb, serial)
        d = dev.d
        # 只在没找到"好友"按钮时尝试点返回回宠物主页（避免把已打开的宠物页退掉）
        btn = d(description='好友')
        if not btn.exists:
            btn = d(description='friend')
        if not btn.exists:
            back = d(description='返回')
            if back.exists:
                back.click(); time.sleep(2)
                btn = d(description='好友')
                if not btn.exists:
                    btn = d(description='friend')
        if btn.exists:
            btn.click(); time.sleep(3)
            visit = d(description='访问')
            if visit.exists:
                visit.click(); time.sleep(4)
            else:
                print('未找到 访问 按钮（请确认在宠物主页点过 好友）')
        else:
            print('未找到 好友 按钮（请先在设备上打开 QQ 宠物主页）')
    else:
        print('探针已挂载。请在手机上点 好友 -> 访问（Ctrl+C 退出）')
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    try:
        script.unload()
    finally:
        session.detach()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
