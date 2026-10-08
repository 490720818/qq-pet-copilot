#!/usr/bin/env bash
# 检测安卓设备是否被 udev/adb 正常识别，并在需要时给出修复命令。
# 用法: tools/linux_android_check.sh
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADB="$ROOT/resources/platform-tools/adb"
[ -x "$ADB" ] || ADB="$(command -v adb || true)"

echo "== adb =="
if [ -z "${ADB:-}" ]; then
  echo "找不到 adb（先跑 tools/fetch_scrcpy.py 或 apt install android-tools-adb）"
else
  echo "路径: $ADB"
  "$ADB" start-server >/dev/null 2>&1
  "$ADB" devices -l
fi

echo
echo "== USB 里的安卓设备（vendor:product） =="
FOUND=0
for d in /sys/bus/usb/devices/*; do
  [ -f "$d/idVendor" ] || continue
  vid=$(cat "$d/idVendor" 2>/dev/null) || continue
  pid=$(cat "$d/idProduct" 2>/dev/null) || continue
  # 只认带 ADB 接口的设备：ADB 接口是 vendor-specific 类
  # （bInterfaceClass=ff, subclass=42, protocol=01）。比按厂商 ID 白名单靠谱——
  # MediaTek(0e8d) 之类的 ID 也用在蓝牙/无线网卡上，会误报。
  IS_ADB=0
  for intf in "$d":*/; do
    [ -f "$intf/bInterfaceClass" ] || continue
    cls=$(cat "$intf/bInterfaceClass" 2>/dev/null)
    sub=$(cat "$intf/bInterfaceSubClass" 2>/dev/null)
    proto=$(cat "$intf/bInterfaceProtocol" 2>/dev/null)
    if [ "$cls" = "ff" ] && [ "$sub" = "42" ] && [ "$proto" = "01" ]; then
      IS_ADB=1
      break
    fi
  done
  [ "$IS_ADB" = 0 ] && continue
  MODEL=""
  [ -f "$d/product" ] && MODEL=$(cat "$d/product")
  DEVPATH="/dev/bus/usb/$(printf '%03d' "$(cat "$d/busnum")")/$(printf '%03d' "$(cat "$d/devnum")")"
  echo "  $vid:$pid  ${MODEL:-（无 product 名）}"
  echo "      节点: $DEVPATH  权限: $(stat -c '%A %U:%G' "$DEVPATH" 2>/dev/null || echo '读取失败')"
  FOUND=1
done
[ "$FOUND" = 0 ] && echo "  未发现带 ADB 接口的安卓设备（确认手机已插好、通知栏选了「文件传输」且开了 USB 调试）"

echo
echo "== 已有 udev 规则 =="
ls /etc/udev/rules.d/ /usr/lib/udev/rules.d/ 2>/dev/null | grep -i -E "android|adb" || echo "  无安卓相关规则"

echo
echo "== 结论 =="
if [ -n "${ADB:-}" ] && "$ADB" devices 2>/dev/null | grep -q "device$"; then
  echo "  ✅ adb 已认到设备，可以启动 GUI 了"
elif [ -n "${ADB:-}" ] && "$ADB" devices 2>/dev/null | grep -q "no permissions"; then
  echo "  ⚠️  认到设备但无权限 -> 需要 udev 规则（见下）"
  echo "     sudo bash tools/linux_android_udev.sh <上面那个 vendor id>"
elif [ "$FOUND" = 1 ]; then
  echo "  ⚠️  USB 上能看到设备，但 adb 列表里没有 -> 手机端「USB 调试」没开，或需要 udev 规则"
  echo "     sudo bash tools/linux_android_udev.sh <上面那个 vendor id>"
else
  echo "  手机还没插上 / 没被识别"
fi
