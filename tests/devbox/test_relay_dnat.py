# -*- coding: utf-8 -*-
"""devbox/relay-vm.sh 的 VM 侧 DNAT（nft）契约与行为校验。

覆盖：read_dnat_env 校验、dnat-apply 规则生成（目标取 relay 服务表
bindaddr/vmPort、LF 行尾、幂等、部分缺失跳过、三方比对告警）、dnat-flush
边界、setup 的单元/enable 接线（附加能力失败不回滚）、teardown 清理、boot
兜底循环重放、status 的 DNAT 段。

沙箱思路与 test_relay.py 的 VmSandbox 一致：自建 bin 桩（nft/ip/ss/
systemctl/dnf）+ 沙箱路径环境变量（RELAY_*），真实执行 relay-vm.sh。
"""
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RELAY_VM = REPO / "devbox" / "relay-vm.sh"

BIND = "10.89.0.1"  # relay 服务表 bindaddr（= socat 实际监听地址）
SERVICES = {"mysql": ("23306", "13306"), "redis": ("26379", "16379")}
MAP_OK = (
    "# 由 relay.ps1 生成，勿手改。\n"
    "# <name> <real_ip> <real_port> <target_ip> <target_port> [domain]\n"
    "mysql 10.129.12.75 3306 10.89.0.1 23306 mysql.corp.internal\n"
    "redis 10.129.20.31 6379 10.89.0.1 26379 -\n"
)


# ── bin 桩 ────────────────────────────────────────────────────────────────
FAKE_SS = r'''#!/bin/sh
# 测试桩 ss：扫 /proc，把 cmdline 带 TCP-LISTEN: 参数的进程报成监听行
for d in /proc/[0-9]*; do
  pid=${d#/proc/}
  args=$(tr '\0' '\n' < "$d/cmdline" 2>/dev/null) || continue
  l=$(printf '%s\n' "$args" | grep '^TCP-LISTEN:' | head -1)
  [ -n "$l" ] || continue
  port=${l#TCP-LISTEN:}; port=${port%%,*}
  bind=
  for seg in $(printf '%s' "$l" | tr ',' ' '); do
    case $seg in bind=*) bind=${seg#bind=} ;; esac
  done
  echo "LISTEN 0 128 $bind:$port 0.0.0.0:* users:((\"socat\",pid=$pid,fd=4))"
done
exit 0
'''

FAKE_IP = r'''#!/bin/sh
# 测试桩 ip：报告沙箱网卡地址（FAKE_BINDADDR；FAKE_BINDADDRS 可给空格分隔多址）
for a in ${FAKE_BINDADDRS:-${FAKE_BINDADDR:-127.0.0.1}}; do
  echo "2: fake0    inet $a/24 brd 10.89.255.255 scope global fake0"
done
exit 0
'''

FAKE_NFT = r'''#!/bin/sh
# 测试桩 nft：记录调用；-f 时快照规则文件并记作“已生效表”；delete 只接受
# 自有表 cadence_dnat（其他表名直接失败，用于验证 flush 边界）；list 回放表。
# NFT_FAIL=1 → -f 失败（验证应用失败路径与临时文件清理）；NFT_ABSENT=1 → 命令不存在。
LOG=${NFT_LOG:?}; ST=${NFT_STATE_DIR:?}
printf '%s\n' "$*" >>"$LOG"
case "$1" in
  -f)
    if [ -n "${NFT_FAIL:-}" ]; then
      echo "桩错误：模拟 nft -f 失败" >&2
      exit 1
    fi
    n=$(cat "$ST/seq" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" >"$ST/seq"
    cp "$2" "$ST/apply-$n.nft"
    cp "$2" "$ST/current.nft"
    exit 0 ;;
  delete)
    if [ "$2" = table ] && [ "$3" = ip ] && [ "$4" = cadence_dnat ]; then
      printf '%s\n' "$*" >>"$ST/deletes"
      rm -f "$ST/current.nft"
      exit 0
    fi
    printf '桩错误：只允许删除自有表，收到：%s\n' "$*" >&2
    exit 1 ;;
  list)
    if [ "$2" = table ] && [ "$3" = ip ] && [ "$4" = cadence_dnat ] \
       && [ -f "$ST/current.nft" ]; then
      cat "$ST/current.nft"
      exit 0
    fi
    echo "No such file or directory" >&2
    exit 1 ;;
  *) exit 0 ;;
esac
'''

FAKE_SYSTEMCTL = r'''#!/bin/sh
# 测试桩 systemctl：调用全记录；cadence-relay@X 的 start/restart 真实 nohup
# run 脚本、disable/stop 真实 kill（MainPID 语义与 systemd 一致）；
# cadence-relay-dnat 只维护 wants 链接（oneshot，ExecStart 不在此重放）。
LOG=${SYSTEMCTL_LOG:?}; ST=${FAKE_STATE_DIR:?}
UNIT_DIR=$(dirname "${RELAY_UNIT_TEMPLATE:?}")
WANTS="$UNIT_DIR/multi-user.target.wants"
echo "$*" >>"$LOG"
cmd=$1; shift
case $cmd in
  is-system-running)
    st=${FAKE_SYSTEMD_STATE:-running}; echo "$st"
    case $st in running|degraded) exit 0 ;; *) exit 1 ;; esac ;;
  daemon-reload) exit 0 ;;
  enable)
    [ -e "$ST/enable_fail" ] && exit 1
    for u in "$@"; do
      case $u in
        cadence-relay@*)
          n=${u#cadence-relay@}; n=${n%%.service*}
          mkdir -p "$WANTS"
          ln -sf "$RELAY_UNIT_TEMPLATE" "$WANTS/cadence-relay@$n.service" ;;
        cadence-relay-dnat | cadence-relay-dnat.service)
          mkdir -p "$WANTS"
          ln -sf "$UNIT_DIR/cadence-relay-dnat.service" \
            "$WANTS/cadence-relay-dnat.service" ;;
      esac
    done
    exit 0 ;;
  disable | stop)
    for u in "$@"; do
      case $u in
        cadence-relay@*)
          n=${u#cadence-relay@}; n=${n%%.service*}
          if [ -f "$ST/$n.mainpid" ]; then
            kill "$(cat "$ST/$n.mainpid")" 2>/dev/null
            rm -f "$ST/$n.mainpid"
          fi
          rm -f "$WANTS/cadence-relay@$n.service" ;;
        cadence-relay-dnat | cadence-relay-dnat.service)
          rm -f "$WANTS/cadence-relay-dnat.service" ;;
      esac
    done
    exit 0 ;;
  restart | start)
    [ -e "$ST/restart_fail" ] && exit 1
    for u in "$@"; do
      case $u in
        cadence-relay@*)
          n=${u#cadence-relay@}; n=${n%%.service*}
          if [ -f "$ST/$n.mainpid" ]; then
            kill "$(cat "$ST/$n.mainpid")" 2>/dev/null; sleep 0.2
          fi
          nohup "$RELAY_RUN_SCRIPT" "$n" >>"$RELAY_LOG_FILE" 2>&1 &
          echo $! >"$ST/$n.mainpid" ;;
      esac
    done
    exit 0 ;;
  is-active)
    for u in "$@"; do
      case $u in
        cadence-relay@*)
          n=${u#cadence-relay@}; n=${n%%.service*}
          if [ -f "$ST/$n.mainpid" ] && kill -0 "$(cat "$ST/$n.mainpid")" 2>/dev/null; then
            echo active; exit 0
          fi ;;
      esac
    done
    echo inactive; exit 3 ;;
  is-enabled)
    for u in "$@"; do
      case $u in
        cadence-relay@*)
          n=${u#cadence-relay@}; n=${n%%.service*}
          if [ -e "$WANTS/cadence-relay@$n.service" ]; then
            echo enabled; exit 0
          fi ;;
        cadence-relay-dnat | cadence-relay-dnat.service)
          if [ -e "$WANTS/cadence-relay-dnat.service" ]; then
            echo enabled; exit 0
          fi ;;
      esac
    done
    echo disabled; exit 1 ;;
  show)
    u=''; prop=''; prev=''
    for a in "$@"; do
      case $a in cadence-relay@*) u=$a ;; esac
      [ "$prev" = "-p" ] && prop=$a
      prev=$a
    done
    n=${u#cadence-relay@}; n=${n%%.service*}
    if [ "$prop" = MainPID ] && [ -f "$ST/$n.mainpid" ]; then
      cat "$ST/$n.mainpid"
    else
      echo 0
    fi
    exit 0 ;;
  *) exit 0 ;;
esac
'''

FAKE_DNF = r'''#!/bin/sh
echo "dnf $*" >>"${DNF_LOG:?}"
exit 0
'''

_can_run_vm = (
    not sys.platform.startswith("win")
    and shutil.which("bash") is not None
    and Path("/proc").is_dir()
    and shutil.which("yes") is not None
)
needs_vm = pytest.mark.skipif(
    not _can_run_vm, reason="需要 POSIX bash、/proc 与 coreutils yes 执行 VM 侧行为验证"
)


class DnatSandbox:
    """POSIX 沙箱：bin 桩 + 沙箱路径环境变量，真实执行 devbox/relay-vm.sh。"""

    def __init__(self, root, *, systemd_state="running",
                 fake_bind=BIND, wsl_conf=None):
        self.root = root
        self.bin = root / "bin"
        self.unit_dir = root / "etc" / "systemd" / "system"
        self.run_script = root / "app" / "cadence-relay-run"
        self.boot_script = root / "app" / "cadence-relay-boot"
        self.env_file = root / "etc" / "cadence-relay.env"
        self.dnat_env = root / "etc" / "cadence-relay-dnat.env"
        self.dnat_unit = self.unit_dir / "cadence-relay-dnat.service"
        self.wants_dir = self.unit_dir / "multi-user.target.wants"
        self.log_file = root / "var" / "cadence-relay.log"
        self.wsl_conf = root / "etc" / "wsl.conf"
        self.state_dir = root / "fake-systemd-state"
        self.nft_state = root / "fake-nft-state"
        self.systemctl_log = root / "systemctl.log"
        self.nft_log = root / "nft.log"
        self.dnf_log = root / "dnf.log"
        for d in (self.bin, self.unit_dir, self.run_script.parent,
                  self.env_file.parent, self.log_file.parent, self.state_dir,
                  self.nft_state):
            d.mkdir(parents=True, exist_ok=True)
        for name, body in (("ss", FAKE_SS), ("systemctl", FAKE_SYSTEMCTL),
                           ("ip", FAKE_IP), ("dnf", FAKE_DNF),
                           ("nft", FAKE_NFT)):
            f = self.bin / name
            f.write_text(body, encoding="utf-8")
            f.chmod(0o755)
        socat_bin = self.bin / "socat"
        shutil.copy2(shutil.which("yes"), socat_bin)  # 真实 ELF：exe 核验可过
        socat_bin.chmod(0o755)
        if wsl_conf is not None:
            self.wsl_conf.write_text(wsl_conf, encoding="utf-8")
        self.env = dict(
            os.environ,
            PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            RELAY_ENV_FILE=str(self.env_file),
            RELAY_DNAT_ENV_FILE=str(self.dnat_env),
            RELAY_RUN_SCRIPT=str(self.run_script),
            RELAY_BOOT_SCRIPT=str(self.boot_script),
            RELAY_UNIT_TEMPLATE=str(self.unit_dir / "cadence-relay@.service"),
            RELAY_LOG_FILE="/dev/null",
            RELAY_WSL_CONF=str(self.wsl_conf),
            SYSTEMCTL_LOG=str(self.systemctl_log),
            FAKE_STATE_DIR=str(self.state_dir),
            FAKE_SYSTEMD_STATE=systemd_state,
            FAKE_BINDADDR=fake_bind,
            NFT_LOG=str(self.nft_log),
            NFT_STATE_DIR=str(self.nft_state),
            DNF_LOG=str(self.dnf_log),
        )

    # ── 输入 ──
    def write_services(self, services=None, bind=BIND):
        services = SERVICES if services is None else services
        self.env_file.write_text(
            "".join(f"{n} {vp} {bind} {hp}\n" for n, (vp, hp) in services.items()),
            encoding="utf-8")

    def write_map(self, text=MAP_OK):
        self.dnat_env.write_text(text, encoding="utf-8")

    # ── 执行与观测 ──
    def run(self, *args, timeout=120):
        return subprocess.run(["bash", str(RELAY_VM), *args], env=self.env,
                              capture_output=True, text=True, timeout=timeout)

    def nft_calls(self):
        if not self.nft_log.exists():
            return []
        return [l for l in self.nft_log.read_text(encoding="utf-8").splitlines() if l]

    def nft_deletes(self):
        f = self.nft_state / "deletes"
        if not f.exists():
            return []
        return [l for l in f.read_text(encoding="utf-8").splitlines() if l]

    def nft_captures(self):
        return [f.read_bytes() for f in sorted(self.nft_state.glob("apply-*.nft"))]

    def nft_current(self):
        f = self.nft_state / "current.nft"
        return f.read_text(encoding="utf-8") if f.exists() else None

    def calls(self):
        if not self.systemctl_log.exists():
            return []
        return [l for l in self.systemctl_log.read_text(encoding="utf-8").splitlines() if l]

    def supervisor_pids(self):
        """自有 boot 监督：exe 为 shell 解释器且完整 argv 恰为 [解释器, BOOT_SCRIPT]。"""
        pids = []
        marker = str(self.boot_script)
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                exe = os.readlink(f"/proc/{entry.name}/exe")
                args = (entry / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if Path(exe).name not in ("sh", "bash", "dash"):
                continue
            args = [a.decode("utf-8", "replace") for a in args if a]
            if len(args) == 2 and args[1] == marker:
                pids.append(int(entry.name))
        return pids

    def pids_on_port(self, vmport):
        pids = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                args = (entry / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            for a in args:
                a = a.decode("utf-8", "replace")
                if a.startswith("TCP-LISTEN:"):
                    if a[len("TCP-LISTEN:"):].split(",")[0] == str(vmport):
                        pids.append(int(entry.name))
                    break
        return pids

    def cleanup(self):
        marker = str(self.bin / "socat").encode()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmd = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if marker in cmd:
                try:
                    os.kill(int(entry.name), signal.SIGTERM)
                except OSError:
                    pass
        for pid in self.supervisor_pids():
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        for f in self.state_dir.glob("*.mainpid"):
            try:
                os.kill(int(f.read_text().strip()), signal.SIGTERM)
            except (OSError, ValueError):
                pass


@pytest.fixture
def vm_factory(tmp_path):
    sandboxes = []
    seq = [0]

    def make(**kw):
        seq[0] += 1
        s = DnatSandbox(tmp_path / f"vm{seq[0]}", **kw)
        sandboxes.append(s)
        return s

    yield make
    for s in sandboxes:
        s.cleanup()


def _rules_text(capture):
    return capture.decode("utf-8")


# ── 源码契约 ──────────────────────────────────────────────────────────────
def test_relay_vm_sh_syntax():
    result = subprocess.run(["bash", "-n", str(RELAY_VM)],
                            capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n 失败：{result.stderr}"


def test_relay_vm_sh_dnat_subcommands():
    text = RELAY_VM.read_text(encoding="utf-8")
    for sub in ("dnat-apply)", "dnat-flush)"):
        assert sub in text, f"relay-vm.sh 缺子命令分支 {sub}"
    header = text.split("set -u", 1)[0]
    assert "dnat-apply" in header and "dnat-flush" in header, \
        "文件头用法注释须同步新子命令"


def test_relay_vm_sh_uses_nft_file_not_cmdline_chain():
    """规则必须写文件再 nft -f；命令行 add chain 的含空格规格会被引号吃掉。"""
    text = RELAY_VM.read_text(encoding="utf-8")
    assert "nft -f" in text
    assert "nft add" not in text, "不得用命令行 nft add（空格/分号会被 shell 吃掉）"


# ── dnat-apply 规则生成 ───────────────────────────────────────────────────
@needs_vm
def test_dnat_apply_writes_rules_from_service_table(vm_factory):
    """规则：目标取服务表 bindaddr/vmPort，覆盖全部映射，纯 LF。"""
    vm = vm_factory()
    vm.write_services()
    vm.write_map()
    r = vm.run("dnat-apply")
    assert r.returncode == 0, r.stdout + r.stderr
    caps = vm.nft_captures()
    assert len(caps) == 1, "应只写一次规则文件"
    text = _rules_text(caps[0])
    assert "table ip cadence_dnat {" in text
    assert "type nat hook prerouting priority -101; policy accept;" in text
    assert "ip daddr 10.129.12.75 tcp dport 3306 dnat to 10.89.0.1:23306" in text
    assert "ip daddr 10.129.20.31 tcp dport 6379 dnat to 10.89.0.1:26379" in text
    assert text.count("dnat to") == 2, "每条映射一行规则"
    assert "\r" not in text, "规则文件必须无 CR（nft 拒绝 CRLF）"
    assert text.endswith("\n")
    assert b"\xef\xbb\xbf" not in caps[0], "规则文件不得带 BOM"
    calls = vm.nft_calls()
    assert calls[0] == "list table ip cadence_dnat", "先查现有表（幂等短路判定）"
    assert calls[1] == "delete table ip cadence_dnat", "再删残留表"
    assert calls[2].startswith("-f "), "最后整表 nft -f 应用"


@needs_vm
def test_dnat_apply_idempotent(vm_factory):
    """连跑两次：规则内容一致；第二次判定为已是最新，不重建表。

    "删表 + 重建"之间存在表不存在的窗口，boot 兜底循环每 5 秒调一次本函数，
    落在窗口内的新连接会绕过改写——因此规则未变时 MUST NOT 删表。
    """
    vm = vm_factory()
    vm.write_services()
    vm.write_map()
    assert vm.run("dnat-apply").returncode == 0
    first = vm.nft_current()
    r = vm.run("dnat-apply")
    assert r.returncode == 0, r.stdout + r.stderr
    caps = vm.nft_captures()
    assert len(caps) == 1, "规则未变时不得重建表"
    assert vm.nft_current() == first
    assert "未改动表" in r.stdout, r.stdout
    assert len(vm.nft_deletes()) == 1, "只在首次建表时删一次"


@needs_vm
def test_dnat_apply_skips_unknown_service_but_writes_rest(vm_factory):
    """服务表缺该服务名：该条不写规则并报错，其余照写，退出码仍 0。"""
    vm = vm_factory()
    vm.write_services()
    vm.write_map(MAP_OK + "oracle 10.129.40.11 1521 10.89.0.1 21521 -\n")
    r = vm.run("dnat-apply")
    assert r.returncode == 0, "部分缺失属配置期错误，不靠重试自愈"
    out = r.stdout + r.stderr
    assert "oracle" in out and "跳过" in out
    text = _rules_text(vm.nft_captures()[-1])
    assert "dnat to" in text
    assert "10.129.40.11" not in text, "缺服务名的条目不得写规则"
    assert "10.129.12.75" in text and "10.129.20.31" in text


@needs_vm
def test_dnat_apply_all_unknown_exits_1(vm_factory):
    """全部条目都缺：不生成任何规则，exit 1。"""
    vm = vm_factory()
    vm.write_services()
    vm.write_map("ora1 10.129.40.11 1521 10.89.0.1 21521 -\n"
                 "ora2 10.129.40.12 1521 10.89.0.1 21522 -\n")
    r = vm.run("dnat-apply")
    assert r.returncode == 1, r.stdout + r.stderr
    assert vm.nft_captures() == []
    assert "全部" in r.stdout + r.stderr


@needs_vm
def test_dnat_apply_mapping_mismatch_uses_service_table(vm_factory):
    """映射表 target 与服务表不一致：告警不阻断，规则按服务表写。"""
    vm = vm_factory()
    vm.write_services()
    vm.write_map("mysql 10.129.12.75 3306 10.89.0.99 9999 -\n"
                 "redis 10.129.20.31 6379 10.89.0.99 9999 -\n")
    r = vm.run("dnat-apply")
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "映射表与 relay 服务表不一致" in out
    assert "重跑 relay.ps1" in out
    text = _rules_text(vm.nft_captures()[-1])
    assert "dnat to 10.89.0.1:23306" in text
    assert "dnat to 10.89.0.1:26379" in text
    assert "10.89.0.99" not in text, "绝不把映射表 target 当死值写进规则"


@needs_vm
def test_dnat_apply_bindaddr_off_nic_warns_but_writes(vm_factory):
    """bindaddr 不在网卡（网桥重建/漂移）：告警，仍按 bindaddr 写规则。"""
    vm = vm_factory(fake_bind="192.0.2.77")
    vm.write_services()
    vm.write_map()
    r = vm.run("dnat-apply")
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "网桥已重建/网关漂移" in out
    assert "重跑 relay.ps1" in out
    text = _rules_text(vm.nft_captures()[-1])
    assert "dnat to 10.89.0.1:23306" in text


@needs_vm
def test_dnat_apply_without_map_file_exits_1(vm_factory):
    """映射表缺失：exit 1 且提示重跑 relay.ps1（供 status 判定）。"""
    vm = vm_factory()
    vm.write_services()
    r = vm.run("dnat-apply")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "重跑 relay.ps1" in r.stdout + r.stderr
    assert vm.nft_captures() == []


@needs_vm
@pytest.mark.parametrize("row, needle", [
    # 4 段：误把 relay 服务表喂进来
    ("mysql 23306 10.89.0.1 13306\n", "4 段"),
    ("mysql 10.129.12 3306 10.89.0.1 23306\n", "IPv4"),
    ("mysql 10.129.12.75 0 10.89.0.1 23306\n", "端口"),
    ("-", None),
])
def test_dnat_apply_rejects_invalid_rows(vm_factory, row, needle):
    vm = vm_factory()
    vm.write_services()
    if row == "-":
        vm.write_map("mysql 10.129.12.75 3306 10.89.0.1 70000\n")
        needle = "端口"
    else:
        vm.write_map(row)
    r = vm.run("dnat-apply")
    assert r.returncode == 1, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "映射表" in out or "格式错误" in out
    assert needle in out, f"错误信息须点明原因，实际：{out}"
    assert vm.nft_captures() == []


@needs_vm
def test_dnat_apply_rejects_duplicate_real_port(vm_factory):
    """(real_ip, real_port) 唯一：同一真实地址端口不能有两个目标。"""
    vm = vm_factory()
    vm.write_services()
    vm.write_map("mysql 10.129.12.75 3306 10.89.0.1 23306 -\n"
                 "dup 10.129.12.75 3306 10.89.0.1 23307 -\n")
    r = vm.run("dnat-apply")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "唯一" in r.stdout + r.stderr or "重复" in r.stdout + r.stderr


@needs_vm
def test_dnat_apply_rejects_ip_literal_domain(vm_factory):
    """第 6 段写 IP 字面量：域名段非法（提示改用 real_ip 段）。"""
    vm = vm_factory()
    vm.write_services()
    vm.write_map("mysql 10.129.12.75 3306 10.89.0.1 23306 10.1.2.3\n")
    r = vm.run("dnat-apply")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "域名" in r.stdout + r.stderr


# ── dnat-flush ────────────────────────────────────────────────────────────
@needs_vm
def test_dnat_flush_only_own_table(vm_factory):
    """flush 只删自有表、幂等（表不存在也返回 0），不碰其他表。"""
    vm = vm_factory()
    vm.write_services()
    vm.write_map()
    assert vm.run("dnat-apply").returncode == 0
    assert vm.nft_deletes() == ["delete table ip cadence_dnat"], "apply 先删残留表"
    r = vm.run("dnat-flush")
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(vm.nft_deletes()) == 2
    assert vm.nft_current() is None
    r = vm.run("dnat-flush")
    assert r.returncode == 0, "表已不存在时仍须 0（幂等）"
    assert set(vm.nft_deletes()) == {"delete table ip cadence_dnat"}, \
        "只应出现自有表名，不得出现其他表"
    assert len(vm.nft_deletes()) == 3


# ── setup / teardown 接线 ─────────────────────────────────────────────────
@needs_vm
def test_setup_writes_and_enables_dnat_unit(vm_factory):
    """setup：写 oneshot 单元（/bin/bash 前缀）、enable --now 并重放规则。"""
    vm = vm_factory()
    vm.write_services()
    vm.write_map()
    r = vm.run("setup")
    assert r.returncode == 0, r.stdout + r.stderr
    unit = vm.dnat_unit.read_text(encoding="utf-8")
    assert "Type=oneshot" in unit
    assert "RemainAfterExit=yes" in unit
    assert "Restart=on-failure" in unit
    assert "RestartSec=5" in unit
    assert "After=network.target" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "ExecStart=/bin/bash " in unit and "dnat-apply" in unit, \
        "cp 不保证可执行位，ExecStart 必须带 /bin/bash"
    assert "ExecStop=/bin/bash " in unit and "dnat-flush" in unit
    assert any("enable --now cadence-relay-dnat" in c for c in vm.calls()), \
        "⑥ 段须 enable --now 单元"
    assert vm.nft_captures(), "setup 末尾须重放规则"
    assert vm.nft_current() is not None


@needs_vm
def test_setup_dnat_failure_does_not_rollback(vm_factory):
    """映射表缺失：DNAT 失败只告警，relay 通道照旧就绪（不误伤已可用通道）。"""
    vm = vm_factory()
    vm.write_services()
    r = vm.run("setup")  # 无映射表
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DNAT" in r.stdout, "须 log 告警"
    assert vm.run_script.exists() and vm.dnat_unit.exists()
    for name, (vp, _hp) in SERVICES.items():
        assert vm.pids_on_port(vp), f"{name} 监听必须仍在（不回滚 relay）"


@needs_vm
def test_setup_rollback_removes_dnat_unit(vm_factory):
    """setup 失败回滚：本次新建的 dnat 单元须随 NEW_FILES 删除。"""
    vm = vm_factory()
    vm.write_services()
    vm.write_map()
    (vm.state_dir / "enable_fail").write_text("", encoding="utf-8")
    r = vm.run("setup")
    assert r.returncode == 1, r.stdout + r.stderr
    assert not vm.dnat_unit.exists(), "回滚须清理本次新建的 dnat 单元"
    assert not vm.run_script.exists()


@needs_vm
def test_setup_snapshot_restores_prior_dnat_unit(vm_factory):
    """已存在的 dnat 单元：快照后回滚还原内容，且不因回滚被 disable。"""
    vm = vm_factory()
    vm.write_services()
    vm.write_map()
    vm.unit_dir.mkdir(parents=True, exist_ok=True)
    vm.dnat_unit.write_text("[Unit]\nDescription=cadence relay DNAT rules (old)\n",
                            encoding="utf-8")
    vm.wants_dir.mkdir(parents=True, exist_ok=True)
    (vm.wants_dir / "cadence-relay-dnat.service").symlink_to(vm.dnat_unit)
    (vm.state_dir / "enable_fail").write_text("", encoding="utf-8")
    r = vm.run("setup")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "DNAT rules (old)" in vm.dnat_unit.read_text(encoding="utf-8"), \
        "快照须还原被覆盖的 dnat 单元"
    assert (vm.wants_dir / "cadence-relay-dnat.service").exists(), \
        "快照时已 enabled 的单元回滚后不得被 disable"


@needs_vm
def test_teardown_removes_dnat_artifacts(vm_factory):
    """teardown：先 disable 单元，再清单元文件/映射表/nft 规则。"""
    vm = vm_factory()
    vm.write_services()
    vm.write_map()
    assert vm.run("setup").returncode == 0
    other_unit = vm.unit_dir / "other-app.service"
    other_unit.write_text("[Unit]\nDescription=unrelated\n", encoding="utf-8")
    r = vm.run("teardown")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = " ".join(vm.calls())
    assert "disable --now cadence-relay-dnat" in calls
    assert not vm.dnat_unit.exists(), "单元文件须删除（否则下次开机被重放）"
    assert not vm.dnat_env.exists(), "映射表须删除"
    assert other_unit.exists(), "无关 unit 不得触碰"
    assert vm.nft_deletes()[-1] == "delete table ip cadence_dnat"
    assert vm.nft_current() is None


@needs_vm
def test_boot_supervisor_replays_dnat(vm_factory):
    """无 systemd：boot 监督每轮先幂等重放 DNAT（不新增第二条 [boot] command）。"""
    vm = vm_factory(systemd_state="offline", wsl_conf="")
    vm.write_services()
    vm.write_map()
    r = vm.run("setup")
    assert r.returncode == 0, r.stdout + r.stderr
    conf = vm.wsl_conf.read_text(encoding="utf-8")
    assert conf.count("[boot]") == 1 and "cadence-relay-boot" in conf
    boot = vm.boot_script.read_text(encoding="utf-8")
    assert "dnat-apply" in boot and "/bin/bash" in boot
    assert "RELAY_DNAT_ENV_FILE" in boot, "须把映射表路径传给子进程"
    before = len(vm.nft_calls())
    for _ in range(80):
        if len(vm.nft_calls()) > before:
            break
        time.sleep(0.1)
    assert len(vm.nft_calls()) > before, "监督循环须重放 DNAT 规则"


# ── status DNAT 段 ────────────────────────────────────────────────────────
@needs_vm
def test_status_reports_dnat_rules(vm_factory):
    vm = vm_factory()
    vm.write_services()
    vm.write_map()
    assert vm.run("dnat-apply").returncode == 0
    r = vm.run("status")
    out = r.stdout
    assert "DNAT" in out
    assert "10.129.12.75:3306" in out and "10.89.0.1:23306" in out
    assert "OK" in out


@needs_vm
def test_status_flags_missing_dnat_map(vm_factory):
    """映射表缺失：status 明确提示重跑 relay.ps1。"""
    vm = vm_factory()
    vm.write_services()
    r = vm.run("status")
    assert "DNAT" in r.stdout
    assert "重跑 relay.ps1" in r.stdout
    assert r.returncode == 1


@needs_vm
def test_status_flags_missing_dnat_rules(vm_factory):
    """映射表在、规则不在：status 报缺失并给出重放指引。

    断言必须精确到 DNAT 段本身——否则会被中继段的 "VM 监听 :23306 FAIL"
    满足（中继根本没起），DNAT 诊断即使完全写错也能过。
    """
    vm = vm_factory()
    vm.write_services()
    vm.write_map()
    r = vm.run("status")
    out = r.stdout
    assert "table ip cadence_dnat 不存在" in out, out
    assert "dnat-apply" in out, "须给出重放指引"
    assert r.returncode == 1


# ── 失败路径 ──────────────────────────────────────────────────────────────
@needs_vm
def test_dnat_apply_fails_when_nft_rejects_file(vm_factory):
    """nft -f 失败：dnat-apply 非零退出、报明确错误、临时文件不残留。"""
    vm = vm_factory()
    vm.write_services()
    vm.write_map()
    vm.env["NFT_FAIL"] = "1"
    r = vm.run("dnat-apply")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "nft -f 应用失败" in (r.stdout + r.stderr), r.stdout + r.stderr
    leftovers = list(Path(vm.env.get("TMPDIR", "/tmp")).glob("cadence-dnat.*"))
    assert not leftovers, f"临时规则文件须清理：{leftovers}"


@needs_vm
def test_dnat_apply_fails_when_nft_absent(vm_factory):
    """VM 内无 nft：明确报错并非零退出（不静默成功）。

    必须把 PATH 限制到"沙箱 bin + 少量 coreutils"，否则本机 /usr/bin/nft
    仍会被 command -v 找到，测不到"缺少 nft"这条分支。
    """
    vm = vm_factory()
    vm.write_services()
    vm.write_map()
    core = vm.root / "corebin"
    core.mkdir(exist_ok=True)
    for tool in ("awk", "cut", "grep", "mktemp", "rm", "cat", "sed", "tr",
                 "head", "tail", "sleep", "date", "mkdir", "ln", "chmod",
                 "dirname", "basename", "env", "printf", "id", "sh", "bash"):
        src = shutil.which(tool)
        if not src:
            continue
        dst = core / tool
        if not dst.exists():
            dst.symlink_to(src)
    vm.env["PATH"] = f"{vm.bin}{os.pathsep}{core}"
    (vm.bin / "nft").unlink()   # 桩移除：PATH 里已无任何 nft
    r = vm.run("dnat-apply")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "缺少 nft" in (r.stdout + r.stderr), r.stdout + r.stderr


@needs_vm
def test_dnat_apply_skips_rebuild_when_rules_unchanged(vm_factory):
    """规则已一致时 MUST NOT 删表重建——boot 循环每 5 秒调一次，
    "删表 + 重建"之间存在表不存在的窗口，落在窗口内的新连接会绕过改写。"""
    vm = vm_factory()
    vm.write_services()
    vm.write_map()
    assert vm.run("dnat-apply").returncode == 0
    deletes_after_first = len(vm.nft_deletes())
    assert vm.run("dnat-apply").returncode == 0
    assert len(vm.nft_deletes()) == deletes_after_first, "规则未变时不得再删表"
    assert "未改动表" in vm.run("dnat-apply").stdout
