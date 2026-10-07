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
set -euo pipefail

RULE_FILE=/etc/udev/rules.d/51-android.rules

# 常见安卓厂商 vendor id（Google / 三星 / HTC / 索尼 / 华为 / 摩托 / 小米 / 一加 /
# 高通 / 联想 / 华硕 / 中兴 / 努比亚 / OPPO / vivo / 魅族 / 锤子 / 传音 / realme 等）
COMMON_IDS="18d1 04e8 0bb4 0fce 12d1 22b8 2717 2a70 2ae5 2b4c 05c6 1004 0b05 \
17ef 1949 1ebf 2314 2916 2d95 19d2 04dd 0955 0bda 0e8d 04c5 109b 1f3a 29a9 \
30b1 2c7c 0b0e 2d01 1f0a 2207 0e8d 2a45 18d1"

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

{
  echo "# 安卓设备 adb 访问权限（由 qq-pet-copilot/tools/linux_android_udev.sh 生成）"
  for id in $IDS; do
    echo "SUBSYSTEM==\"usb\", ATTR{idVendor}==\"$id\", MODE=\"0666\", GROUP=\"plugdev\""
  done
} > "$RULE_FILE"

chmod 644 "$RULE_FILE"
echo "已写入 $RULE_FILE"

# 让当前用户属于 plugdev（重登后生效；MODE=0666 时其实不需要，作为兜底）
TARGET_USER="${SUDO_USER:-}"
if [ -n "$TARGET_USER" ] && ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx plugdev; then
  usermod -aG plugdev "$TARGET_USER" && echo "已把 $TARGET_USER 加入 plugdev（需重新登录生效）"
fi

udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --action=add
echo "udev 规则已重载。把手机拔下再插上，然后运行 tools/linux_android_check.sh 验证。"
