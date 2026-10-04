#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "请以 root 运行：sudo bash deploy/deploy.sh"
  exit 1
fi

SERVICE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$SERVICE_DIR/.venv"
UNIT_SRC="$SERVICE_DIR/deploy/auto-douyin-sparks-pro.service"
UNIT_DST="/etc/systemd/system/auto-douyin-sparks-pro.service"

# ============================================================
# 镜像源选择：官方 / 阿里云 / 清华 TUNA
#   非交互方式：MIRROR=aliyun bash deploy/deploy.sh
#              （可选 aliyun / tuna / official，默认 official）
# ============================================================
MIRROR="${MIRROR:-}"
if [ -z "$MIRROR" ]; then
  echo "==> 请选择软件源（apt / pip / Playwright 下载统一切换）"
  echo "    1) 官方源（默认，海外服务器选这个）"
  echo "    2) 阿里云镜像（国内服务器推荐）"
  echo "    3) 清华 TUNA 镜像（国内服务器推荐）"
  read -r -p "输入序号 [1-3，回车默认 1]: " choice || choice=""
  case "${choice:-1}" in
    2) MIRROR="aliyun" ;;
    3) MIRROR="tuna" ;;
    *) MIRROR="official" ;;
  esac
fi

APT_MIRROR=""
PIP_INDEX=""
PY_MIRROR_DESC="官方源"
case "$MIRROR" in
  aliyun)
    APT_MIRROR="mirrors.aliyun.com"
    PIP_INDEX="https://mirrors.aliyun.com/pypi/simple/"
    PLAYWRIGHT_HOST="https://cdn.npmmirror.com/binaries/playwright"
    PY_MIRROR_DESC="阿里云镜像（Playwright 用 npmmirror）"
    ;;
  tuna)
    APT_MIRROR="mirrors.tuna.tsinghua.edu.cn"
    PIP_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
    PLAYWRIGHT_HOST="https://cdn.npmmirror.com/binaries/playwright"
    PY_MIRROR_DESC="清华 TUNA 镜像（Playwright 用 npmmirror）"
    ;;
  *)
    MIRROR="official"
    PLAYWRIGHT_HOST=""
    PY_MIRROR_DESC="官方源"
    ;;
esac
echo "==> 使用 $PY_MIRROR_DESC"

# ------------------------------------------------------------
# apt 换源（仅在用户选择了国内镜像时执行；先备份）
# ------------------------------------------------------------
if [ -n "$APT_MIRROR" ]; then
  echo "==> 切换 apt 源到 $APT_MIRROR（原文件备份为 .bak）"
  for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list.d/ubuntu.sources; do
    [ -f "$f" ] || continue
    cp -n "$f" "$f.bak" 2>/dev/null || true
    sed -i \
      -e "s|http://deb.debian.org|https://$APT_MIRROR|g" \
      -e "s|https://deb.debian.org|https://$APT_MIRROR|g" \
      -e "s|http://security.debian.org|https://$APT_MIRROR/debian-security|g" \
      -e "s|https://security.debian.org|https://$APT_MIRROR/debian-security|g" \
      -e "s|http://archive.ubuntu.com|https://$APT_MIRROR/ubuntu|g" \
      -e "s|https://archive.ubuntu.com|https://$APT_MIRROR/ubuntu|g" \
      -e "s|http://security.ubuntu.com|https://$APT_MIRROR/ubuntu|g" \
      -e "s|https://security.ubuntu.com|https://$APT_MIRROR/ubuntu|g" \
      "$f"
  done
fi

echo "==> 安装系统依赖"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip \
  xvfb xauth \
  fonts-wqy-zenhei fonts-noto-color-emoji \
  ca-certificates curl

echo "==> 创建 Python 虚拟环境"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi

echo "==> 安装 Python 依赖"
PIP_ARGS=()
if [ -n "$PIP_INDEX" ]; then
  PIP_ARGS+=(-i "$PIP_INDEX")
fi
"$VENV/bin/pip" install --upgrade pip "${PIP_ARGS[@]}"
"$VENV/bin/pip" install "${PIP_ARGS[@]}" -r "$SERVICE_DIR/requirements.txt"

echo "==> 安装 Chromium 的系统依赖（libnss3 等运行库）"
"$VENV/bin/playwright" install-deps chromium

echo "==> 安装 Chromium（首次需下载数百 MB）"
if [ -n "$PLAYWRIGHT_HOST" ]; then
  PLAYWRIGHT_DOWNLOAD_HOST="$PLAYWRIGHT_HOST" "$VENV/bin/playwright" install chromium
else
  "$VENV/bin/playwright" install chromium
fi

# ------------------------------------------------------------
# 虚拟显示：无图形界面的服务器上，「网页登录（有头浏览器）」
# 依赖 X Server。安装 Xvfb 后由 systemd 用 xvfb-run 启动服务，
# 有头浏览器即可在虚拟显示中运行。
# ------------------------------------------------------------
XVFB_RUN=""
if command -v xvfb-run >/dev/null 2>&1; then
  # systemd 只认双引号；引号必须写在这里，替换进 unit 后
  # ExecStart 形如：
  #   ExecStart=/usr/bin/xvfb-run --auto-servernum "--server-args=-screen 0 1366x768x24" /path/.venv/bin/python app.py
  XVFB_RUN='/usr/bin/xvfb-run --auto-servernum "--server-args=-screen 0 1366x768x24" '
  echo "==> 已配置 Xvfb 虚拟显示（有头浏览器将在虚拟屏幕中运行）"
else
  echo "==> 未找到 xvfb-run，有头浏览器将不可用（网页扫码登录不受影响）"
fi

echo "==> 配置 2G 交换空间（1G 内存服务器跑浏览器需要）"
if ! swapon --show | grep -q 'swap'; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "swap 已创建并启用"
else
  echo "检测到已有 swap，跳过"
fi

echo "==> 设置时区为 Asia/Shanghai"
timedatectl set-timezone Asia/Shanghai || echo "无法设置时区（容器环境可忽略）"

echo "==> 生成访问令牌"
if [ ! -f "$SERVICE_DIR/.env" ]; then
  TOKEN="$(head -c 24 /dev/urandom | sha256sum | head -c 32)"
  cat > "$SERVICE_DIR/.env" <<EOF
AUTH_TOKEN=$TOKEN
PORT=8000
EOF
fi
TOKEN_VALUE="$(grep '^AUTH_TOKEN=' "$SERVICE_DIR/.env" | cut -d= -f2- | tr -d '\r\n')"
if [ -z "$TOKEN_VALUE" ]; then
  TOKEN_VALUE="$(head -c 24 /dev/urandom | sha256sum | head -c 32)"
  sed -i "s/^AUTH_TOKEN=.*/AUTH_TOKEN=$TOKEN_VALUE/" "$SERVICE_DIR/.env"
fi

echo "==> 安装 systemd 服务"
sed "s|__DIR__|$SERVICE_DIR|g; s|__VENV__|$VENV|g; s|__XVFB_RUN__|$XVFB_RUN|g" \
  "$UNIT_SRC" > "$UNIT_DST"
# 默认不自动启动（手动 python app.py 时端口不冲突）；需要自启：AUTOSTART=1
AUTOSTART="${AUTOSTART:-0}"
systemctl daemon-reload
if [ "$AUTOSTART" = "1" ]; then
  systemctl enable --now auto-douyin-sparks-pro
  sleep 2
  systemctl --no-pager --lines=5 status auto-douyin-sparks-pro || true
else
  systemctl disable auto-douyin-sparks-pro >/dev/null 2>&1 || true
  if systemctl is-active --quiet auto-douyin-sparks-pro; then
    systemctl stop auto-douyin-sparks-pro
    echo "==> 已停止正在运行的服务（8000 端口已释放）"
  fi
  echo "==> 已安装服务但未启动（默认不自启）"
fi

IP="$(hostname -I | awk '{print $1}')"
echo ""
echo "======================================================"
echo "部署完成！"
echo "软件源:   $PY_MIRROR_DESC"
echo "网页地址: http://$IP:8000"
echo "访问令牌: $TOKEN_VALUE"
echo "令牌保存在: $SERVICE_DIR/.env"
echo "======================================================"
if [ "$AUTOSTART" = "1" ]; then
  echo "服务已启动并设为开机自启（systemd 管理）"
else
  echo "启动方式（三选一）："
  echo "  1. 前台手动运行:  cd $SERVICE_DIR && .venv/bin/python app.py"
  echo "  2. 本次启动服务:  sudo systemctl start auto-douyin-sparks-pro"
  echo "  3. 开机自启:      sudo AUTOSTART=1 bash deploy/deploy.sh"
fi
echo "接下来："
echo "1. 打开网页 -> 凭证 -> 「手机扫码登录」：网页上会显示二维码，"
echo "   用手机抖音 App 扫码即可（服务器无图形界面也能用）"
echo "2. 也可以上传本机导出的 state.json（python extract_cookie.py）"
echo "3. 配置好友与发送时间，点「干跑测试」验证后 再点「立即发送」"
