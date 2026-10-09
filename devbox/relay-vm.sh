#!/usr/bin/env bash
# cadence relay VM 侧脚本：由 relay.ps1 经 podman machine cp/ssh 以 root 调用，勿手工内嵌长命令执行。
# 用法：relay-vm.sh setup [--force] | status | teardown
# 服务表 /etc/cadence-relay.env：每行 "<name> <vmport> <bindaddr> <hostport>"（relay.ps1 生成）
# 端口被占且未带 --force 时 exit 3（relay.ps1 据此询问用户后重试接管）。
set -u

ENV_FILE=/etc/cadence-relay.env
RUN_SCRIPT=/usr/local/bin/cadence-relay-run
BOOT_SCRIPT=/usr/local/bin/cadence-relay-boot
UNIT_FILE=/etc/systemd/system/cadence-relay.service
WANTS_LINK=/etc/systemd/system/multi-user.target.wants/cadence-relay.service
LOG_FILE=/var/log/cadence-relay.log
WSL_CONF=/etc/wsl.conf

log() { printf '[cadence-relay] %s\n' "$*"; }
die() { printf '[cadence-relay][错误] %s\n' "$*" >&2; exit 1; }
die3() { printf '[cadence-relay][冲突] %s\n' "$*" >&2; exit 3; }

read_env() {
  RELAY_LINES=()
  [ -f "$ENV_FILE" ] || die "缺 $ENV_FILE（由 relay.ps1 生成后 machine cp 进来）"
  local name vmport bindaddr hostport
  while read -r name vmport bindaddr hostport; do
    case "$name" in '' | '#'* ) continue ;; esac
    if [ -z "${vmport:-}" ] || [ -z "${bindaddr:-}" ] || [ -z "${hostport:-}" ]; then
      die "env 行格式错误（应为 name vmport bindaddr hostport）：$name"
    fi
    RELAY_LINES+=("$name $vmport $bindaddr $hostport")
  done < "$ENV_FILE"
  [ "${#RELAY_LINES[@]}" -gt 0 ] || die "$ENV_FILE 无有效服务行"
}

# 监听某端口的 pid 列表（可能为空）
port_busy_pids() {
  ss -tlnp 2>/dev/null | awk -v p=":$1\$" '$4 ~ p' | grep -oP 'pid=\K[0-9]+' || true
}

listener_up() { ss -tln 2>/dev/null | awk -v p=":$1\$" '$4 ~ p' | grep -q .; }

unit_of_pid() { systemctl status "$1" 2>/dev/null | sed -n '1s/^.* \(.*\.service\) - .*$/\1/p'; }

kill_port() { # 停掉监听端口的进程；若属某 systemd unit 则先尝试 stop 该 unit
  local pids p unit
  pids=$(port_busy_pids "$1")
  for p in $pids; do
    unit=$(unit_of_pid "$p")
    [ -z "$unit" ] || systemctl stop "$unit" >/dev/null 2>&1 || true
  done
  pids=$(port_busy_pids "$1")
  for p in $pids; do kill "$p" 2>/dev/null || true; done
  sleep 1
  pids=$(port_busy_pids "$1")
  for p in $pids; do kill -9 "$p" 2>/dev/null || true; done
}

write_run_script() {
  cat > "$RUN_SCRIPT" <<'HEADER'
#!/usr/bin/env bash
# cadence relay 监听器（relay-vm.sh 生成）：先清旧监听再按服务表拉起，幂等
set -u
ENV_FILE=/etc/cadence-relay.env
LOG_FILE=/var/log/cadence-relay.log
SOCAT=$(command -v socat || echo /usr/sbin/socat)
HEADER
  cat >> "$RUN_SCRIPT" <<'BODY'
while read -r name vmport bindaddr hostport; do
  case "$name" in '' | '#'* ) continue ;; esac
  pkill -f "TCP-LISTEN:$vmport" 2>/dev/null || true
  nohup "$SOCAT" "TCP-LISTEN:$vmport,bind=$bindaddr,fork,reuseaddr" \
    "TCP:127.0.0.1:$hostport" >>"$LOG_FILE" 2>&1 &
done < "$ENV_FILE"
wait
BODY
  chmod +x "$RUN_SCRIPT"
}

try_systemd() { # systemd 可用则建服务并 enable --now，成功返回 0
  local state
  state=$(systemctl is-system-running 2>/dev/null) || true
  case "$state" in
    running | degraded) ;;
    *) return 1 ;;
  esac
  cat > "$UNIT_FILE" <<EOF
[Unit]
Description=cadence relay: podman bridge gw -> Windows portproxy (127.0.0.1)
After=network.target

[Service]
ExecStart=$RUN_SCRIPT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload >/dev/null 2>&1 || return 1
  systemctl enable --now cadence-relay >/dev/null 2>&1 || return 1
}

write_boot_script() {
  printf '#!/bin/sh\nnohup %s >>%s 2>&1 &\n' "$RUN_SCRIPT" "$LOG_FILE" > "$BOOT_SCRIPT"
  chmod +x "$BOOT_SCRIPT"
}

hook_wslconf() { # 移除旧钩子行后追加（保留其他内容）；存在外来 [boot] 段时拒绝
  if grep -q '^\[boot\]' "$WSL_CONF" 2>/dev/null && ! grep -q cadence-relay-boot "$WSL_CONF" 2>/dev/null; then
    die "systemd 不可用且 $WSL_CONF 已有外来 [boot] 段，请手工设 command = $BOOT_SCRIPT"
  fi
  if [ -f "$WSL_CONF" ]; then
    grep -v cadence-relay-boot "$WSL_CONF" > "$WSL_CONF.tmp" || true
    mv "$WSL_CONF.tmp" "$WSL_CONF"
  fi
  printf '\n[boot]\ncommand = %s\n' "$BOOT_SCRIPT" >> "$WSL_CONF"
}

cmd_setup() {
  local force=0
  [ "${1:-}" = "--force" ] && force=1
  read_env
  command -v socat >/dev/null 2>&1 || dnf -y install socat >/dev/null 2>&1 \
    || die "socat 安装失败（VM 需联网）"

  local line name vmport bindaddr hostport pids
  for line in "${RELAY_LINES[@]}"; do
    read -r name vmport _ _ <<< "$line"
    pids=$(port_busy_pids "$vmport")
    if [ -n "$pids" ]; then
      if [ "$force" -eq 1 ]; then
        kill_port "$vmport"
      else
        log "端口 $vmport 被占用，占用者："
        ss -tlnp | awk -v p=":$vmport\$" '$4 ~ p'
        die3 "需接管请由 relay.ps1 确认后带 --force 重试"
      fi
    fi
  done

  write_run_script
  if try_systemd; then
    log "持久化：systemd 服务 cadence-relay（enable --now，异常自动重启）"
  else
    write_boot_script
    hook_wslconf
    "$BOOT_SCRIPT"
    log "持久化：wsl.conf [boot] 钩子（本环境 systemd 非交互不可用）"
  fi

  sleep 1
  local fail=0
  for line in "${RELAY_LINES[@]}"; do
    read -r name vmport _ _ <<< "$line"
    if listener_up "$vmport"; then
      log "$name: VM 监听 :$vmport OK"
    else
      log "$name: VM 监听 :$vmport FAIL（详见 $LOG_FILE）"
      fail=1
    fi
  done
  [ "$fail" -eq 0 ] || exit 1
}

cmd_status() {
  read_env
  local line name vmport bindaddr hostport fail=0
  for line in "${RELAY_LINES[@]}"; do
    read -r name vmport _ hostport <<< "$line"
    if listener_up "$vmport"; then
      log "$name: VM 监听 :$vmport OK"
    else
      log "$name: VM 监听 :$vmport FAIL（重跑 relay.ps1）"
      fail=1
    fi
    if timeout 3 bash -c "echo > /dev/tcp/127.0.0.1/$hostport" 2>/dev/null; then
      log "$name: 回环 127.0.0.1:$hostport OK（mirrored → Windows portproxy）"
    else
      log "$name: 回环 127.0.0.1:$hostport FAIL（查 Windows 侧 portproxy 规则）"
      fail=1
    fi
  done
  exit "$fail"
}

cmd_teardown() {
  local name vmport
  if [ -f "$ENV_FILE" ]; then
    while read -r name vmport _ _; do
      case "$name" in '' | '#'* ) continue ;; esac
      kill_port "$vmport"
    done < "$ENV_FILE"
  fi
  pkill -f cadence-relay-run 2>/dev/null || true
  systemctl disable --now cadence-relay >/dev/null 2>&1 || true
  rm -f "$UNIT_FILE" "$WANTS_LINK" "$RUN_SCRIPT" "$BOOT_SCRIPT" "$ENV_FILE" "$LOG_FILE"
  if [ -f "$WSL_CONF" ]; then
    grep -v cadence-relay-boot "$WSL_CONF" > "$WSL_CONF.tmp" || true
    mv "$WSL_CONF.tmp" "$WSL_CONF"
  fi
  dnf -y remove socat >/dev/null 2>&1 || true
  log "teardown 完成：unit/钩子/监听/配置/socat 已清除"
}

case "${1:-}" in
  setup) shift; cmd_setup "$@" ;;
  status) cmd_status ;;
  teardown) cmd_teardown ;;
  *)
    printf '用法：relay-vm.sh setup [--force] | status | teardown\n' >&2
    exit 64
    ;;
esac
