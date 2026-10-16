#!/usr/bin/env bash
# cadence relay VM 侧脚本：由 relay.ps1 经 podman machine cp/ssh 以 root 调用，
# 或由用户在交互 root WSL 窗口直接执行（固化入口，见 devbox/README.md §2.6）。
# 用法：relay-vm.sh setup [--force] [--retire <旧服务表>] | status | teardown
# 服务表 /etc/cadence-relay.env：每行 "<name> <vmport> <bindaddr> <hostport>"
# （relay.ps1 生成，或按 README §2.6 手工写入；服务名与 vmport 不得重复）。
# 端口被占且未带 --force 时 exit 3；--force 仅接管身份完全一致的前台 socat：
#   readlink /proc/<pid>/exe 的 basename 必须是 socat，且 NUL 边界 argv 恰为
#   [socat, TCP-LISTEN:<vmport>,bind=<bindaddr>,fork,reuseaddr, TCP:127.0.0.1:<hostport>]
#   三参（kill 前重核）。任何 argv/身份对不上、或归属任何 systemd unit（本方案
#   自有实例/旧版单 unit 除外）的占用者一律拒绝：不杀、不停、不删。
# setup 顺序：服务表（重复预检）→ 工具 → bind 在网卡 → systemd 可用性 →
# 自有实例回收（wants 扫描：不在新服务表的 cadence-relay@* 且所有权核验通过
# 才 disable+清链接，未知一律不动；回收表达删除意图，不参与事务回滚）→
# 只读占用分析 → 事务快照（被覆盖文件 + 各实例 enabled/active）→ 写文件 →
# 旧版单 unit 安全迁移（Description 所有权核验）→ 精确接管 → enable+restart →
# 逐服务验证。任一步失败按快照回滚：还原旧文件、恢复原实例 enabled/active、
# 按原命令行恢复被接管监听；不卸载 socat 包。
# 持久化：systemd 可用时每服务一个 cadence-relay@<name>.service 模板实例，
# run 脚本 exec socat 前台运行，Restart=always + RestartSec=5 +
# StartLimitIntervalSec=0（网桥未就绪自动重试；单服务故障互不影响）；
# systemd 不可用时 wsl.conf [boot] 兜底：监督循环每 5 秒巡检、监听消失即重拉
# （等价 Restart=always 的自恢复；外来 [boot] 段时显式失败）。
# --retire <file>（relay.ps1 生成旧服务表，同 4 列格式）：boot 兜底模式下按
# 三参核验回收被删/改端口服务的自有 socat 监听（systemd 模式由实例回收覆盖，
# 对账为幂等 no-op）；未知监听一律不动。
set -u

ENV_FILE=${RELAY_ENV_FILE:-/etc/cadence-relay.env}
RUN_SCRIPT=${RELAY_RUN_SCRIPT:-/usr/local/bin/cadence-relay-run}
BOOT_SCRIPT=${RELAY_BOOT_SCRIPT:-/usr/local/bin/cadence-relay-boot}
UNIT_TEMPLATE=${RELAY_UNIT_TEMPLATE:-/etc/systemd/system/cadence-relay@.service}
UNIT_DIR=$(dirname "$UNIT_TEMPLATE")
UNIT_WANTS_DIR="$UNIT_DIR/multi-user.target.wants"
OLD_UNIT="$UNIT_DIR/cadence-relay.service"
OLD_WANTS="$UNIT_WANTS_DIR/cadence-relay.service"
LOG_FILE=${RELAY_LOG_FILE:-/var/log/cadence-relay.log}
WSL_CONF=${RELAY_WSL_CONF:-/etc/wsl.conf}
UNIT=cadence-relay
RETIRE_FILE=${RELAY_RETIRE_FILE:-/etc/cadence-relay.retired.env}

RELAY_LINES=()
RELAY_NAMES=()
RELAY_PORTS=()
RETIRE_LINES=()   # --retire 旧服务表（boot 兜底回收对账用）
TAKEOVER_ARGV=()
UNKNOWN_INFO=()
NEW_FILES=()
SNAP_FILES=()
SNAP_ENABLED=()   # 快照时已 enabled 的自有单元（全名，回滚恢复 enable）
SNAP_ACTIVE=()    # 快照时已 active 的自有单元（全名，回滚恢复 restart）
NEW_ENABLED=()    # 本轮新 enable 的自有单元（全名，回滚 disable）
SNAP_DIR=
SNAP_WSL_EXISTED=0      # 快照时 wsl.conf 是否存在（回滚还原内容或删除）
SNAP_SUPERV_EXISTED=0   # 快照时自有 boot 监督是否在跑（回滚重启）

log() { printf '[cadence-relay] %s\n' "$*"; }
die() { printf '[cadence-relay][错误] %s\n' "$*" >&2; exit 1; }
die3() { printf '[cadence-relay][冲突] %s\n' "$*" >&2; exit 3; }

read_env() {
  RELAY_LINES=()
  RELAY_NAMES=()
  RELAY_PORTS=()
  [ -f "$ENV_FILE" ] || die "缺 $ENV_FILE（由 relay.ps1 生成，或按 README §2.6 手工写入）"
  local name vmport bindaddr hostport
  while read -r name vmport bindaddr hostport; do
    case "$name" in '' | '#'* ) continue ;; esac
    hostport=${hostport%$'\r'}
    if [ -z "${vmport:-}" ] || [ -z "${bindaddr:-}" ] || [ -z "${hostport:-}" ]; then
      die "env 行格式错误（应为 name vmport bindaddr hostport）：$name"
    fi
    case "$name" in
      *[!A-Za-z0-9_-]*) die "服务名限字母数字与 _-（systemd 实例名）：$name" ;;
    esac
    case " ${RELAY_NAMES[*]:-} " in *" $name "*) die "服务名重复：$name（每服务一行）" ;; esac
    case " ${RELAY_PORTS[*]:-} " in *" $vmport "*) die "vmport 重复：$vmport（每行监听端口须唯一）" ;; esac
    RELAY_LINES+=("$name $vmport $bindaddr $hostport")
    RELAY_NAMES+=("$name")
    RELAY_PORTS+=("$vmport")
  done < "$ENV_FILE"
  [ "${#RELAY_LINES[@]}" -gt 0 ] || die "$ENV_FILE 无有效服务行"
}

# 监听某端口的 pid 列表（可能为空）
port_busy_pids() {
  ss -tlnp 2>/dev/null | awk -v p=":$1\$" '$4 ~ p' | grep -oP 'pid=\K[0-9]+' || true
}

listener_up() { ss -tln 2>/dev/null | awk -v p=":$1\$" '$4 ~ p' | grep -q .; }

wait_listener() { # 端口就绪轮询（Restart=always 重试期间 bind 可能需数秒）
  local port=$1
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    listener_up "$port" && return 0
    sleep 1
  done
  return 1
}

pid_argv() { tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null; }

is_expected_socat() { # pid vmport bindaddr hostport：exe + NUL argv 完全一致才 0
  local pid=$1 want_l want_t exe item
  want_l="TCP-LISTEN:$2,bind=$3,fork,reuseaddr"
  want_t="TCP:127.0.0.1:$4"
  exe=$(readlink "/proc/$pid/exe" 2>/dev/null) || return 1
  [ "${exe##*/}" = "socat" ] || return 1
  local argv=()
  while IFS= read -r -d '' item; do argv+=("$item"); done < "/proc/$pid/cmdline" 2>/dev/null
  [ "${#argv[@]}" -eq 3 ] || return 1
  [ "${argv[0]##*/}" = "socat" ] || return 1
  [ "${argv[1]}" = "$want_l" ] || return 1
  [ "${argv[2]}" = "$want_t" ] || return 1
  return 0
}

unit_of_pid() { # pid 所属 systemd 单元全名（如 cadence-relay@mysql.service /
  # socat-mysql-relay.service）；无单元归属（前台进程）则空
  local svc
  svc=$(grep -aoP '[A-Za-z0-9_.@-]+\.service' "/proc/$1/cgroup" 2>/dev/null | head -1)
  printf '%s' "${svc:-}"
}

write_run_script() {
  cat > "$RUN_SCRIPT" <<HEADER
#!/usr/bin/env bash
# cadence relay 单服务监听器（relay-vm.sh 生成）：查服务表后 exec socat 前台运行。
# 由 systemd 模板实例 cadence-relay@<name>.service 或 boot 兜底监督调用；
# exec 保证 MainPID 即监听进程本身。
set -u
ENV_FILE='$ENV_FILE'
name=\${1:-}
[ -n "\$name" ] || { printf '[cadence-relay-run] 缺服务名参数\n' >&2; exit 64; }
[ -f "\$ENV_FILE" ] || { printf '[cadence-relay-run] 缺 %s\n' "\$ENV_FILE" >&2; exit 66; }
SOCAT=\$(command -v socat || echo /usr/sbin/socat)
line=\$(awk -v n="\$name" '\$1 == n { print; exit }' "\$ENV_FILE")
[ -n "\$line" ] || { printf '[cadence-relay-run] %s 不在服务表 %s\n' "\$name" "\$ENV_FILE" >&2; exit 66; }
read -r _ vmport bindaddr hostport <<<"\$line"
hostport=\${hostport%\$'\r'}
exec "\$SOCAT" "TCP-LISTEN:\$vmport,bind=\$bindaddr,fork,reuseaddr" "TCP:127.0.0.1:\$hostport"
HEADER
  chmod +x "$RUN_SCRIPT"
}

write_unit_template() {
  cat > "$UNIT_TEMPLATE" <<EOF
[Unit]
Description=cadence relay %i: VM bridge listener -> Windows portproxy
After=network.target
StartLimitIntervalSec=0

[Service]
ExecStart=$RUN_SCRIPT %i
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

write_boot_script() {
  cat > "$BOOT_SCRIPT" <<HEADER
#!/bin/sh
# cadence relay boot 兜底监督（relay-vm.sh 生成）：systemd 不可用时代替
# Restart=always——每 5 秒巡检服务表，监听消失即重拉（网桥晚就绪自恢复）。
ENV_FILE='$ENV_FILE'
RUN_SCRIPT='$RUN_SCRIPT'
LOG_FILE='$LOG_FILE'
while :; do
  while read -r name vmport rest; do
    case "\$name" in ''|'#'*) continue ;; esac
    if ! ss -tln 2>/dev/null | awk -v p=":\$vmport\$" '\$4 ~ p' | grep -q .; then
      nohup "\$RUN_SCRIPT" "\$name" >>"\$LOG_FILE" 2>&1 &
    fi
  done < "\$ENV_FILE"
  sleep 5
done
HEADER
  chmod +x "$BOOT_SCRIPT"
}

relay_supervisor_pids() { # 自有监督：exe 为 shell 解释器且完整 argv 恰为
  # [解释器, BOOT_SCRIPT] 两项——argv 仅“包含”该路径不足以判定（编辑器
  # 打开同一文件形态相同），必须解释器本体执行脚本本身
  local d pid exe item argv=()
  for d in /proc/[0-9]*; do
    pid=${d#/proc/}
    exe=$(readlink "$d/exe" 2>/dev/null) || continue
    case "${exe##*/}" in sh | bash | dash) ;; *) continue ;; esac
    argv=()
    while IFS= read -r -d '' item; do argv+=("$item"); done < "$d/cmdline" 2>/dev/null
    [ "${#argv[@]}" -eq 2 ] || continue
    [ "${argv[1]}" = "$BOOT_SCRIPT" ] || continue
    printf '%s\n' "$pid"
  done
}

kill_relay_supervisor() { # 停自有 boot 监督（与 relay_supervisor_pids 同构判定，
  # 只杀 argv 恰为 [解释器, BOOT_SCRIPT] 的进程，编辑器等相似形态不受影响）；
  # 有界等待退出后再返回，防 teardown 清完监听后被监督重拉
  local p w=0
  for p in $(relay_supervisor_pids); do
    kill "$p" 2>/dev/null || true
  done
  while [ -n "$(relay_supervisor_pids)" ] && [ "$w" -lt 10 ]; do
    sleep 1
    w=$((w + 1))
  done
  if [ -n "$(relay_supervisor_pids)" ]; then
    log "警告：boot 监督 ${w} 秒内未退出（疑似被挂起），继续后续清理"
  fi
}

hook_wslconf() { # 移除旧钩子行后追加（保留其他内容）；存在外来 [boot] 段时拒绝
  if grep -q '^\[boot\]' "$WSL_CONF" 2>/dev/null && ! grep -q cadence-relay-boot "$WSL_CONF" 2>/dev/null; then
    die "$WSL_CONF 已有外来 [boot] 段，拒绝改动；请改用 systemd 路线或手工设 command = $BOOT_SCRIPT"
  fi
  if [ -f "$WSL_CONF" ]; then
    grep -v cadence-relay-boot "$WSL_CONF" > "$WSL_CONF.tmp" || true
    mv "$WSL_CONF.tmp" "$WSL_CONF"
  fi
  printf '\n[boot]\ncommand = %s\n' "$BOOT_SCRIPT" >> "$WSL_CONF"
}

unhook_wslconf() { # 只删本方案钩子行，其余内容原样保留
  if [ -f "$WSL_CONF" ] && grep -q cadence-relay-boot "$WSL_CONF" 2>/dev/null; then
    grep -v cadence-relay-boot "$WSL_CONF" > "$WSL_CONF.tmp" || true
    mv "$WSL_CONF.tmp" "$WSL_CONF"
  fi
}

rollback_setup() { # $1=use_systemd：事务回滚（快照文件/单元状态/监督/被接管监听）
  local nm f argv line name vmport bindaddr hostport p
  log "setup 未通过，回滚：恢复接管前状态"
  if [ "$1" -ne 1 ]; then
    kill_relay_supervisor
    for line in ${RELAY_LINES[@]+"${RELAY_LINES[@]}"}; do
      read -r name vmport bindaddr hostport <<<"$line"
      for p in $(port_busy_pids "$vmport"); do
        if is_expected_socat "$p" "$vmport" "$bindaddr" "$hostport"; then
          kill "$p" 2>/dev/null || true
        fi
      done
    done
  fi
  # 还原被覆盖文件，删除本轮新建文件
  for f in ${SNAP_FILES[@]+"${SNAP_FILES[@]}"}; do
    cp -p "$SNAP_DIR/snap-$(basename "$f")" "$f" 2>/dev/null || true
  done
  rm -f ${NEW_FILES[@]+"${NEW_FILES[@]}"}
  # wsl.conf 按快照恢复：原存在→原样还原（含旧钩子）；原不存在→删除本轮创建
  if [ "$SNAP_WSL_EXISTED" -eq 1 ]; then
    cp -p "$SNAP_DIR/snap-wsl.conf" "$WSL_CONF" 2>/dev/null || true
  else
    rm -f "$WSL_CONF"
  fi
  if [ "$1" -eq 1 ]; then
    for nm in ${NEW_ENABLED[@]+"${NEW_ENABLED[@]}"}; do
      systemctl disable --now "$nm" >/dev/null 2>&1 || true
    done
    systemctl daemon-reload >/dev/null 2>&1 || true
    for nm in ${SNAP_ENABLED[@]+"${SNAP_ENABLED[@]}"}; do
      systemctl enable "$nm" >/dev/null 2>&1 || true
    done
    for nm in ${SNAP_ACTIVE[@]+"${SNAP_ACTIVE[@]}"}; do
      systemctl restart "$nm" >/dev/null 2>&1 || true
    done
  elif [ "$SNAP_SUPERV_EXISTED" -eq 1 ]; then
    # 恢复快照时在跑的原有 boot 监督（此刻 BOOT_SCRIPT 已还原为快照内容）
    nohup "$BOOT_SCRIPT" >>"$LOG_FILE" 2>&1 &
    log "已恢复原有 boot 监督（按快照脚本）"
  fi
  if [ "${#TAKEOVER_ARGV[@]}" -gt 0 ]; then
    for argv in "${TAKEOVER_ARGV[@]}"; do
      # shellcheck disable=SC2086  # is_expected_socat 已核验 argv 各项均无空格
      nohup $argv >>"$LOG_FILE" 2>&1 &
    done
    log "已按原命令行恢复被接管的监听（原前台窗口不再持有该进程）"
  fi
  rm -rf "${SNAP_DIR:-/nonexistent}" 2>/dev/null || true
  return 0
}

recycle_stale_instances() { # wants 中自有 cadence-relay@* 实例不在新服务表 → 回收
  # （disable --now + 清链接），服务删除的 VM 侧对称清理；所有权核验：链接须
  # 解析到本方案模板，且模板存在时其 Description 为本方案产物——未知/外来
  # 链接一律不动（宁残留勿误删）。回收先于占用分析：改名/重推导端口后旧实例
  # 占着新端口时可先腾出；不参与事务回滚（删除是用户明确意图，重跑可再建）
  [ -d "$UNIT_WANTS_DIR" ] || return 0
  local link inst target tmpl
  tmpl=$(readlink -f "$UNIT_TEMPLATE" 2>/dev/null) || tmpl=$UNIT_TEMPLATE
  for link in "$UNIT_WANTS_DIR/$UNIT"@*.service; do
    [ -L "$link" ] || [ -e "$link" ] || continue
    inst=${link##*/}
    inst=${inst#"$UNIT"@}
    inst=${inst%.service}
    case " ${RELAY_NAMES[*]:-} " in *" $inst "*) continue ;; esac
    target=$(readlink -f "$link" 2>/dev/null) || target=
    if [ -z "$target" ] || [ "$target" != "$tmpl" ]; then
      log "跳过 $UNIT@$inst：wants 链接非指向本方案模板，不动"
      continue
    fi
    if [ -f "$UNIT_TEMPLATE" ] && ! grep -q '^Description=cadence relay' "$UNIT_TEMPLATE" 2>/dev/null; then
      log "跳过 $UNIT@$inst：模板 Description 非本方案产物，不动"
      continue
    fi
    log "回收自有实例 $UNIT@$inst（不在新服务表）"
    systemctl disable --now "$UNIT@$inst" >/dev/null 2>&1 || true
    rm -f "$link"
  done
}

read_retire_env() { # --retire 旧服务表（与主表同 4 列格式）：只解析行，杀前核验
  RETIRE_LINES=()
  local name vmport bindaddr hostport
  while read -r name vmport bindaddr hostport; do
    case "$name" in '' | '#'* ) continue ;; esac
    hostport=${hostport%$'\r'}
    if [ -z "${vmport:-}" ] || [ -z "${bindaddr:-}" ] || [ -z "${hostport:-}" ]; then
      die "retire 表行格式错误（应为 name vmport bindaddr hostport）：$name"
    fi
    RETIRE_LINES+=("$name $vmport $bindaddr $hostport")
  done < "$1"
}

retire_stale_listeners() { # 旧服务表对账：旧端口上身份一致的自有 socat（三参核验）
  # 才 kill——boot 兜底模式下被删/改端口服务监听的对称回收；systemd 模式已由
  # recycle_stale_instances 停实例，此处通常 no-op（幂等）；未知监听一律不动
  local line name vmport bindaddr hostport p
  for line in ${RETIRE_LINES[@]+"${RETIRE_LINES[@]}"}; do
    read -r name vmport bindaddr hostport <<<"$line"
    for p in $(port_busy_pids "$vmport"); do
      if is_expected_socat "$p" "$vmport" "$bindaddr" "$hostport"; then
        kill "$p" 2>/dev/null || true
        log "回收旧监听 $name :$vmport（删除/改端口遗留）"
      else
        log "跳过旧端口 :$vmport 上的未知监听 pid=$p（非本方案，不处理）"
      fi
    done
  done
}

cmd_setup() {
  local force=0 retire_env=
  while [ $# -gt 0 ]; do
    case "$1" in
      --force) force=1 ;;
      --retire)
        [ $# -ge 2 ] || die "--retire 需要旧服务表文件参数"
        retire_env=$2
        shift
        ;;
      *) die "未知参数：$1（仅支持 --force / --retire <file>）" ;;
    esac
    shift
  done

  # ── ① 服务表与所需工具：任何停止/接管之前 ──
  read_env
  command -v ss >/dev/null 2>&1 || die "缺少 ss（iproute2）"
  command -v socat >/dev/null 2>&1 || dnf -y install socat >/dev/null 2>&1 \
    || die "socat 安装失败（VM 需联网）"
  command -v systemctl >/dev/null 2>&1 || die "缺少 systemctl"
  local line name vmport bindaddr hostport
  if command -v ip >/dev/null 2>&1; then
    for line in "${RELAY_LINES[@]}"; do
      read -r name vmport bindaddr hostport <<<"$line"
      if ! ip -o addr 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | grep -qx "$bindaddr"; then
        die "bind 地址 $bindaddr 不在本机网卡上（podman 网桥未创建或已漂移）——请先起 stack；网络重建后须重跑 setup"
      fi
    done
  fi

  # ── ② 持久化方式探测：先于任何停止/接管 ──
  local state use_systemd=1
  state=$(systemctl is-system-running 2>/dev/null) || true
  case "$state" in
    running | degraded) ;;
    *)
      use_systemd=0
      log "systemd 不可用（state=${state:-none}）→ wsl.conf [boot] 兜底模式"
      if grep -q '^\[boot\]' "$WSL_CONF" 2>/dev/null && ! grep -q cadence-relay-boot "$WSL_CONF" 2>/dev/null; then
        die "$WSL_CONF 已有外来 [boot] 段，拒绝改动；请在交互窗口改用 systemd 路线或手工配置"
      fi
      ;;
  esac

  # ── ②.5 实例/监听回收：不在新服务表的先停再分析占用（腾出新端口）──
  if [ -n "$retire_env" ]; then
    [ -f "$retire_env" ] || die "--retire 文件不存在：$retire_env"
    read_retire_env "$retire_env"
  fi
  recycle_stale_instances
  retire_stale_listeners

  # ── ③ 占用分析（只读；分类完成前不动任何进程）──
  TAKEOVER_ARGV=()
  UNKNOWN_INFO=()
  local takeover_pids=() takeover_specs=() p unit
  for line in "${RELAY_LINES[@]}"; do
    read -r name vmport bindaddr hostport <<<"$line"
    for p in $(port_busy_pids "$vmport"); do
      unit=$(unit_of_pid "$p")
      if [ -n "$unit" ]; then
        if [ "$unit" = "$UNIT@$name.service" ] || [ "$unit" = "$UNIT.service" ]; then
          : # 本方案单元（新版实例/旧版单 unit）：enable+restart 或 ④ 迁移，无需 kill
        else
          UNKNOWN_INFO+=("pid=$p unit=$unit（外来/其它 unit，一律不动） $(pid_argv "$p")")
        fi
      elif is_expected_socat "$p" "$vmport" "$bindaddr" "$hostport"; then
        takeover_pids+=("$p")
        takeover_specs+=("$vmport $bindaddr $hostport")
        TAKEOVER_ARGV+=("$(pid_argv "$p")")
      else
        UNKNOWN_INFO+=("pid=$p（exe/argv 与服务表不一致） $(pid_argv "$p")")
      fi
    done
  done

  if [ "${#UNKNOWN_INFO[@]}" -gt 0 ]; then
    log "以下监听与本方案服务表不一致（--force 也不会动它）："
    for p in "${UNKNOWN_INFO[@]}"; do log "  $p"; done
    die3 "请先手工处理上述占用者，或修正 $ENV_FILE 后重跑"
  fi
  if [ "${#takeover_pids[@]}" -gt 0 ] && [ "$force" -eq 0 ]; then
    log "以下前台 socat 身份与服务表完全一致，可接管（断现有连接，约 1-5 秒空档）："
    for p in "${takeover_pids[@]}"; do log "  pid=$p $(pid_argv "$p")"; done
    die3 "确认接管请带 --force 重跑"
  fi

  # ── ④ 事务快照与持久化文件 ──
  SNAP_DIR=$(mktemp -d 2>/dev/null) || die "mktemp 失败（无法建立回滚快照）"
  SNAP_FILES=() SNAP_ENABLED=() SNAP_ACTIVE=() NEW_ENABLED=()
  SNAP_WSL_EXISTED=0 SNAP_SUPERV_EXISTED=0
  local sf nm
  for sf in "$RUN_SCRIPT" "$UNIT_TEMPLATE" "$BOOT_SCRIPT" "$OLD_UNIT"; do
    if [ -f "$sf" ]; then
      cp -p "$sf" "$SNAP_DIR/snap-$(basename "$sf")" || { rm -rf "$SNAP_DIR"; die "快照备份失败：$sf"; }
      SNAP_FILES+=("$sf")
    fi
  done
  # wsl.conf 原样快照（含“原不存在”状态；boot 分支 hook 会改写它）
  if [ -f "$WSL_CONF" ]; then
    cp -p "$WSL_CONF" "$SNAP_DIR/snap-wsl.conf" || { rm -rf "$SNAP_DIR"; die "快照备份失败：$WSL_CONF"; }
    SNAP_WSL_EXISTED=1
  fi
  # 原有自有 boot 监督存活状态（boot 分支会先停它防竞拉，回滚须恢复）
  [ -n "$(relay_supervisor_pids)" ] && SNAP_SUPERV_EXISTED=1
  if [ "$use_systemd" -eq 1 ]; then
    systemctl is-enabled "$UNIT" >/dev/null 2>&1 && SNAP_ENABLED+=("$UNIT")
    systemctl is-active "$UNIT" >/dev/null 2>&1 && SNAP_ACTIVE+=("$UNIT")
    for nm in ${RELAY_NAMES[@]+"${RELAY_NAMES[@]}"}; do
      systemctl is-enabled "$UNIT@$nm" >/dev/null 2>&1 && SNAP_ENABLED+=("$UNIT@$nm")
      systemctl is-active "$UNIT@$nm" >/dev/null 2>&1 && SNAP_ACTIVE+=("$UNIT@$nm")
    done
  fi



  NEW_FILES=()
  [ -e "$RUN_SCRIPT" ] || NEW_FILES+=("$RUN_SCRIPT")
  write_run_script
  if [ "$use_systemd" -eq 1 ]; then
    # 旧版单 unit 安全迁移：所有权核验（Description 前缀）后停用移除，回滚可还原
    if [ -f "$OLD_UNIT" ]; then
      if grep -q '^Description=cadence relay' "$OLD_UNIT"; then
        systemctl stop "$UNIT" >/dev/null 2>&1 || true
        systemctl disable "$UNIT" >/dev/null 2>&1 || true
        rm -f "$OLD_UNIT" "$OLD_WANTS"
      else
        rm -rf "$SNAP_DIR"
        die "$OLD_UNIT 存在但 Description 非本方案产物，拒绝迁移；请手工处理"
      fi
    fi
    [ -e "$UNIT_TEMPLATE" ] || NEW_FILES+=("$UNIT_TEMPLATE")
    write_unit_template
    systemctl daemon-reload >/dev/null 2>&1 \
      || { rollback_setup 1; die "daemon-reload 失败"; }
  else
    kill_relay_supervisor # 幂等：先停旧监督，避免双监督竞拉
    [ -e "$BOOT_SCRIPT" ] || NEW_FILES+=("$BOOT_SCRIPT")
    write_boot_script
    hook_wslconf
  fi

  # ── ⑤ 精确接管：kill 前重核身份，只杀 ③ 核验一致的前台 socat ──
  local idx t_vp t_bd t_hp
  for idx in "${!takeover_pids[@]}"; do
    read -r t_vp t_bd t_hp <<<"${takeover_specs[$idx]}"
    if is_expected_socat "${takeover_pids[$idx]}" "$t_vp" "$t_bd" "$t_hp"; then
      kill "${takeover_pids[$idx]}" 2>/dev/null || true
    else
      log "pid ${takeover_pids[$idx]} 接管前身份变化，跳过（不杀）"
    fi
  done
  if [ "${#takeover_pids[@]}" -gt 0 ]; then
    local w
    for line in "${RELAY_LINES[@]}"; do
      read -r name vmport _ _ <<<"$line"
      w=0
      while [ -n "$(port_busy_pids "$vmport")" ] && [ "$w" -lt 10 ]; do
        sleep 1
        w=$((w + 1))
      done
    done
  fi

  # ── ⑥ 启动并验证；失败即回滚 ──
  if [ "$use_systemd" -eq 1 ]; then
    local full was_enabled
    for name in "${RELAY_NAMES[@]}"; do
      full="$UNIT@$name"
      case " ${SNAP_ENABLED[*]:-} " in *" $full "*) was_enabled=1 ;; *) was_enabled=0 ;; esac
      if ! systemctl enable "$full" >/dev/null 2>&1; then
        rollback_setup 1
        die "enable $full 失败"
      fi
      [ "$was_enabled" -eq 1 ] || NEW_ENABLED+=("$full")
    done
    for name in "${RELAY_NAMES[@]}"; do
      if ! systemctl restart "$UNIT@$name" >/dev/null 2>&1; then
        rollback_setup 1
        die "restart $UNIT@$name 失败"
      fi
    done
  else
    nohup "$BOOT_SCRIPT" >>"$LOG_FILE" 2>&1 &
  fi

  local fail=0
  for line in "${RELAY_LINES[@]}"; do
    read -r name vmport _ _ <<<"$line"
    if wait_listener "$vmport"; then
      log "$name: VM 监听 :$vmport OK"
    else
      log "$name: VM 监听 :$vmport FAIL"
      fail=1
    fi
  done
  if [ "$fail" -eq 1 ]; then
    rollback_setup "$use_systemd"
    exit 1
  fi
  rm -rf "$SNAP_DIR" 2>/dev/null || true
  if [ "$use_systemd" -eq 1 ]; then
    log "持久化：每服务 systemd 单元 $UNIT@<name>（Restart=always，开机自启）"
  else
    log "持久化：wsl.conf [boot] 兜底监督（5 秒巡检自恢复；本环境 systemd 非交互不可用）"
  fi
}

cmd_status() {
  read_env
  local state have_systemd=0 line name vmport bindaddr hostport fail=0
  local pids mainpid
  state=$(systemctl is-system-running 2>/dev/null) || true
  case "$state" in
    running | degraded) have_systemd=1 ;;
    *) log "systemd 不可用（state=${state:-none}），跳过单元检查（boot 兜底监督模式）" ;;
  esac
  for line in "${RELAY_LINES[@]}"; do
    read -r name vmport bindaddr hostport <<<"$line"
    hostport=${hostport%$'\r'}
    pids=$(port_busy_pids "$vmport")
    if [ -z "$pids" ]; then
      log "$name: VM 监听 :$vmport FAIL（重跑 setup 重建）"
      fail=1
    else
      log "$name: VM 监听 :$vmport OK（pid ${pids//$'\n'/ }）"
      if [ "$have_systemd" -eq 1 ]; then
        mainpid=$(systemctl show "$UNIT@$name" -p MainPID --value 2>/dev/null | tr -d '[:space:]')
        if [ "${mainpid:-0}" != "0" ] && [ "$pids" = "$mainpid" ]; then
          log "$name: $UNIT@$name active（MainPID=$mainpid = 监听 pid，自恢复在管）"
        else
          log "$name: 监听 pid 与 $UNIT@$name MainPID=${mainpid:-?} 不一致——疑似前台 socat 而非 systemd 托管，重跑 setup 可接管"
          fail=1
        fi
      fi
    fi
    if command -v ip >/dev/null 2>&1; then
      if ip -o addr 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | grep -qx "$bindaddr"; then
        log "$name: bind $bindaddr 在网卡上 OK"
      else
        log "$name: bind $bindaddr 不在本机网卡上——podman 网络已重建（网关漂移），请重跑 setup 重绑"
        fail=1
      fi
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
  # 只管理自有痕迹：boot 监督、cadence-relay@ 实例与模板、run/boot 脚本、服务表、
  # 日志、wsl.conf 钩子行、旧版单 unit。不 pkill -f、不卸载 socat 包、不动未知
  # 监听与无关 unit、不动 Windows 侧 portproxy（如需删除请在 Windows 侧处理）。
  local line name vmport bindaddr hostport p link inst
  kill_relay_supervisor
  if [ -f "$ENV_FILE" ]; then
    while read -r name vmport bindaddr hostport; do
      case "$name" in '' | '#'* ) continue ;; esac
      hostport=${hostport%$'\r'}
      systemctl disable --now "$UNIT@$name" >/dev/null 2>&1 || true
      for p in $(port_busy_pids "$vmport"); do
        if is_expected_socat "$p" "$vmport" "$bindaddr" "$hostport"; then
          kill "$p" 2>/dev/null || true
        else
          log "跳过 :$vmport 上的未知监听 pid=$p（非本方案，不处理）"
        fi
      done
    done < "$ENV_FILE"
  fi
  # env 缺失/损坏时按 wants 链接兜底停实例
  if [ -d "$UNIT_WANTS_DIR" ]; then
    for link in "$UNIT_WANTS_DIR/$UNIT"@*.service; do
      [ -e "$link" ] || continue
      inst=${link##*/}
      inst=${inst#"$UNIT"@}
      inst=${inst%.service}
      systemctl disable --now "$UNIT@$inst" >/dev/null 2>&1 || true
      rm -f "$link"
    done
  fi
  # 旧版单 unit 兼容清理
  systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
  rm -f "$UNIT_TEMPLATE" "$OLD_UNIT" "$OLD_WANTS" "$RUN_SCRIPT" "$BOOT_SCRIPT" "$ENV_FILE" "$RETIRE_FILE" "$LOG_FILE"
  unhook_wslconf
  systemctl daemon-reload >/dev/null 2>&1 || true
  log "teardown 完成：监督/systemd 单元（模板+实例+旧版）/boot 钩子/run 脚本/服务表/日志已清除"
  log "未动：socat 包、未知监听、Windows 侧 portproxy（如需删除请在 Windows 侧处理）"
}

case "${1:-}" in
  setup) shift; cmd_setup "$@" ;;
  status) cmd_status ;;
  teardown) cmd_teardown ;;
  *)
    printf '用法：relay-vm.sh setup [--force] [--retire <旧服务表>] | status | teardown\n' >&2
    exit 64
    ;;
esac
