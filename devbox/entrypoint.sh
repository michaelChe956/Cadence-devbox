#!/usr/bin/env bash
# cadence-entrypoint —— 容器首启编排，顺序固定（Contract 3；设计 4.4/4.5/4.7）：
#   1) JDK 持久状态初始化/校验（空卷→内置版本；已有选择校验后保留；损坏→明确报错且不重置）
#   2) git 对齐默认 + 轮询 env + 卷内依赖源配置刷新
#   3) render-auth 渲染鉴权（失败即拒启 exit 2）
#   4) stack 模板同步（/opt/cadence/stack ↔ $CADENCE_STACK_DIR；CHANGES.md 存在=用户改过→保留；否则分叉文件先备份再覆盖）
#   5) skills 首启安装/幂等更新（软失败：日志落盘+下次启动重试）
#   6) exec "$@"
set -euo pipefail

HOME_DIR="${HOME:?HOME 未设置}"
CADENCE_STACK_DIR="${CADENCE_STACK_DIR:-/cadence/stack}"
CADENCE_STACK_TEMPLATE_DIR="${CADENCE_STACK_TEMPLATE_DIR:-/opt/cadence/stack}"
CADENCE_RENDER_AUTH="${CADENCE_RENDER_AUTH:-/usr/local/lib/cadence/render-auth.py}"
CADENCE_AUTH_FILE="${CADENCE_AUTH_FILE:-/cadence/auth.yaml}"
CADENCE_JDK="${CADENCE_JDK:-/usr/local/bin/jdk}"
CADENCE_INSTALL_MIRRORS=(
  "https://ghfast.top/https://raw.githubusercontent.com/michaelChe956/Cadence-skills/main/install.sh"
  "https://gh-proxy.com/https://raw.githubusercontent.com/michaelChe956/Cadence-skills/main/install.sh"
  "https://raw.githubusercontent.com/michaelChe956/Cadence-skills/main/install.sh"
)
LOG_DIR="$HOME_DIR/.cadence/logs"
mkdir -p "$LOG_DIR"
log() { printf '[cadence-entrypoint] %s\n' "$*" | tee -a "$LOG_DIR/entrypoint.log" >&2; }

# ---------- 步骤 1：JDK 持久状态（仅空卷选内置版本；已有选择校验后保留；损坏明确报错不重置/不阻断启动） ----------
log "步骤 1/5：JDK 持久状态"
if ! "$CADENCE_JDK" init 2>&1 | tee -a "$LOG_DIR/entrypoint.log" >&2; then
  log "警告：JDK 状态校验未通过——按上方 jdk 提示处理（jdk list / jdk use ${CADENCE_BUILTIN_JDK_MAJOR:-21}）；未自动重置已有选择"
fi

# ---------- 步骤 2：git 对齐默认 + 轮询 env + 卷内源配置 ----------
log "步骤 2/5：git 对齐与依赖源配置"
git config --global core.autocrlf true    # 与 git-for-windows 默认一致，避免换行符幻影 diff（设计 4.5）
git config --global core.filemode false   # Windows 盘无执行位语义，避免权限幻影 diff（设计 4.5）
export CHOKIDAR_USEPOLLING=1              # 跨挂载 inotify 不传播：vite/webpack 默认轮询（设计 4.5）
if ! grep -q 'cadence-entrypoint-env' "$HOME_DIR/.bashrc" 2>/dev/null; then
  cat >> "$HOME_DIR/.bashrc" <<'EOF'

# --- cadence-entrypoint-env：docker exec 会话同样拿到轮询与 PATH（幂等块）---
export CHOKIDAR_USEPOLLING=1
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$HOME/.kimi-code/bin:$PATH"
# --- cadence-entrypoint-env 结束 ---
EOF
fi
# ~/.m2、~/.gradle 是 named volume（首挂空卷会遮蔽镜像层）——每次启动幂等重写国内源配置
mkdir -p "$HOME_DIR/.m2" "$HOME_DIR/.gradle"
cat > "$HOME_DIR/.m2/settings.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0">
  <mirrors>
    <mirror>
      <id>aliyun-central</id>
      <mirrorOf>central</mirrorOf>
      <name>aliyun public</name>
      <url>https://maven.aliyun.com/repository/public</url>
    </mirror>
  </mirrors>
</settings>
EOF
cat > "$HOME_DIR/.gradle/init.gradle" <<'EOF'
// aliyun maven 仓库镜像（gradle 发行版下载请看 devbox/README.md 的 tencent 镜像说明）
allprojects {
  repositories {
    maven { url 'https://maven.aliyun.com/repository/public' }
  }
}
buildscript {
  repositories {
    maven { url 'https://maven.aliyun.com/repository/public' }
  }
}
EOF

# ---------- 步骤 3：渲染鉴权（失败即拒启） ----------
log "步骤 3/5：渲染鉴权配置（cadence-box.yaml → 五端）"
if ! python3 "$CADENCE_RENDER_AUTH" --config "$CADENCE_AUTH_FILE" --home "$HOME_DIR"; then
  log "鉴权渲染失败——明细见上方 render-auth 输出：多为 cadence-box.yaml 缺字段/格式错；若是渲染脚本/环境异常，请把 entrypoint 日志反馈维护者。修正后重启容器"
  exit 2
fi

# ---------- 步骤 4：stack 模板同步 ----------
log "步骤 4/5：同步 stack 官方模板"
if [ -f "$CADENCE_STACK_DIR/CHANGES.md" ]; then
  log "检测到 CHANGES.md（用户/agent 已改动）：保留当前 $CADENCE_STACK_DIR，不自动覆盖；新版模板差异请人工比对 /opt/cadence/stack"
else
  mkdir -p "$CADENCE_STACK_DIR"
  # 覆盖前把与模板分叉的本地文件备份为 <名>.bak-<时间戳>：无 CHANGES.md 的手工改动不得静默丢失
  # （2026-09-28 Windows 实测：手工挂载行被模板同步抹掉 2 次）；模板没有的本地文件原样保留（合并语义）。
  while IFS= read -r f; do
    dst="$CADENCE_STACK_DIR/$f"
    if [ -f "$dst" ] && ! cmp -s "$CADENCE_STACK_TEMPLATE_DIR/$f" "$dst"; then
      cp -a "$dst" "$dst.bak-$(date +%Y%m%d%H%M%S)"
      log "警告：$f 与官方模板分叉，已留 .bak- 备份后覆盖——长期保留本地版请在 stack 目录创建 CHANGES.md"
    fi
    mkdir -p "$(dirname "$dst")"
    cp -f "$CADENCE_STACK_TEMPLATE_DIR/$f" "$dst"
  done < <(cd "$CADENCE_STACK_TEMPLATE_DIR" && find compose.yaml docker-compose.no-sock.yml catalog -type f)
  log "stack 模板已同步为镜像内置版本（.env 与 CHANGES.md 不在同步范围）"
fi

# ---------- 步骤 5：skills 首启安装/幂等更新（软失败） ----------
# 2026-10-28 Windows 真机：首跑报 `install.sh: line 2: set: pipefail: invalid option name`
# （脚本第 2 行是 `set -euo pipefail`，说明那次执行的 shell 不认 pipefail；根因未定位，
#  POSIXLY_CORRECT / bash --posix / dash 三种假设均已在本机试过、均不复现）。
# 处置：首跑失败后换绝对路径 /bin/bash + 清掉 POSIXLY_CORRECT 重试一次；
# 仍失败则把 shell 路径、版本与脚本头 3 行打进日志，供下次直接定位。
run_skills_installer() {
  local script=$1
  bash "$script" && return 0
  log "警告：skills 安装脚本首跑失败（shell=$(command -v bash)，$(bash --version | head -1)），用 /bin/bash 重试"
  env -u POSIXLY_CORRECT /bin/bash "$script" && return 0
  log "警告：重试仍失败；脚本开头：$(head -3 "$script" | tr '\n' ' ')"
  return 1
}

boot_skills() {
  local repo="$HOME_DIR/.agents/Cadence-skills" url
  if [ -d "$repo/.git" ]; then
    git -C "$repo" pull --ff-only || return 1
    run_skills_installer "$repo/install.sh" || return 1
  else
    for url in "${CADENCE_INSTALL_MIRRORS[@]}"; do
      curl -fsSL --retry 2 --max-time 60 "$url" -o /tmp/cadence-install.sh || continue
      run_skills_installer /tmp/cadence-install.sh && return 0
    done
    return 1
  fi
}

log "步骤 5/5：安装/更新 Cadence-skills（软失败不阻断）"
if ! boot_skills; then
  log "警告：skills 安装/更新失败——容器与 agent 仍可用；日志已落 $LOG_DIR/entrypoint.log，下次启动自动重试"
fi

# ---------- 步骤 6：移交主进程 ----------
log "就绪：exec $*"
exec "$@"
