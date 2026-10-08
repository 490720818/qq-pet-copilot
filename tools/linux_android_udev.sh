#!/usr/bin/env bash
# 给指定厂商的安卓设备安装 udev 规则（需要 root）。
#
# 用法:
#   sudo bash tools/linux_android_udev.sh 18d1      # 只加一个厂商（推荐）
#   sudo bash tools/linux_android_udev.sh          # 不加参数 = 装常见安卓厂商全量规则
#
# 说明：Pop!_OS / Ubuntu 没装 android-sdk-platform-tools-common 时，普通用户
# 访问 /dev/bus/usb/... 会被拒（adb 显示 "no permissions"）。本脚本写一条
# udev 规则把对应设备的权限给 plugdev 组，并把你加进 plugdev 组。
#
# 规则文件名带 qqpetcopilot，**不覆盖**发行版/官方包已有的 51-android.rules
# （那个文件常由 android-sdk-platform-tools-common 提供，覆盖掉会影响别的工具）。
set -euo pipefail

RULE_FILE=/etc/udev/rules.d/51-android-qqpetcopilot.rules
LEGACY_FILE=/etc/udev/rules.d/51-android.rules

# 常见安卓厂商 vendor id（Google / 三星 / HTC / 索尼 / 华为 / 摩托 / 小米 / 一加 /
# 高通 / 联想 / 华硕 / 中兴 / 努比亚 / OPPO / vivo / 魅族 / 锤子 / 传音 / realme 等）
# 注意 0e8d 是联发科：它同时用在蓝牙/无线网卡上，全量规则会给这些设备也放权限
# （这也是 linux_android_check.sh 按 ADB 接口签名而不是厂商 ID 识别设备的原因）。
# 只接自己那台手机时更推荐 `sudo bash tools/linux_android_udev.sh <vendor id>`。
COMMON_IDS="18d1 04e8 0bb4 0fce 12d1 22b8 2717 2a70 2ae5 2b4c 05c6 1004 0b05 \
17ef 1949 1ebf 2314 2916 2d95 19d2 04dd 0955 0bda 0e8d 04c5 109b 1f3a 29a9 \
30b1 2c7c 0b0e 2d01 1f0a 2207 2a45"

if [ "$(id -u)" -ne 0 ]; then
  echo "需要 root：sudo bash $0 ${1:-}" >&2
  exit 1
fi

if [ $# -ge 1 ]; then
  IDS="$*"
  echo "只添加指定厂商: $IDS"
else
  IDS="$COMMON_IDS"
  echo "添加常见安卓厂商全量规则"
fi

# 去重（同一个 id 写两遍没意义）
IDS="$(printf '%s\n' $IDS | sort -u | tr '\n' ' ')"

{
  echo "# 安卓设备 adb 访问权限（由 qq-pet-copilot/tools/linux_android_udev.sh 生成）"
  echo "# 只需要 adb 访问；MODE=0660 + plugdev 组，比 0666 更收敛"
  for id in $IDS; do
    echo "SUBSYSTEM==\"usb\", ATTR{idVendor}==\"$id\", MODE=\"0660\", GROUP=\"plugdev\""
  done
} > "$RULE_FILE"

chmod 644 "$RULE_FILE"
echo "已写入 $RULE_FILE（未改动其他 udev 规则文件）"
if [ -f "$LEGACY_FILE" ]; then
  echo "提示：已存在 $LEGACY_FILE（可能来自发行版/官方 platform-tools 包），它与本规则并存，本脚本不会覆盖它。"
fi

# 让当前用户属于 plugdev：MODE=0660 时**必须**在组里才能访问（重登后生效）
TARGET_USER="${SUDO_USER:-}"
if [ -n "$TARGET_USER" ]; then
  if id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx plugdev; then
    echo "$TARGET_USER 已在 plugdev 组"
  else
    usermod -aG plugdev "$TARGET_USER" && echo "已把 $TARGET_USER 加入 plugdev（需重新登录生效）"
  fi
else
  echo "提示：没检测到 SUDO_USER，请自己确认当前用户在 plugdev 组里：sudo usermod -aG plugdev <用户名>"
fi

udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --action=add
echo "udev 规则已重载。把手机拔下再插上（组权限要重新登录才生效），然后运行 tools/linux_android_check.sh 验证。"
