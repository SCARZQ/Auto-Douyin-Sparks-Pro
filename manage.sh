#!/usr/bin/env bash
# ============================================================
# 云逸续火花助手 · 服务管理脚本（v1.6.9）
#
# 用法：
#   bash manage.sh            # 交互菜单
#   bash manage.sh start      # 后台启动（退出 SSH 可用）
#   bash manage.sh stop       # 强制停止
#   bash manage.sh restart    # 重启
#   bash manage.sh status     # 运行状态
#   bash manage.sh fg         # 前台启动（看实时输出，Ctrl+C 退出）
#   bash manage.sh logs       # 跟踪运行日志（Ctrl+C 退出）
#   bash manage.sh mem        # 查看内存占用
# ============================================================
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/.venv"
PY="$VENV/bin/python"
RUN_DIR="$DIR/run"
PID_FILE="$RUN_DIR/app.pid"
LOG_FILE="$RUN_DIR/app.log"
PORT="${PORT:-8000}"

C_G="\033[32m"; C_R="\033[31m"; C_Y="\033[33m"; C_N="\033[0m"
ok()   { echo -e "${C_G}[OK]${C_N} $1"; }
err()  { echo -e "${C_R}[失败]${C_N} $1"; }
warn() { echo -e "${C_Y}[提示]${C_N} $1"; }

# ---------- 环境检查 ----------
[ -x "$PY" ] || { err "未找到虚拟环境 $VENV，请先运行 deploy/deploy.sh"; exit 1; }

# 若 systemd 服务在跑，提示避免端口冲突
systemd_active() {
  command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet auto-douyin-sparks-pro 2>/dev/null
}

is_running() {
  # 以 pid 文件为准，并校验进程还活着
  if [ -f "$PID_FILE" ]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

find_port_pid() {
  # 兜底：谁占着 PORT
  if command -v fuser >/dev/null 2>&1; then
    fuser "$PORT"/tcp 2>/dev/null | tr -s ' '
  elif command -v lsof >/dev/null 2>&1; then
    lsof -ti tcp:"$PORT" 2>/dev/null
  fi
}

do_start_fg() {
  if systemd_active; then
    warn "systemd 服务正在运行，先执行: sudo systemctl stop auto-douyin-sparks-pro"
    return 1
  fi
  if is_running || [ -n "$(find_port_pid)" ]; then
    err "服务已在运行（或端口 $PORT 被占用），请先 stop"
    return 1
  fi
  mkdir -p "$RUN_DIR"
  echo "前台启动（Ctrl+C 停止，日志同时写入 $LOG_FILE）..."
  cd "$DIR"
  exec "$PY" app.py 2>&1 | tee -a "$LOG_FILE"
}

do_start_bg() {
  if systemd_active; then
    warn "systemd 服务正在运行；如需脚本管理，先执行: sudo systemctl disable --now auto-douyin-sparks-pro"
    return 1
  fi
  if is_running; then
    warn "已在后台运行（PID $(cat "$PID_FILE")），无需重复启动"
    return 0
  fi
  if [ -n "$(find_port_pid)" ]; then
    err "端口 $PORT 已被占用（PID $(find_port_pid)），请先 stop"
    return 1
  fi
  mkdir -p "$RUN_DIR"
  cd "$DIR"
  # nohup + nohup 忽略 SIGHUP：退出 SSH 后继续运行
  nohup "$PY" app.py >> "$LOG_FILE" 2>&1 &
  local pid=$!
  echo "$pid" > "$PID_FILE"
  sleep 2
  if kill -0 "$pid" 2>/dev/null; then
    ok "后台启动成功 PID=$pid（退出 SSH 不受影响）"
    echo "      日志: tail -f $LOG_FILE"
  else
    err "启动失败，最近日志："
    tail -5 "$LOG_FILE"
    rm -f "$PID_FILE"
    return 1
  fi
}

do_stop() {
  local stopped=0
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    kill "$pid" 2>/dev/null
    # 等最多 5 秒优雅退出
    for _ in 1 2 3 4 5; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
      warn "未响应，强制结束 PID $pid"
      kill -9 "$pid" 2>/dev/null
    fi
    rm -f "$PID_FILE"
    ok "已停止 PID $pid"
    stopped=1
  fi
  # 兜底：强杀占端口的进程
  local ppid
  ppid="$(find_port_pid)"
  if [ -n "$ppid" ]; then
    for p in $ppid; do
      kill -9 "$p" 2>/dev/null
      ok "强制结束占用端口 $PORT 的进程 $p"
    done
    stopped=1
  fi
  # 兜底：清理残留的 app.py 进程
  pkill -9 -f "$PY app.py" 2>/dev/null && { ok "已清理残留 app.py 进程"; stopped=1; }
  [ "$stopped" = "1" ] || warn "服务本来就没在运行"
}

do_status() {
  if systemd_active; then
    echo -e "systemd 服务: ${C_G}运行中${C_N}（systemctl 管理）"
  fi
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    local uptime=""
    uptime="$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')"
    echo -e "脚本服务: ${C_G}运行中${C_N} PID=$pid 已运行=${uptime:-?} 端口=$PORT"
  else
    echo -e "脚本服务: ${C_R}未运行${C_N}"
  fi
  local ppid
  ppid="$(find_port_pid)"
  [ -n "$ppid" ] && echo "端口 $PORT 占用: PID $ppid"
  [ -f "$LOG_FILE" ] && echo "日志文件: $LOG_FILE ($(du -h "$LOG_FILE" | cut -f1))"
}

do_logs() {
  [ -f "$LOG_FILE" ] || { warn "暂无日志文件 $LOG_FILE"; return; }
  tail -n 200 -f "$LOG_FILE"
}

do_mem() {
  echo "=== 内存占用 ==="
  if is_running; then
    ps -o pid,etime,rss,cmd -p "$(cat "$PID_FILE")" 2>/dev/null | awk 'NR==1{print} NR>1{printf "%s %s %.1fMB %s\n",$1,$2,$3/1024,$4}'
  fi
  echo "--- Chromium 进程 ---"
  ps -eo pid,rss,cmd 2>/dev/null | grep -i "[c]hrome\|[c]hromium" | awk '{printf "%s %.1fMB\n",$1,$2/1024}' | head -10
  echo "--- 系统内存 ---"
  free -h 2>/dev/null | head -3
}

do_restart() {
  do_stop
  sleep 1
  do_start_bg
}

menu() {
  while true; do
    echo ""
    echo "======================================"
    echo "  云逸续火花助手 · 服务管理"
    echo "======================================"
    is_running && echo -e "  状态: ${C_G}运行中${C_N} (PID $(cat "$PID_FILE" 2>/dev/null))" \
                || echo -e "  状态: ${C_R}未运行${C_N}"
    echo "  1) 后台启动（退出 SSH 可用）"
    echo "  2) 前台启动（看实时输出）"
    echo "  3) 强制停止"
    echo "  4) 重启"
    echo "  5) 运行状态"
    echo "  6) 跟踪日志"
    echo "  7) 内存占用"
    echo "  0) 退出菜单"
    read -r -p "请选择 [0-7]: " choice
    case "$choice" in
      1) do_start_bg ;;
      2) do_start_fg ;;
      3) do_stop ;;
      4) do_restart ;;
      5) do_status ;;
      6) do_logs ;;
      7) do_mem ;;
      0) exit 0 ;;
      *) warn "无效选择" ;;
    esac
  done
}

case "${1:-menu}" in
  start)   do_start_bg ;;
  stop)    do_stop ;;
  restart) do_restart ;;
  status)  do_status ;;
  fg)      do_start_fg ;;
  logs)    do_logs ;;
  mem)     do_mem ;;
  menu)    menu ;;
  *)       echo "用法: bash manage.sh {start|stop|restart|status|fg|logs|mem|menu}"; exit 1 ;;
esac
