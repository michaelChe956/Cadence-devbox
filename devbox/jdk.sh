#!/usr/bin/env bash
# cadence jdk —— 容器内 JDK 选择入口（设计《OpenJDK 8 按需安装与持久切换》组件 1/2）
#   状态：$JDK_ROOT/current → 所选 JDK 目录（同卷临时链接 + mv -T 原子替换；选择以链接目标为准，无独立状态文件）
#   命令：jdk list / jdk use <主版本>；jdk init 由 entrypoint 每启调用
#         （空卷→内置版本；已有选择校验后保留；目标损坏→明确报错且不重置）
#   环境：JAVA_HOME 固定指向 $JDK_ROOT/current（镜像 ENV，非 .bashrc），current/bin 排在 PATH 前部
#   按需安装：jdk use <清单内主版本> 首次会从清单内可信源下载完整 JDK（见“可信发行物清单”），逐步
#             校验包类型/身份 → 固定 SHA-256 → 安全解包 → 真实 java/javac 版本 → 同卷原子发布 → 原子切换；
#             任一步失败或被中断都清理临时目录，且不改变旧选择、不重复下载已安装版本；
#             SIGKILL/断电来不及清理的 `.install.*` 由下次安装前的过期清理兜底（见 jdk_prune_stale_installs）
set -euo pipefail

JDK_ROOT="${CADENCE_JDK_ROOT:-$(dirname "${JAVA_HOME:-/home/dev/.cadence/jdks/current}")}"
CURRENT="$JDK_ROOT/current"
BUILTIN_MAJOR="${CADENCE_BUILTIN_JDK_MAJOR:-21}"
BUILTIN_DIR="${CADENCE_BUILTIN_JDK_DIR:-/usr/lib/jvm/temurin-${BUILTIN_MAJOR}-jdk-amd64}"
# ---------- 可信发行物清单（Plan Task 1 实测；证据 cadence/analysis-docs/2026-09-23_分析报告_OpenJDK8容器安装验证_v1.0.md）----------
# 来源与校验分离：URL 只回答“从哪里取”，SHA-256 是离线固化的信任锚——取自 Adoptium 公开密钥签名的 apt
# 元数据链（Release → Packages → 包哈希，密钥指纹 3B04 D753 … 65F8 F04B），并经独立下载实测复核。
# 自建镜像/离线导入/自动化测试必须成对覆盖 CADENCE_JDK8_URL 与 CADENCE_JDK8_SHA256（后者须来自对该发行物
# 独立核实的元数据）：不存在静默换源、也没有只放宽校验的路径，实际使用的来源与期望哈希都会打印到 stderr。
JDK8_PACKAGE="temurin-8-jdk"
JDK8_VERSION="8.0.504.0.0+1-0"
JDK8_ARCH="amd64"
JDK8_SHA256="8747c07903772fb7fcfff803e06bd4761124def9d2aef781c98c745c10e6841a"
JDK8_DIR_IN_PKG="usr/lib/jvm/temurin-8-jdk-amd64"
JDK8_URL="https://mirrors.ustc.edu.cn/adoptium/deb/pool/main/t/temurin-8/${JDK8_PACKAGE}_${JDK8_VERSION}_${JDK8_ARCH}.deb"

# 清单内可按需安装的主版本；其余主版本直接判为不支持
jdk_supported_major() {
  case "$1" in
    8) return 0 ;;
    *) return 1 ;;
  esac
}

# 运行期架构 → deb 命名与控制字段使用的架构名
jdk_arch() {
  case "$(uname -m)" in
    x86_64|amd64) printf 'amd64\n' ;;
    aarch64|arm64) printf 'arm64\n' ;;
    *) uname -m ;;
  esac
}

# 安装临时目录：失败与中断都必须清理（成功发布前也显式清理）
JDK_INSTALL_TMP=""
jdk_install_cleanup() {
  if [ -n "$JDK_INSTALL_TMP" ] && [ -d "$JDK_INSTALL_TMP" ]; then
    rm -rf "$JDK_INSTALL_TMP"
  fi
  JDK_INSTALL_TMP=""
  return 0
}
trap 'jdk_install_cleanup' EXIT
trap 'jdk_install_cleanup; exit 130' INT
trap 'jdk_install_cleanup; exit 143' TERM
trap 'jdk_install_cleanup; exit 129' HUP

# 残留安装临时目录：SIGKILL/断电不会走上面的 trap，`.install.*` 会留在状态卷里。下次安装创建自己的
# 临时目录之前清理过期的那些。保守三条件：固定前缀、必须是目录（find -type d 不匹配符号链接，杜绝
# 顺链删除）、且目录 mtime 早于阈值——正在进行的安装（含并发进程）随时写入，mtime 始终新鲜，不会被
# 误删。阈值取 60 分钟：正常安装 < 1 分钟（85MB 实测约 9 秒），余量足够，宁可留着也不误删。
JDK_STALE_TMP_MINUTES=60
jdk_prune_stale_installs() {
  local dir
  [ -d "$JDK_ROOT" ] || return 0
  while IFS= read -r dir; do
    [ -n "$dir" ] || continue
    if rm -rf "$dir"; then
      log "清理残留安装临时目录（超过 ${JDK_STALE_TMP_MINUTES} 分钟未更新）：$dir"
    else
      log "警告：无法清理残留临时目录 $dir（权限？）——继续安装"
    fi
  done < <(find "$JDK_ROOT" -maxdepth 1 -type d -name '.install.*' \
             -mmin "+${JDK_STALE_TMP_MINUTES}" 2>/dev/null)
  return 0
}

log() { printf 'jdk: %s\n' "$*" >&2; }
err() { printf 'jdk: 错误：%s\n' "$*" >&2; }
usage() {
  cat <<'EOF'
用法：
  jdk list          列出内置与已安装版本，并标记当前版本
  jdk use <主版本>  切换当前 JDK（如 jdk use 21）；清单内未安装的版本先按需下载安装（如 jdk use 8）
EOF
}

# ---------- 版本识别 ----------

# 从 java/javac 版本行取主版本：1.8.0_504 → 8；21.0.12 → 21；javac 21.0.12 → 21
jdk_major_of() {
  local line="$1" v
  v=$(printf '%s\n' "$line" | sed -n 's/.*version[[:space:]]*"\([^"]*\)".*/\1/p')
  if [ -z "$v" ]; then
    v=$(printf '%s\n' "$line" | sed -n 's/^[^[:space:]]*[[:space:]]\{1,\}\([0-9][^[:space:]]*\).*/\1/p')
  fi
  [ -n "$v" ] || return 1
  if [ "${v%%.*}" = "1" ]; then v="${v#1.}"; fi
  v="${v%%.*}"
  case "$v" in ''|*[!0-9]*) return 1 ;; esac
  printf '%s\n' "$v"
}

# 执行 bin/java|bin/javac -version 并输出其主版本（真实 java 版本走 stderr）
jdk_probe_major() {
  local bin="$1" out
  out=$("$bin" -version 2>&1 || true)
  out="${out%%$'\n'*}"
  jdk_major_of "$out" || { err "无法识别 $bin 的版本输出：$out"; return 1; }
}

# 校验目录是否为可用完整 JDK（bin/java + bin/javac 存在可执行且主版本一致）；成功时输出主版本
jdk_verify_dir() {
  local dir="$1" java_major javac_major
  [ -d "$dir" ] || { err "$dir 不存在"; return 1; }
  [ -x "$dir/bin/java" ] || { err "$dir 缺少可执行的 bin/java（不是 JDK 安装目录）"; return 1; }
  [ -x "$dir/bin/javac" ] || { err "$dir 缺少可执行的 bin/javac（不是完整 JDK）"; return 1; }
  java_major=$(jdk_probe_major "$dir/bin/java") || return 1
  javac_major=$(jdk_probe_major "$dir/bin/javac") || return 1
  [ "$javac_major" = "$java_major" ] || {
    err "$dir 的 java 主版本 $java_major 与 javac 主版本 $javac_major 不一致"
    return 1
  }
  printf '%s\n' "$java_major"
}

# ---------- 状态读写 ----------

# 稳定入口：current 链接的绝对目标（未设置时无输出）
jdk_current_target() {
  if [ -L "$CURRENT" ]; then readlink -m "$CURRENT"; fi
}

# 当前选择的主版本；无法确定（未设置/损坏）时非零退出
jdk_current_major() {
  local target major
  [ -L "$CURRENT" ] || return 1
  target=$(readlink -m "$CURRENT")
  [ -d "$target" ] || return 1
  major=$(jdk_verify_dir "$target" 2>/dev/null) || return 1
  printf '%s\n' "$major"
}

# 失败消息用：主版本号 / 未设置 / 损坏
jdk_current_major_desc() {
  local major
  major=$(jdk_current_major 2>/dev/null) || major=""
  if [ -n "$major" ]; then printf '%s\n' "$major"
  elif [ -L "$CURRENT" ]; then printf '损坏\n'
  else printf '未设置\n'; fi
}

jdk_resolve_dir() {  # 主版本 → 内置目录或卷内已安装目录
  local major="$1"
  if [ "$major" = "$BUILTIN_MAJOR" ]; then printf '%s\n' "$BUILTIN_DIR"
  else printf '%s\n' "$JDK_ROOT/$major"; fi
}

# 同卷临时链接 + rename 原子替换；失败保留原选择
jdk_switch_to() {
  local target="$1" tmp="$JDK_ROOT/.current.$$"
  rm -f "$tmp"
  if ! ln -s "$target" "$tmp"; then
    err "无法在 $JDK_ROOT 创建临时链接（状态卷不可写？）"
    return 1
  fi
  if ! mv -T "$tmp" "$CURRENT"; then
    rm -f "$tmp"
    err "原子替换 $CURRENT 失败"
    return 1
  fi
}

# ---------- 按需下载安装 ----------

# 安全解包 Debian 包的 data 成员到 $2：校验包类型、控制字段身份与成员路径安全。
# 不调用 dpkg-deb/ar 的原因：镜像内没有 ar，开发机（Arch）没有 dpkg-deb，而 python3 在镜像内必然存在
# （render-auth.py 依赖它）；单一实现让容器与开发机走同一条代码路径，并能显式实现本步要求的检查：
# ar 归档 + debian-binary 2.0（包类型）、control 的 Package/Architecture/Version（受限发行物）、
# 成员路径（绝对路径、..、设备/管道，以及经由包内链接写入、相对符号链接越界，一律拒绝）。
jdk_unpack_deb() {  # $1=包路径 $2=解包目标目录
  python3 - "$1" "$2" "$JDK8_PACKAGE" "$JDK8_ARCH" "$JDK8_VERSION" "$JDK8_DIR_IN_PKG" <<'PY'
import os
import sys
import tarfile

COMPRESS = {"xz": "r|xz", "gz": "r|gz", "bz2": "r|bz2"}


def die(msg):
    sys.stderr.write("jdk: 错误：" + msg + "\n")
    raise SystemExit(1)


def ar_members(path):
    """扫描 ar 归档，返回 {成员名: (数据偏移, 长度)}；非 Debian 包即失败。"""
    members = {}
    with open(path, "rb") as handle:
        if handle.read(8) != b"!<arch>\n":
            die("不是 Debian 包：缺少 ar 归档魔数")
        while True:
            header = handle.read(60)
            if not header:
                break
            if len(header) != 60 or header[58:60] != b"`\n":
                die("不是 Debian 包：成员头不完整")
            name = header[:16].decode("ascii", "replace").rstrip(" ")
            if name.endswith("/"):
                name = name[:-1]
            if not name or name.startswith("#1/"):
                die("不是 Debian 包：成员名不合法：%s" % name)
            size = header[48:58].decode("ascii", "replace").strip()
            if not size.isdigit():
                die("不是 Debian 包：成员 %s 的长度字段不合法" % name)
            members[name] = (handle.tell(), int(size))
            handle.seek(int(size) + int(size) % 2, os.SEEK_CUR)
    return members


def member_bytes(path, members, name):
    offset, size = members[name]
    with open(path, "rb") as handle:
        handle.seek(offset)
        return handle.read(size)


def open_member_tar(path, members, base):
    """按成员后缀选定压缩格式并流式打开（流式模式只顺序读取，不受成员偏移影响）。"""
    for ext, mode in COMPRESS.items():
        name = "%s.%s" % (base, ext)
        if name in members:
            handle = open(path, "rb")
            handle.seek(members[name][0])
            return tarfile.open(fileobj=handle, mode=mode), handle
    die("不是 Debian 包：缺少 %s（支持 .xz/.gz/.bz2）" % base)


def read_control(path, members):
    tar, handle = open_member_tar(path, members, "control.tar")
    try:
        for member in tar:
            if os.path.normpath(member.name) == "control":
                return tar.extractfile(member).read().decode("utf-8", "replace")
        die("不是 Debian 包：control 成员内缺少 control 文件")
    finally:
        tar.close()
        handle.close()


def control_field(text, key):
    for line in text.splitlines():
        if line.startswith(key + ":"):
            return line.split(":", 1)[1].strip()
    return None


def extract_data(path, members, dest, want_dir):
    """单趟流式解包：逐成员校验路径安全后立即解包（不预读成员表，流式流不做回退 seek）。"""
    tar, handle = open_member_tar(path, members, "data.tar")
    seen = []
    links = set()
    try:
        for member in tar:
            name = member.name
            normal = os.path.normpath(name)
            if name.startswith("/") or normal.startswith("/"):
                die("包内成员使用绝对路径：%s（解包逃逸）" % name)
            if normal == ".." or normal.startswith("../"):
                die("包内成员越过上级目录：%s（解包逃逸）" % name)
            if member.isdev() or member.isfifo():
                die("包内成员 %s 是设备或管道等特殊文件，拒绝解包" % name)
            parts = normal.split("/")
            for index in range(1, len(parts)):
                if "/".join(parts[:index]) in links:
                    die("包内成员 %s 经由链接 %s 写入（解包逃逸）" % (name, "/".join(parts[:index])))
            if member.islnk():
                target = os.path.normpath(member.linkname)
                if member.linkname.startswith("/") or target == ".." or target.startswith("../"):
                    die("包内硬链接 %s 指向包外：%s" % (name, member.linkname))
            if member.issym() and not member.linkname.startswith("/"):
                target = os.path.normpath(os.path.join(os.path.dirname(normal), member.linkname))
                if target == ".." or target.startswith("../"):
                    die("包内符号链接 %s 指向解包目录外：%s" % (name, member.linkname))
            # 路径安全已由本函数逐成员完成，故显式 fully_trusted：tarfile 自 3.12 起按过滤器解包，
            # 其默认 data 过滤器会拒绝官方包内合法的系统证书符号链接（cacerts → /etc/ssl/...）。
            tar.extract(member, dest, filter="fully_trusted")
            if member.issym() or member.islnk():
                for previous in seen:
                    if previous == normal or previous.startswith(normal + "/"):
                        die("包内链接 %s 覆盖已解包路径 %s（解包逃逸）" % (name, previous))
                links.add(normal)
            seen.append(normal)
    finally:
        tar.close()
        handle.close()
    if not os.path.isdir(os.path.join(dest, want_dir)):
        die("包内缺少预期 JDK 目录：%s" % want_dir)


def main():
    if len(sys.argv) != 7:
        die("用法：<包> <解包目录> <包名> <架构> <版本> <包内 JDK 目录>")
    pkg, dest, want_package, want_arch, want_version, want_dir = sys.argv[1:7]
    members = ar_members(pkg)
    if "debian-binary" not in members:
        die("不是 Debian 包：缺少 debian-binary 成员")
    if member_bytes(pkg, members, "debian-binary") != b"2.0\n":
        die("不是 Debian 包：debian-binary 版本不是 2.0")
    control = read_control(pkg, members)
    for key, want in (("Package", want_package), ("Architecture", want_arch), ("Version", want_version)):
        found = control_field(control, key)
        if found != want:
            die("包身份不符：%s 期望 %s，实际 %s" % (key, want, found))
    extract_data(pkg, members, dest, want_dir)


main()
PY
}

# 按需安装清单内主版本：下载 → 独立哈希校验 → 安全解包 → 真实 java/javac 版本检查 → 同卷原子发布 → 原子切换
jdk_install() {  # $1=清单内主版本
  local major="$1" dir version url sha pkg_name want_arch want_dir tmp pkg found
  dir=$(jdk_resolve_dir "$major")

  case "$major" in
    8)
      version="$JDK8_VERSION"; pkg_name="$JDK8_PACKAGE"
      want_arch="$JDK8_ARCH"; want_dir="$JDK8_DIR_IN_PKG"
      url="$JDK8_URL"; sha="$JDK8_SHA256"
      if [ -n "${CADENCE_JDK8_URL:-}" ] || [ -n "${CADENCE_JDK8_SHA256:-}" ]; then
        if [ -z "${CADENCE_JDK8_URL:-}" ] || [ -z "${CADENCE_JDK8_SHA256:-}" ]; then
          err "CADENCE_JDK8_URL 与 CADENCE_JDK8_SHA256 必须成对配置（来源与其独立校验值）；当前选择未改变（$(jdk_current_major_desc)）"
          return 1
        fi
        url="$CADENCE_JDK8_URL"; sha="$CADENCE_JDK8_SHA256"
        log "使用显式配置的来源与校验值（自建镜像/离线导入）"
      fi
      ;;
    *)
      err "不支持的主版本 $major（当前可用：${BUILTIN_MAJOR}）；当前选择未改变（$(jdk_current_major_desc)）"
      return 3 ;;
  esac

  if [ "$(jdk_arch)" != "$want_arch" ]; then
    err "暂不在 $(jdk_arch) 架构上安装 JDK $major（清单仅提供 $want_arch）；当前选择未改变（$(jdk_current_major_desc)）"
    return 1
  fi
  if [ ! -d "$JDK_ROOT" ]; then
    err "JDK 状态目录 $JDK_ROOT 不存在（状态卷未挂载？）；当前选择未改变（$(jdk_current_major_desc)）"
    return 1
  fi
  if [ ! -w "$JDK_ROOT" ]; then
    err "JDK 状态目录 $JDK_ROOT 不可写（卷属主不是当前用户？）；当前选择未改变（$(jdk_current_major_desc)）"
    return 1
  fi

  jdk_prune_stale_installs
  tmp=$(mktemp -d "$JDK_ROOT/.install.XXXXXX") || {
    err "无法在 $JDK_ROOT 创建安装临时目录；当前选择未改变（$(jdk_current_major_desc)）"
    return 1
  }
  JDK_INSTALL_TMP="$tmp"
  pkg="$tmp/package.deb"

  log "下载 JDK $major（${pkg_name} ${version} ${want_arch}）← $url"
  log "期望 SHA-256 $sha"
  if ! curl -fsSL --proto-redir '=https' --retry 2 -o "$pkg" "$url"; then
    err "下载失败：$url；未安装，当前选择未改变（$(jdk_current_major_desc)）"
    return 1
  fi

  found=$(sha256sum "$pkg" | cut -d' ' -f1) || found=""
  if [ -z "$found" ] || [ "$found" != "$sha" ]; then
    err "SHA-256 校验失败：期望 $sha，实际 ${found:-（无法计算）}（来源 $url）；未安装，当前选择未改变（$(jdk_current_major_desc)）"
    return 1
  fi

  jdk_unpack_deb "$pkg" "$tmp/root" || {
    err "解包 $pkg 失败；未安装，当前选择未改变（$(jdk_current_major_desc)）"
    return 1
  }

  found=$(jdk_verify_dir "$tmp/root/$want_dir") || {
    err "包内 $want_dir 不是可用完整 JDK（见上）；未安装，当前选择未改变（$(jdk_current_major_desc)）"
    return 1
  }
  if [ "$found" != "$major" ]; then
    err "包内 JDK 实际主版本 $found 与请求的 $major 不一致；未发布，当前选择未改变（$(jdk_current_major_desc)）"
    return 1
  fi

  if [ -e "$dir" ]; then
    err "目标目录 $dir 已存在（并发安装？）；未覆盖，当前选择未改变（$(jdk_current_major_desc)）"
    return 1
  fi
  if ! mv -T "$tmp/root/$want_dir" "$dir"; then
    err "原子发布 $dir 失败；当前选择未改变（$(jdk_current_major_desc)）"
    return 1
  fi
  jdk_install_cleanup

  jdk_switch_to "$dir" || return 1
  log "已安装并切换 JDK $major → $dir"
}

# ---------- 命令 ----------

jdk_init() {
  local target major old_major
  [ -d "$JDK_ROOT" ] || { err "JDK 状态目录 $JDK_ROOT 不存在（状态卷未挂载？）"; return 1; }
  [ -w "$JDK_ROOT" ] || { err "JDK 状态目录 $JDK_ROOT 不可写（卷属主不是当前用户？）"; return 1; }

  if [ ! -L "$CURRENT" ]; then
    if [ -e "$CURRENT" ]; then
      err "JDK 状态损坏：$CURRENT 不是链接（预期指向所选 JDK）；未自动重置，请人工处理后重试"
      return 1
    fi
    log "未发现已有 JDK 选择——初始化为镜像内置 ${BUILTIN_MAJOR}（$BUILTIN_DIR）"
    jdk_use "$BUILTIN_MAJOR"
    return $?
  fi

  target=$(readlink -m "$CURRENT")
  major=$(jdk_verify_dir "$target") || major=""
  if [ -n "$major" ]; then
    log "已有 JDK 选择有效：主版本 ${major}（$target）——保留"
    return 0
  fi

  # 目标失效：镜像升级换了内置路径时迁移指向；其余视为损坏，明确报错且不重置
  old_major=$(printf '%s\n' "$target" | sed -n 's#.*/temurin-\([0-9][0-9]*\)-jdk.*#\1#p')
  if [ -n "$old_major" ] && [ "$old_major" = "$BUILTIN_MAJOR" ] && [ -d "$BUILTIN_DIR" ]; then
    log "内置 JDK 路径随镜像升级变化（$target 不存在）——迁移指向 $BUILTIN_DIR"
    jdk_switch_to "$BUILTIN_DIR"
    return $?
  fi

  err "JDK 状态损坏：$CURRENT → $target 不可用；未自动重置。用 jdk list 查看，jdk use ${BUILTIN_MAJOR} 修复"
  return 1
}

jdk_use() {
  local major="${1:-}" dir installed_major
  case "$major" in
    ''|*[!0-9]*) err "用法：jdk use <主版本>（当前可用：${BUILTIN_MAJOR}）"; return 2 ;;
  esac

  dir=$(jdk_resolve_dir "$major")
  if [ -e "$dir" ] && [ ! -d "$dir" ]; then
    err "$dir 存在但不是目录；当前选择未改变（$(jdk_current_major_desc)）"
    return 1
  fi
  if [ -d "$dir" ]; then
    installed_major=$(jdk_verify_dir "$dir") || {
      err "已存在的 $dir 不是可用 JDK；未重复下载也未覆盖——确认不需要后删除该目录再重试。当前选择未改变（$(jdk_current_major_desc)）"
      return 1
    }
    if [ "$installed_major" != "$major" ]; then
      err "$dir 实际主版本 $installed_major 与请求的 $major 不一致；当前选择未改变（$(jdk_current_major_desc)）"
      return 1
    fi
    jdk_switch_to "$dir" || return 1
    log "已切换 JDK $major → $dir"
    return 0
  fi

  if [ "$major" = "$BUILTIN_MAJOR" ]; then
    err "镜像内置 JDK ${BUILTIN_MAJOR} 目录不存在：$BUILTIN_DIR（镜像与 CADENCE_BUILTIN_JDK_DIR 不一致？）；当前选择未改变（$(jdk_current_major_desc)）"
    return 1
  fi
  if jdk_supported_major "$major"; then
    jdk_install "$major" || return $?
    return 0
  fi
  err "不支持的主版本 $major（当前可用：${BUILTIN_MAJOR}）；当前选择未改变（$(jdk_current_major_desc)）"
  return 3
}

# 内置版本 + 卷内数字命名的已安装版本
jdk_known_majors() {
  local path
  printf '%s\n' "$BUILTIN_MAJOR"
  if [ -d "$JDK_ROOT" ]; then
    for path in "$JDK_ROOT"/*; do
      [ -d "$path" ] || continue
      case "${path##*/}" in *[!0-9]*) continue ;; esac
      printf '%s\n' "${path##*/}"
    done
  fi
}

jdk_list() {
  local target major mark source dir
  major=$(jdk_current_major 2>/dev/null) || major=""
  if [ -n "$major" ]; then
    printf '当前选择：%s\n' "$major"
  elif [ -L "$CURRENT" ]; then
    printf '当前选择：损坏（%s → %s 不可用）\n' "$CURRENT" "$(readlink -m "$CURRENT")"
  else
    printf '当前选择：未设置（下次启动初始化为内置 %s）\n' "$BUILTIN_MAJOR"
  fi

  target=$(jdk_current_target)
  for major in $(jdk_known_majors | sort -n -u); do
    dir=$(jdk_resolve_dir "$major")
    if [ "$major" = "$BUILTIN_MAJOR" ]; then source="内置"; else source="已安装"; fi
    if [ -n "$target" ] && [ "$dir" = "$target" ]; then mark="*"; else mark="-"; fi
    printf '%s %s  %s  %s\n' "$mark" "$major" "$source" "$dir"
  done
}

main() {
  case "${1:-}" in
    list) jdk_list ;;
    use)
      shift
      [ $# -ge 1 ] || { err "缺少参数：jdk use <主版本>"; return 2; }
      [ $# -eq 1 ] || { err "用法：jdk use <主版本>（不接受额外参数：${*:2}）"; return 2; }
      jdk_use "$1" ;;
    init)
      shift
      jdk_init ;;
    -h|--help|help) usage ;;
    '') usage; return 2 ;;
    *) err "未知子命令：$1"; usage; return 2 ;;
  esac
}

main "$@"
