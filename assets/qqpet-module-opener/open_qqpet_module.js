/*
 * QQ 宠物模块打开器 hook 脚本（Frida JS）
 *
 * 来源：https://github.com/yikehuang/qqpet-module-opener 的 src/open_qqpet_module.js
 * 本项目只保留这一个 JS 文件（其余上游代码不引入），QQ 更新导致类名/方法变化时
 * 从这里手动同步最新版本：上游仓库 -> src/open_qqpet_module.js -> 本文件。
 * 与 src/opener.py 的约定：脚本通过 send({event, detail}) 回报
 * account / opened / error 事件，Python 侧据此判断成功与失败。
 */
'use strict';

setImmediate(function () {
  Java.perform(function () {
    function report(event, detail) {
      send({ event: event, detail: String(detail || '') });
      if (event === 'account') console.log('[QQPET_ACCOUNT] QQ：' + detail);
      if (event === 'opened') console.log('[QQPET_OPENED] ' + detail);
      if (event === 'error') console.log('[QQPET_ERROR] ' + detail);
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
      const Intent = Java.use('android.content.Intent');
      const Unit = Java.use('kotlin.Unit');
      const Function1 = Java.use('kotlin.jvm.functions.Function1');
      const FragmentHost = Java.use('com.tencent.mobileqq.activity.QPublicFragmentActivity');
      const PetMainFragment = Java.use('com.tencent.mobileqq.qqpet.main.PetMainFragment');
      const PetQQMC = Java.use('com.tencent.mobileqq.qqpet.qqmc.PetQQMC');
      const Sdk = Java.use('com.tencent.mobileqq.qqpet.sdk.a');
      const uin = currentUin();
      report('account', uin);

      try {
        PetQQMC.e.implementation = function () { return true; };
      } catch (_) {}

      // 好友访问/踩踩/PK 跳转（mqqapi://qpet/open?uin=<目标QQ号>）在模拟器 QQ 上会弹
      // "功能暂未开放，敬请期待"（qqpet.jump.a.doAction 返回 false），这里直接接管：
      // 用跳转里的 uin 打开目标宠物主页（无 uin 时用当前登录账号，即打开自己）。
      // 注意：该 override 需要在整个自动化运行期间持续生效（src/opener.py
      // 注入成功后保持脚本存活，不解除）。
      try {
        const JumpAction = Java.use('com.tencent.mobileqq.qqpet.jump.a');
        JumpAction.doAction.implementation = function () {
          try {
            const attrs = Java.cast(this.attrs.value, Java.use('java.util.Map'));
            let target = attrs.get('uin');
            if (target === null || target === undefined) {
              target = uin; // 无目标 uin（打开自己）时用当前登录账号
            }
            const intent = Intent.$new();
            // 透传原始跳转的全部参数（friendListSource / friendDataKey / report_source 等）
            const keys = attrs.keySet().toArray();
            const obj = {};
            for (let i = 0; i < keys.length; i++) {
              const k = String(keys[i]);
              const v = String(attrs.get(k));
              obj[k] = v;
              intent.putExtra(k, v);
            }
            intent.putExtra('petUin', String(target));
            // 关键：pageData 必须传 attrs 的 JSON（真机实测），'{}' 会导致好友页
            // 底部好友列表只加载默认几个，传完整 JSON 才会出现全部好友
            intent.putExtra('pageData', JSON.stringify(obj));
            intent.putExtra('from_adopt', false);
            intent.putExtra('adopt_closing_pose_id', 0);
            FragmentHost.start.overload(
              'android.content.Context', 'android.content.Intent', 'java.lang.Class'
            ).call(FragmentHost, this.context.value, intent, PetMainFragment.class);
            send({ event: 'visited', detail: 'PetMainFragment uin=' + target });
            return true;
          } catch (error) {
            report('error', error.stack || error);
            return false;
          }
        };
      } catch (_) {}

      const sdkClass = Sdk.class;
      const singleton = sdkClass.getDeclaredField('a');
      singleton.setAccessible(true);
      const sdk = singleton.get(null);
      const initMethod = sdkClass.getDeclaredMethod(
        'd', Java.array('java.lang.Class', [Function1.class])
      );
      initMethod.setAccessible(true);

      let activity = null;
      Java.choose('com.tencent.mobileqq.activity.SplashActivity', {
        onMatch: function (candidate) {
          try {
            if (!candidate.isFinishing() && !candidate.isDestroyed()) {
              activity = Java.retain(candidate);
            }
          } catch (_) {}
        },
        onComplete: function () {
          if (activity === null) {
            report('error', '没有找到 QQ 主界面，请先打开并登录手机 QQ');
            return;
          }
          const Callback = Java.registerClass({
            name: 'com.tencent.mobileqq.qqpet.EntryBypassCallback' + Date.now(),
            implements: [Function1],
            methods: {
              invoke: function (result) {
                Java.scheduleOnMainThread(function () {
                  try {
                    const intent = Intent.$new();
                    intent.putExtra('petUin', uin);
                    intent.putExtra('pageData', '{}');
                    intent.putExtra('from_adopt', false);
                    intent.putExtra('adopt_closing_pose_id', 0);
                    FragmentHost.start.overload(
                      'android.content.Context',
                      'android.content.Intent',
                      'java.lang.Class'
                    ).call(FragmentHost, activity, intent, PetMainFragment.class);
                    report('opened', 'PetMainFragment');
                  } catch (error) {
                    report('error', error.stack || error);
                  }
                });
                return Unit.INSTANCE.value;
              }
            }
          });
          initMethod.invoke(sdk, Java.array('java.lang.Object', [Callback.$new()]));
        }
      });
    } catch (error) {
      report('error', error.stack || error);
    }
  });
});
