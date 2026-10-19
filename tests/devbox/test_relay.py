# -*- coding: utf-8 -*-
"""relay 一键通道脚本的契约校验（relay.ps1 / relay-vm.sh / 文档集成）。

除源码结构契约外，用 pwsh 执行从 relay.ps1 AST 抽取的真实函数做行为回归
（守卫的平台/运行状态判定、原生错误显式传播、Die 退出码契约）。
"""
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
RELAY_PS1 = REPO / "devbox" / "relay.ps1"
RELAY_VM = REPO / "devbox" / "relay-vm.sh"
INSTALL_PS1 = REPO / "devbox" / "install.ps1"
README = REPO / "devbox" / "README.md"
COMPOSE = REPO / "devbox" / "compose.yaml"

RELAY_PORTS = {"mysql": (13306, 23306), "redis": (16379, 26379)}


def test_relay_scripts_exist():
    assert RELAY_PS1.is_file(), "缺少 devbox/relay.ps1"
    assert RELAY_VM.is_file(), "缺少 devbox/relay-vm.sh"


def test_relay_vm_sh_syntax():
    result = subprocess.run(
        ["bash", "-n", str(RELAY_VM)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"bash -n 失败：{result.stderr}"


def test_relay_vm_sh_subcommands():
    text = RELAY_VM.read_text(encoding="utf-8")
    for sub in ("setup)", "status)", "teardown)"):
        assert sub in text, f"relay-vm.sh 缺子命令分支 {sub}"


def test_relay_ps1_switches():
    text = RELAY_PS1.read_text(encoding="utf-8")
    for token in (
        "[switch]$Status",
        "[switch]$Teardown",
        "[string]$MySQL",
        "[string]$Redis",
    ):
        assert token in text, f"relay.ps1 缺参数 {token}"


def test_relay_ps1_guards():
    text = RELAY_PS1.read_text(encoding="utf-8")
    assert "RunAs" in text, "缺管理员自提升（RunAs）"
    assert "wsl" in text, "缺 WSL 后端守卫"
    assert "System.Net.Sockets.Socket" in text, "缺远端预检 Socket"
    assert "TcpClient" not in text, "远端预检残留 TcpClient 字面量（会触发终端管控静默终止）"


def test_relay_ports_no_overlap():
    ps1 = RELAY_PS1.read_text(encoding="utf-8")
    for name, (host_port, vm_port) in RELAY_PORTS.items():
        assert str(host_port) in ps1, f"{name} hostPort {host_port} 未在 relay.ps1 出现"
        assert str(vm_port) in ps1, f"{name} vmPort {vm_port} 未在 relay.ps1 出现"
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    published = set()
    for spec in doc["services"].values():
        for p in spec.get("ports", []):
            published.add(int(str(p).split(":")[1]))
    overlap = published & {p for pair in RELAY_PORTS.values() for p in pair}
    assert not overlap, f"relay 端口与 compose 发布端口重叠：{sorted(overlap)}"


def test_install_ps1_mentions_relay():
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "relay.ps1" in text, "install.ps1 完成提示未提及 relay.ps1"


def test_readme_has_relay_section():
    text = README.read_text(encoding="utf-8")
    assert "### 2.6" in text, "README 缺 §2.6"
    section = text.split("### 2.6", 1)[1].split("## 3.", 1)[0]
    assert "relay.ps1" in section, "§2.6 未包含 relay.ps1 使用说明"


def test_relay_env_crlf_tolerated(tmp_path):
    """CRLF 行尾 env（PS Set-Content 默认产物）必须正确解析——真机首跑必败回归。"""
    import os

    env = tmp_path / "cadence-relay.env"
    env.write_bytes(b"mysql 23306 127.0.0.1 13306\r\n")
    result = subprocess.run(
        ["bash", str(RELAY_VM), "status"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "RELAY_ENV_FILE": str(env)},
    )
    out = result.stdout + result.stderr
    assert "格式错误" not in out
    assert ":23306" in out, "vmport 解析失败"
    assert "127.0.0.1:13306" in out, "hostport 解析失败"


def test_relay_ps1_fix_pins():
    text = RELAY_PS1.read_text(encoding="utf-8")
    assert "-NoNewline" in text, "env 写入须避免 CRLF（-NoNewline 手工拼 LF）"
    assert "Undo-Portproxy" in text, "改动后失败路径须回滚 portproxy"
    assert "已保留 relay.json" in text, "teardown 失败须保留留痕可重试"
    assert "'-InstallDir'" in text, "UAC 自提升须转发 -InstallDir"


def test_install_ps1_gitignore_covers_relay_json():
    text = INSTALL_PS1.read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if "stack/data/" in l]
    assert lines and "relay.json" in lines[0], "安装器写出的 .gitignore 未含 relay.json"


# ---------------------------------------------------------------------------
# 2026-10-16 多服务通道改造：服务目录 / 端口推导（+10000/+20000）/ 删除回收。

CATALOG_REMOTE_PORTS = {
    "mysql": 3306, "redis": 6379, "kafka": 9092,
    "zookeeper": 2181, "rabbitmq": 5672,
}
SERVICE_CATALOG = tuple(CATALOG_REMOTE_PORTS)


def test_relay_ps1_service_catalog():
    """服务目录：mysql/redis/kafka/zookeeper/rabbitmq 全部在册，且有自定义入口。"""
    text = RELAY_PS1.read_text(encoding="utf-8")
    for name in SERVICE_CATALOG:
        assert f"{name} = " in text, f"服务目录缺目录项 {name}"
    assert "custom" in text, "缺自定义服务入口（custom）"


def test_relay_ps1_port_derivation_pins():
    """推导规则钉住（2026-10-16 裁决）：hostPort=远端+10000、vmPort=远端+20000。"""
    text = RELAY_PS1.read_text(encoding="utf-8")
    assert "Get-RelayPorts" in text, "缺端口推导函数 Get-RelayPorts"
    assert "+ 10000" in text and "+ 20000" in text, "推导字面量 +10000/+20000 缺失"
    assert "45535" in text, "缺远端端口上限校验（45535+20000=65535，不越界）"
    assert "46535" not in text, "旧错值 46535 须清除（+20000 后越 65535）"


def test_relay_stack_ports_guard_matches_compose():
    """推导撞栈守卫：Get-RelayPorts 内置 stack 发布端口清单须与 compose 一致。"""
    text = RELAY_PS1.read_text(encoding="utf-8")
    m = re.search(r"\$stack = @\(([^)]*)\)", text)
    assert m, "relay.ps1 缺内置 stack 发布端口清单 $stack"
    got = {int(x) for x in re.findall(r"\d+", m.group(1))}
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    published = set()
    for spec in doc["services"].values():
        for p in spec.get("ports", []):
            published.add(int(str(p).split(":")[1]))
    assert got == published, f"$stack {sorted(got)} 与 compose {sorted(published)} 不一致"


def test_catalog_derived_ports_vs_compose():
    """目录各服务按常见远端端口推导后与 compose 发布端口零重叠；唯一例外
    rabbitmq 5672（+10000=15672 撞本地管理台）由显式守卫拒绝（见行为测试）。"""
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    published = set()
    for spec in doc["services"].values():
        for p in spec.get("ports", []):
            published.add(int(str(p).split(":")[1]))
    for name, remote in CATALOG_REMOTE_PORTS.items():
        hit = {remote + 10000, remote + 20000} & published
        if name == "rabbitmq":
            assert hit == {15672}, "rabbitmq 撞点假设失效，守卫与文档须复核"
        else:
            assert not hit, f"{name} 推导端口与 compose 重叠：{sorted(hit)}"


def test_relay_ps1_stale_service_cleanup_pins():
    """删除服务：setup 须删被移除服务的 portproxy；VM 侧实例由 relay-vm.sh 回收。"""
    ps1 = RELAY_PS1.read_text(encoding="utf-8")
    setup = ps1.split("function Invoke-Setup", 1)[1].split("function Invoke-Status", 1)[0]
    assert "portproxy delete" in setup, "setup 缺被移除服务的 portproxy delete"
    assert "回收" in setup, "setup 须说明 VM 侧实例随 setup 回收"
    assert "newPorts -notcontains" in setup, \
        "陈旧判定须按旧 hostPort 不在新清单（覆盖改名/改端口残留），非按服务名"
    assert "--retire" in setup, "setup 须把旧服务表传 VM 侧对账回收 boot 监听"
    vm = RELAY_VM.read_text(encoding="utf-8")
    assert "recycle_stale_instances" in vm, "relay-vm.sh 缺自有实例回收逻辑"
    assert "retire_stale_listeners" in vm, "relay-vm.sh 缺 boot 兜底旧监听回收"
    assert "disable --now" in vm, "回收须 disable --now 停实例"


# ---------------------------------------------------------------------------
# relay.ps1 行为回归：AST 抽取真实函数后在桩环境（fake cmd / fake podman）中执行。
# 需要本机有 pwsh 且为 POSIX 平台（桩命令是 /bin/sh 脚本）；
# Windows 侧人工解析验证同构：Language.Parser::ParseFile 须 0 错误（见 PARSE_HARNESS）。
PWSH = shutil.which("pwsh")
_can_run_ps = PWSH is not None and not sys.platform.startswith("win")
needs_pwsh = pytest.mark.skipif(
    not _can_run_ps, reason="需要 Linux/macOS 上的 pwsh 执行 PowerShell 行为验证"
)

# 官方文档 podman-machine-list.1 / podman-machine-inspect.1 公布的 JSON 字段样例
LIST_JSON_WSL_RUNNING = (
    '[{"Name":"podman-machine-default","Default":true,'
    '"Created":"2021-12-27T10:36:14.373347492-05:00","Running":true,'
    '"LastUp":"2021-12-27T11:22:50.17333371-05:00","Stream":"default",'
    '"VMType":"wsl","CPUs":1,"Memory":"2147483648","DiskSize":"10737418240"}]'
)
LIST_JSON_HYPERV = '[{"Name":"podman-machine-default","Default":true,"Running":true,"VMType":"hyperv"}]'
LIST_JSON_STOPPED = '[{"Name":"podman-machine-default","Default":true,"Running":false,"VMType":"wsl"}]'
# 真实形态：podman 日志行前缀 WARN[0000]/LEVEL[0000] 含 [，随 stderr 合并后置于 JSON 前
LIST_WARN_PREFIX = "WARN[0000] found extra config: some file"
# 用户 2026-10-13 实测的原生报错：--format "{{.Provider}}" 在 InspectInfo 上不存在
PROVIDER_TEMPLATE_ERROR = (
    'Error: template: inspect:1:13: executing "inspect" at <.Provider>: '
    "can't evaluate field Provider in type machine.InspectInfo"
)

GUARD_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { Write-Host "PARSE_ERROR: $($errors[0].Message)"; exit 2 }
foreach ($n in @('Info','Warn','Die','RunLoud','ConvertFrom-JsonText','Resolve-Machine')) {
  $fs = $ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true)
  if (-not $fs) { Write-Host "MISSING_FUNC: $n"; exit 3 }
  Invoke-Expression $fs[0].Extent.Text
}
$mach = Resolve-Machine
Write-Host "machine=$mach"
exit 0
"""

DIE_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { Write-Host "PARSE_ERROR: $($errors[0].Message)"; exit 2 }
foreach ($n in @('Info','Warn','Die')) {
  $fs = $ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true)
  if (-not $fs) { Write-Host "MISSING_FUNC: $n"; exit 3 }
  Invoke-Expression $fs[0].Extent.Text
}
Die 'boom-die'
Write-Host 'UNREACHABLE'
exit 0
"""

PARSE_HARNESS = r"""
$tokens = $null; $errors = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { Write-Host "PARSE_ERROR: $($errors[0].Message)"; exit 2 }
Write-Host 'PARSE_OK'
exit 0
"""


DERIVE_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { Write-Host "PARSE_ERROR: $($errors[0].Message)"; exit 2 }
foreach ($n in @('Info','Warn','Die','Get-RelayPorts')) {
  $fs = $ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true)
  if (-not $fs) { Write-Host "MISSING_FUNC: $n"; exit 3 }
  Invoke-Expression $fs[0].Extent.Text
}
foreach ($p in @(3306, 6379, 9092, 2181, 45535)) {
  $r = Get-RelayPorts $p
  Write-Host "port $p host=$($r.hostPort) vm=$($r.vmPort)"
}
exit 0
"""

DERIVE_CLASH_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { Write-Host "PARSE_ERROR: $($errors[0].Message)"; exit 2 }
foreach ($n in @('Info','Warn','Die','Get-RelayPorts')) {
  $fs = $ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true)
  if (-not $fs) { Write-Host "MISSING_FUNC: $n"; exit 3 }
  Invoke-Expression $fs[0].Extent.Text
}
$null = Get-RelayPorts 5672
Write-Host 'UNREACHABLE'
exit 0
"""

DERIVE_RANGE_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { Write-Host "PARSE_ERROR: $($errors[0].Message)"; exit 2 }
foreach ($n in @('Info','Warn','Die','Get-RelayPorts')) {
  $fs = $ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true)
  if (-not $fs) { Write-Host "MISSING_FUNC: $n"; exit 3 }
  Invoke-Expression $fs[0].Extent.Text
}
$null = Get-RelayPorts 45536
Write-Host 'UNREACHABLE'
exit 0
"""

# 期望：远端端口 → (hostPort, vmPort)；mysql/redis 两行为存量兼容锚点（不得漂移）；
# 45535 为上界锚点（+20000 恰 65535，不得再降一档拒收）
DERIVED_EXPECT = {
    3306: (13306, 23306), 6379: (16379, 26379),
    9092: (19092, 29092), 2181: (12181, 22181),
    45535: (55535, 65535),
}


def _write_stubs(bin_dir):
    """cmd / podman 桩：模拟 Windows 侧 cmd /c 与 podman machine 子命令。"""
    (bin_dir / "cmd").write_text(
        "#!/bin/sh\n"
        '[ "$1" = "/c" ] && shift\n'
        "s=$(printf '%s' \"$*\" | sed 's|2>nul|2>/dev/null|g')\n"
        'exec sh -c "$s"\n'
    )
    (bin_dir / "podman").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "machine" ] && [ "$2" = "list" ]; then\n'
        '  [ -n "$FAKE_LIST_STDERR" ] && echo "$FAKE_LIST_STDERR" >&2\n'
        '  [ -n "$FAKE_LIST_FILE" ] && cat "$FAKE_LIST_FILE"\n'
        "  exit ${FAKE_LIST_EXIT:-0}\n"
        "fi\n"
        'if [ "$1" = "machine" ] && [ "$2" = "inspect" ]; then\n'
        '  [ -n "$FAKE_INSPECT_STDERR" ] && echo "$FAKE_INSPECT_STDERR" >&2\n'
        '  [ -n "$FAKE_INSPECT_FILE" ] && cat "$FAKE_INSPECT_FILE"\n'
        "  exit ${FAKE_INSPECT_EXIT:-0}\n"
        "fi\n"
        'echo "podman: unsupported: $*" >&2\n'
        "exit 1\n"
    )
    for f in bin_dir.iterdir():
        f.chmod(0o755)


def _run_harness(harness_text, tmp_path, *, env_extra=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_stubs(bin_dir)
    harness = tmp_path / "harness.ps1"
    harness.write_text(harness_text, encoding="utf-8")
    env = dict(
        os.environ,
        PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        FAKE_LIST_EXIT="0",
        FAKE_INSPECT_EXIT="0",
        FAKE_LIST_STDERR="",
        FAKE_INSPECT_STDERR="",
    )
    env.update(env_extra or {})
    return subprocess.run(
        [PWSH, "-NoProfile", "-File", str(harness), str(RELAY_PS1)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )


@needs_pwsh
def test_relay_ps1_parses_cleanly(tmp_path):
    """PS 解析验证：Language.Parser 对 relay.ps1 必须 0 错误。"""
    r = _run_harness(PARSE_HARNESS, tmp_path)
    assert r.returncode == 0 and "PARSE_OK" in r.stdout, r.stdout + r.stderr


@needs_pwsh
def test_guard_resolves_wsl_running_machine(tmp_path):
    """官方 machine list --format json（VMType=wsl, Running=true）应解析出 machine 名。"""
    fx = tmp_path / "list.json"
    fx.write_text(LIST_JSON_WSL_RUNNING, encoding="utf-8")
    r = _run_harness(GUARD_HARNESS, tmp_path, env_extra={"FAKE_LIST_FILE": str(fx)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "machine=podman-machine-default" in r.stdout


@needs_pwsh
def test_guard_rejects_hyperv_backend(tmp_path):
    """Hyper-V 后端必须被明确拒绝并指出当前后端（显式失败，非静默）。"""
    fx = tmp_path / "list.json"
    fx.write_text(LIST_JSON_HYPERV, encoding="utf-8")
    r = _run_harness(GUARD_HARNESS, tmp_path, env_extra={"FAKE_LIST_FILE": str(fx)})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "仅支持 WSL" in r.stdout and "hyperv" in r.stdout


@needs_pwsh
def test_guard_rejects_stopped_machine(tmp_path):
    """machine 存在且为 wsl 但未运行：必须报“未运行”并给出 start 指引。"""
    fx = tmp_path / "list.json"
    fx.write_text(LIST_JSON_STOPPED, encoding="utf-8")
    r = _run_harness(GUARD_HARNESS, tmp_path, env_extra={"FAKE_LIST_FILE": str(fx)})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "未运行" in r.stdout and "podman machine start" in r.stdout


@needs_pwsh
def test_guard_parses_json_behind_warning_prefix(tmp_path):
    """真实回归：WARN[0000] 前缀含 [ 抢首位时，仍须定位到真正 JSON 并解析成功。"""
    fx = tmp_path / "list.json"
    fx.write_text(LIST_JSON_WSL_RUNNING, encoding="utf-8")
    r = _run_harness(
        GUARD_HARNESS, tmp_path,
        env_extra={"FAKE_LIST_FILE": str(fx), "FAKE_LIST_STDERR": LIST_WARN_PREFIX},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "machine=podman-machine-default" in r.stdout


@needs_pwsh
def test_guard_reports_native_error_and_exit_code(tmp_path):
    """list 失败契约：退出码 1，输出同时含 podman 原始 stderr 文本与 exit=125。"""
    r = _run_harness(
        GUARD_HARNESS, tmp_path,
        env_extra={"FAKE_LIST_EXIT": "125", "FAKE_LIST_STDERR": PROVIDER_TEMPLATE_ERROR},
    )
    assert r.returncode == 1, r.stdout + r.stderr
    assert "machine.InspectInfo" in r.stdout, "原生错误文本被吞（回归到 2>nul 静默）"
    assert "exit 125" in r.stdout, "原始退出码未保留"


@needs_pwsh
def test_die_exits_nonzero_and_prints(tmp_path):
    """Die 契约：红字输出 + exit 1（任何失败路径不得静默 exit 0）。"""
    r = _run_harness(DIE_HARNESS, tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "boom-die" in r.stdout


@needs_pwsh
def test_relay_derives_ports_for_any_remote_port(tmp_path):
    """端口推导行为：任意远端端口 +10000/+20000；mysql/redis 存量值不变。"""
    r = _run_harness(DERIVE_HARNESS, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    for remote, (host_port, vm_port) in DERIVED_EXPECT.items():
        want = f"port {remote} host={host_port} vm={vm_port}"
        assert want in r.stdout, f"推导应得 {want}：\n{r.stdout}"


@needs_pwsh
def test_relay_derivation_rejects_stack_clash(tmp_path):
    """推导撞本地 stack（远端 5672→hostPort 15672 撞 rabbitmq 管理台）须显式拒绝。"""
    r = _run_harness(DERIVE_CLASH_HARNESS, tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "错误" in r.stdout and "15672" in r.stdout
    assert "UNREACHABLE" not in r.stdout

@needs_pwsh
def test_relay_derivation_rejects_out_of_range(tmp_path):
    """上界行为：45535 通过（vmPort 恰 65535），45536 显式拒绝（+20000 越界）。"""
    ok = _run_harness(DERIVE_HARNESS, tmp_path)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "port 45535 host=55535 vm=65535" in ok.stdout, ok.stdout
    bad = _run_harness(DERIVE_RANGE_HARNESS, tmp_path)
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert "错误" in bad.stdout and "45535" in bad.stdout
    assert "UNREACHABLE" not in bad.stdout

# ---------------------------------------------------------------------------
# relay-vm.sh 每服务 systemd 持久化：沙箱真实行为回归（2026-10-14 固化裁决）。
# 桩均为“真实执行”型：fake systemctl 对 cadence-relay@X 的 start/restart
# 真拉起沙箱 run 脚本（真实监听进程、真实 cmdline，MainPID 语义与 systemd 一致）；
# fake ss 扫 /proc 反映进程真实存亡。断言只看真实副作用：
# 进程生死、文件系统、退出码、systemctl 调用记录。

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

FAKE_SYSTEMCTL = r'''#!/bin/sh
# 测试桩 systemctl：调用全记录；对 cadence-relay@X 的 start/restart 真实
# nohup $RELAY_RUN_SCRIPT，disable/stop 真实 kill（MainPID 语义与 systemd 一致）
LOG=${SYSTEMCTL_LOG:?}; ST=${FAKE_STATE_DIR:?}
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
      case $u in cadence-relay@*)
        n=${u#cadence-relay@}; n=${n%%.service*}
        d=$(dirname "$RELAY_UNIT_TEMPLATE")/multi-user.target.wants
        mkdir -p "$d"
        ln -sf "$RELAY_UNIT_TEMPLATE" "$d/cadence-relay@$n.service" ;;
      esac
    done
    exit 0 ;;
  restart|start)
    [ -e "$ST/restart_fail" ] && exit 1
    for u in "$@"; do
      case $u in cadence-relay@*)
        n=${u#cadence-relay@}; n=${n%%.service*}
        if [ -f "$ST/$n.mainpid" ]; then
          kill "$(cat "$ST/$n.mainpid")" 2>/dev/null; sleep 0.2
        fi
        nohup "$RELAY_RUN_SCRIPT" "$n" >>"$RELAY_LOG_FILE" 2>&1 &
        echo $! >"$ST/$n.mainpid" ;;
      esac
    done
    exit 0 ;;
  disable|stop)
    for u in "$@"; do
      case $u in cadence-relay@*)
        n=${u#cadence-relay@}; n=${n%%.service*}
        if [ -f "$ST/$n.mainpid" ]; then
          kill "$(cat "$ST/$n.mainpid")" 2>/dev/null
          rm -f "$ST/$n.mainpid"
        fi
        rm -f "$(dirname "$RELAY_UNIT_TEMPLATE")/multi-user.target.wants/cadence-relay@$n.service" ;;
      esac
    done
    exit 0 ;;
  is-active)
    for u in "$@"; do
      case $u in cadence-relay@*)
        n=${u#cadence-relay@}; n=${n%%.service*}
        if [ -f "$ST/$n.mainpid" ] && kill -0 "$(cat "$ST/$n.mainpid")" 2>/dev/null; then
          echo active; exit 0
        fi ;;
      esac
    done
    echo inactive; exit 3 ;;
  is-enabled)
    for u in "$@"; do
      case $u in cadence-relay@*)
        n=${u#cadence-relay@}; n=${n%%.service*}
        if [ -e "$(dirname "$RELAY_UNIT_TEMPLATE")/multi-user.target.wants/cadence-relay@$n.service" ]; then
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

# fake socat 不再用脚本桩：产品的 exe 身份核验（readlink /proc/<pid>/exe 的
# basename 必须是 socat）要求沙箱 socat 是真实 ELF。做法：复制 coreutils
# yes 二进制为 $bin/socat（任意参数常驻、TERM 可杀、argv 原样保留）。
# 伪身份负例见 start_spoofed_listener（executable= 换 exe、伪装 argv[0]）。

FAKE_IP = r'''#!/bin/sh
echo "2: fake0    inet ${FAKE_BINDADDR:-127.0.0.1}/24 brd 10.89.255.255 scope global fake0"
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


class VmSandbox:
    """POSIX 沙箱：bin 桩 + 沙箱路径环境变量，真实执行 devbox/relay-vm.sh。"""

    def __init__(self, tmp_path, *, systemd_state="running",
                 fake_bind="10.89.0.1", wsl_conf=None):
        root = tmp_path / "vm"
        self.bin = root / "bin"
        self.unit_dir = root / "etc" / "systemd" / "system"
        self.run_script = root / "app" / "cadence-relay-run"
        self.boot_script = root / "app" / "cadence-relay-boot"
        self.env_file = root / "etc" / "cadence-relay.env"
        self.log_file = root / "var" / "cadence-relay.log"
        self.wsl_conf = root / "etc" / "wsl.conf"
        self.state_dir = root / "fake-systemd-state"
        self.systemctl_log = root / "systemctl.log"
        self.dnf_log = root / "dnf.log"
        for d in (self.bin, self.unit_dir, self.run_script.parent,
                  self.env_file.parent, self.log_file.parent, self.state_dir):
            d.mkdir(parents=True, exist_ok=True)
        for name, body in (("ss", FAKE_SS), ("systemctl", FAKE_SYSTEMCTL),
                           ("ip", FAKE_IP), ("dnf", FAKE_DNF)):
            f = self.bin / name
            f.write_text(body, encoding="utf-8")
            f.chmod(0o755)
        socat_bin = self.bin / "socat"
        shutil.copy2(shutil.which("yes"), socat_bin)  # 真实 ELF：exe 核验可过
        socat_bin.chmod(0o755)
        if wsl_conf is not None:
            self.wsl_conf.write_text(wsl_conf, encoding="utf-8")
        # ELF 桩 socat（yes 副本）会满速写 stdout：全部日志重定向 /dev/null。
        # relay-vm.sh 对 LOG_FILE 仅追加/末尾 rm -f（rm /dev/null 失败不中断，
        # cmd_teardown 收尾是 log → 退出码仍 0），无文件内容断言，安全。
        self.env = dict(
            os.environ,
            PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            RELAY_ENV_FILE=str(self.env_file),
            RELAY_RUN_SCRIPT=str(self.run_script),
            RELAY_BOOT_SCRIPT=str(self.boot_script),
            RELAY_UNIT_TEMPLATE=str(self.unit_dir / "cadence-relay@.service"),
            RELAY_LOG_FILE="/dev/null",
            RELAY_WSL_CONF=str(self.wsl_conf),
            SYSTEMCTL_LOG=str(self.systemctl_log),
            FAKE_STATE_DIR=str(self.state_dir),
            FAKE_SYSTEMD_STATE=systemd_state,
            FAKE_BINDADDR=fake_bind,
            DNF_LOG=str(self.dnf_log),
        )
        self._procs = []

    def write_env(self, services):
        """services: {name: (vmport, hostport)}；bind 固定写服务表值 VM_BIND
        （fake_bind 只控制 fake ip 显示的网卡地址，用于网关漂移场景）。"""
        self.env_file.write_text(
            "".join(f"{n} {vp} {VM_BIND} {hp}\n"
                    for n, (vp, hp) in services.items()),
            encoding="utf-8")


    def start_listener(self, listen_arg, target_arg):
        """以 ELF socat 真实启动一个“前台 socat”进程（exe/argv 均真），返回 Popen。"""
        p = subprocess.Popen(
            [str(self.bin / "socat"), listen_arg, target_arg],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._procs.append(p)
        return p

    def start_spoofed_listener(self, listen_arg, target_arg):
        """伪身份占用者：argv 与服务表完全一致（argv[0]=socat），但 exe 换成
        yes——产品的 /proc/<pid>/exe 核验必须把它拒绝为未知占用者。"""
        p = subprocess.Popen(
            ["socat", listen_arg, target_arg],
            executable=shutil.which("yes"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._procs.append(p)
        return p

    def supervisor_pids(self):
        """自有监督：exe 为 shell 解释器且完整 argv 恰为 [解释器, BOOT_SCRIPT]
        两项（与 relay-vm.sh relay_supervisor_pids 同构；argv 仅“包含”路径
        不足以判定——编辑器打开同一文件形态相同）。"""
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

    def run(self, *args, timeout=120):
        return subprocess.run(["bash", str(RELAY_VM), *args], env=self.env,
                              capture_output=True, text=True, timeout=timeout)

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

    def calls(self):
        if not self.systemctl_log.exists():
            return []
        return [l for l in
                self.systemctl_log.read_text(encoding="utf-8").splitlines() if l]

    def dnf_calls(self):
        if not self.dnf_log.exists():
            return []
        return self.dnf_log.read_text(encoding="utf-8").splitlines()

    def mainpid(self, name):
        f = self.state_dir / f"{name}.mainpid"
        return int(f.read_text().strip()) if f.exists() else 0

    def cleanup(self):
        marker = str(self.bin / "socat").encode()
        sup_pids = self.supervisor_pids()  # 收养自有监督（同构判定，防误杀编辑器形态）
        for entry in Path("/proc").iterdir():  # 收养孤儿监听与 boot 监督进程
            if not entry.name.isdigit():
                continue
            if int(entry.name) in sup_pids:
                try:
                    os.kill(int(entry.name), signal.SIGTERM)
                except OSError:
                    pass
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
        for f in self.state_dir.glob("*.mainpid"):
            try:
                os.kill(int(f.read_text().strip()), signal.SIGTERM)
            except (OSError, ValueError):
                pass
        for p in self._procs:
            if p.poll() is None:
                p.terminate()
        for p in self._procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


@pytest.fixture
def vm_factory(tmp_path):
    sandboxes = []

    def make(**kw):
        s = VmSandbox(tmp_path, **kw)
        sandboxes.append(s)
        return s

    yield make
    for s in sandboxes:
        s.cleanup()


VM_SVC = {"mysql": (45331, 47331), "redis": (45332, 47332)}
VM_BIND = "10.89.0.1"


def _listen_arg(name):
    return f"TCP-LISTEN:{VM_SVC[name][0]},bind={VM_BIND},fork,reuseaddr"


def _target_arg(name):
    return f"TCP:127.0.0.1:{VM_SVC[name][1]}"


def _wait_dead(p, timeout_s=8):
    for _ in range(int(timeout_s * 10)):
        if p.poll() is not None:
            return True
        time.sleep(0.1)
    return False


@needs_vm
def test_vm_setup_free_ports_writes_per_service_units(vm_factory):
    """端口空闲：setup 成功，写每服务模板单元与 exec 型 run 脚本，真实拉起监听。"""
    vm = vm_factory()
    vm.write_env(VM_SVC)
    r = vm.run("setup")
    assert r.returncode == 0, r.stdout + r.stderr
    unit = (vm.unit_dir / "cadence-relay@.service").read_text(encoding="utf-8")
    assert "StartLimitIntervalSec=0" in unit
    assert "Restart=always" in unit and "RestartSec=5" in unit
    assert f"ExecStart={vm.run_script} %i" in unit
    run_text = vm.run_script.read_text(encoding="utf-8")
    assert "command -v socat" in run_text, "socat 路径须运行时 command -v 动态解析"
    assert 'exec "$SOCAT"' in run_text, "run 脚本必须 exec socat 前台（MainPID=监听进程）"
    for name, (vp, _hp) in VM_SVC.items():
        assert vm.pids_on_port(vp) == [vm.mainpid(name)], \
            f"{name} 监听 pid 必须等于单元 MainPID"
    calls = " ".join(vm.calls())
    assert "enable cadence-relay@mysql" in calls
    assert "enable cadence-relay@redis" in calls
    assert "restart cadence-relay@mysql" in calls


@needs_vm
def test_vm_setup_exit3_without_force_keeps_listener(vm_factory):
    """匹配的前台 socat 占用：无 --force 时 exit 3，进程不动，不写任何文件。"""
    vm = vm_factory()
    vm.write_env(VM_SVC)
    p = vm.start_listener(_listen_arg("mysql"), _target_arg("mysql"))
    r = vm.run("setup")
    assert r.returncode == 3, r.stdout + r.stderr
    assert p.poll() is None, "未确认前不得杀占用者"
    assert not vm.run_script.exists()
    assert not (vm.unit_dir / "cadence-relay@.service").exists()


@needs_vm
def test_vm_setup_force_rejects_unknown_listener(vm_factory):
    """未知占用者（argv 对不上）：--force 也拒绝（exit 3），任何进程都不动。"""
    vm = vm_factory()
    vm.write_env(VM_SVC)
    ok = vm.start_listener(_listen_arg("mysql"), _target_arg("mysql"))
    bad = vm.start_listener(
        f"TCP-LISTEN:{VM_SVC['redis'][0]},bind=192.0.2.9,fork,reuseaddr",
        f"TCP:127.0.0.1:{VM_SVC['redis'][1] + 1}")
    r = vm.run("setup", "--force")
    assert r.returncode == 3, r.stdout + r.stderr
    assert ok.poll() is None and bad.poll() is None, "未知占用者与匹配占用者都不能被杀"
    assert not vm.run_script.exists(), "拒绝路径不得写任何文件"


@needs_vm
def test_vm_setup_force_takes_over_exact_match(vm_factory):
    """--force：仅接管 argv 完全一致的前台 socat，监听转到 systemd 实例名下。"""
    vm = vm_factory()
    vm.write_env(VM_SVC)
    p = vm.start_listener(_listen_arg("mysql"), _target_arg("mysql"))
    old_pid = p.pid
    r = vm.run("setup", "--force")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _wait_dead(p), "被接管的前台 socat 必须退出"
    pids = vm.pids_on_port(VM_SVC["mysql"][0])
    assert pids and old_pid not in pids, "监听必须由 systemd 实例的新进程接管"
    assert vm.mainpid("mysql") in pids
    assert vm.mainpid("redis") in vm.pids_on_port(VM_SVC["redis"][0])


@needs_vm
def test_vm_setup_rollback_on_enable_failure(vm_factory):
    """enable 失败：回滚——原监听按原命令行恢复，本次新建文件清空。"""
    vm = vm_factory()
    vm.write_env(VM_SVC)
    p = vm.start_listener(_listen_arg("mysql"), _target_arg("mysql"))
    (vm.state_dir / "enable_fail").write_text("", encoding="utf-8")
    r = vm.run("setup", "--force")
    assert r.returncode == 1, r.stdout + r.stderr
    for _ in range(80):
        if vm.pids_on_port(VM_SVC["mysql"][0]):
            break
        time.sleep(0.1)
    pids = vm.pids_on_port(VM_SVC["mysql"][0])
    assert pids, "回滚必须以原命令行恢复接管前的监听"
    assert p.pid not in pids, "恢复的必须是新进程（原前台窗口已不持有）"
    assert not (vm.unit_dir / "cadence-relay@.service").exists(), "回滚须清理本次新建文件"
    assert not vm.run_script.exists()


@needs_vm
def test_vm_setup_never_stops_when_systemd_unusable(vm_factory):
    """systemd 不可用且 wsl.conf 有外来 [boot] 段：显式失败，占用者与文件零变动。"""
    vm = vm_factory(systemd_state="offline", wsl_conf="[boot]\nsystemd=true\n")
    vm.write_env(VM_SVC)
    p = vm.start_listener(_listen_arg("mysql"), _target_arg("mysql"))
    r = vm.run("setup", "--force")
    assert r.returncode == 1, r.stdout + r.stderr
    assert p.poll() is None, "持久化方式未确定前不得动占用者"
    assert not vm.run_script.exists()
    conf = vm.wsl_conf.read_text(encoding="utf-8")
    assert conf == "[boot]\nsystemd=true\n", "不得改动外来 wsl.conf"


@needs_vm
def test_vm_setup_boot_fallback_without_systemd(vm_factory):
    """无 systemd：wsl.conf [boot] 兜底逐服务拉起，接管同样精确，不调 enable。"""
    vm = vm_factory(systemd_state="offline", wsl_conf="")
    vm.write_env(VM_SVC)
    p = vm.start_listener(_listen_arg("mysql"), _target_arg("mysql"))
    r = vm.run("setup", "--force")
    assert r.returncode == 0, r.stdout + r.stderr
    conf = vm.wsl_conf.read_text(encoding="utf-8")
    assert "cadence-relay-boot" in conf, "兜底钩子须写入 wsl.conf"
    assert conf.count("[boot]") == 1, "wsl.conf 只保留一个钩子段（幂等）"
    assert _wait_dead(p), "兜底路径同样只接管完全匹配的前台 socat"
    for name, (vp, _hp) in VM_SVC.items():
        assert vm.pids_on_port(vp), f"{name} 兜底监听必须存在"
    assert all("enable" not in c for c in vm.calls()), "无 systemd 时不得调用 enable"


@needs_vm
def test_vm_status_reports_pid_bind_and_loopback(vm_factory):
    """status：逐服务报告 MainPID=监听 pid、bind 在网卡、回环真实探活。"""
    vm = vm_factory()
    vm.write_env(VM_SVC)
    assert vm.run("setup").returncode == 0
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", VM_SVC["mysql"][1]))
    srv.listen(4)
    try:
        r = vm.run("status")
        assert r.returncode == 1, "redis 回环无监听，整体须 FAIL"
        assert "MainPID" in r.stdout
        assert f"bind {VM_BIND} 在网卡上 OK" in r.stdout
        assert f"回环 127.0.0.1:{VM_SVC['mysql'][1]} OK" in r.stdout
        assert f"回环 127.0.0.1:{VM_SVC['redis'][1]} FAIL" in r.stdout
    finally:
        srv.close()


@needs_vm
def test_vm_status_flags_gateway_drift(vm_factory):
    """bind 地址不在网卡上：status 明确提示 podman 网络重建、需重跑 setup。"""
    vm = vm_factory(fake_bind="192.0.2.77")
    vm.write_env(VM_SVC)
    vm.start_listener(_listen_arg("mysql"), _target_arg("mysql"))
    r = vm.run("status")
    assert "重跑 setup" in r.stdout, "网关漂移必须给出重跑指引"


@needs_vm
def test_vm_teardown_removes_only_own_artifacts(vm_factory):
    """teardown：清自有 units/脚本/env/日志；不动未知监听、无关文件、socat 包。"""
    vm = vm_factory()
    vm.write_env(VM_SVC)
    assert vm.run("setup").returncode == 0
    other_unit = vm.unit_dir / "other-app.service"
    other_unit.write_text("[Unit]\nDescription=unrelated\n", encoding="utf-8")
    stranger = vm.start_listener(
        f"TCP-LISTEN:{VM_SVC['redis'][0]},bind=192.0.2.5,fork,reuseaddr",
        f"TCP:127.0.0.1:{VM_SVC['redis'][1]}")
    r = vm.run("teardown")
    assert r.returncode == 0, r.stdout + r.stderr
    assert stranger.poll() is None, "未知监听必须保留"
    assert other_unit.exists(), "无关 unit 文件必须保留"
    assert not vm.env_file.exists()
    assert not vm.run_script.exists()
    assert not (vm.unit_dir / "cadence-relay@.service").exists()
    calls = " ".join(vm.calls())
    assert "disable --now cadence-relay@mysql" in calls
    assert "disable --now cadence-relay@redis" in calls
    assert vm.dnf_calls() == [], "socat 缺失时才可 dnf install，teardown 不得卸载"
    assert vm.pids_on_port(VM_SVC["mysql"][0]) == []
    assert stranger.pid in vm.pids_on_port(VM_SVC["redis"][0])


@needs_vm
def test_vm_teardown_tolerates_missing_env(vm_factory):
    """env 已缺失：teardown 仍按 wants 链接停实例并清理其余自有文件。"""
    vm = vm_factory()
    vm.write_env(VM_SVC)
    assert vm.run("setup").returncode == 0
    vm.env_file.unlink()
    r = vm.run("teardown")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not vm.run_script.exists()
    assert not (vm.unit_dir / "cadence-relay@.service").exists()
    for name, (vp, _hp) in VM_SVC.items():
        assert vm.pids_on_port(vp) == [], f"{name} 实例监听须全部停止"


@needs_vm
def test_vm_setup_rejects_spoofed_exe(vm_factory):
    """伪身份占用者（argv 完全一致但 exe 是 yes）：--force 也必须拒绝且不杀。"""
    vm = vm_factory()
    vm.write_env(VM_SVC)
    p = vm.start_spoofed_listener(_listen_arg("mysql"), _target_arg("mysql"))
    r = vm.run("setup", "--force")
    assert r.returncode == 3, "exe 身份不符必须按未知占用者 exit 3 拒绝"
    assert p.poll() is None, "伪身份进程不得被杀"
    assert not vm.run_script.exists(), "拒绝路径不得写任何文件"


@needs_vm
def test_vm_read_env_rejects_duplicates(vm_factory):
    """服务表重复服务名/重复 vmport：在任何写入前显式报错退出。"""
    vm = vm_factory()
    vm.env_file.write_text(
        f"mysql {VM_SVC['mysql'][0]} {VM_BIND} 47331\n"
        f"mysql {VM_SVC['redis'][0]} {VM_BIND} 47332\n",
        encoding="utf-8")
    r = vm.run("setup")
    assert r.returncode == 1
    assert "服务名重复" in r.stdout + r.stderr
    vm.env_file.write_text(
        f"mysql {VM_SVC['mysql'][0]} {VM_BIND} 47331\n"
        f"redis {VM_SVC['mysql'][0]} {VM_BIND} 47332\n",
        encoding="utf-8")
    r2 = vm.run("setup")
    assert r2.returncode == 1
    assert "vmport 重复" in r2.stdout + r2.stderr
    assert not vm.run_script.exists()


@needs_vm
def test_vm_setup_rollback_preserves_existing_units(vm_factory):
    """已有两服务正常时重跑失败：事务回滚保住原 enabled/文件，不误停。
    防假通过：必须证明原监听 pid 真被接管杀死、恢复的是新进程——
    否则“原进程一直活着”也会让监听断言通过。"""
    vm = vm_factory()
    vm.write_env(VM_SVC)
    assert vm.run("setup").returncode == 0
    wants_mysql = (vm.unit_dir / "multi-user.target.wants"
                   / "cadence-relay@mysql.service")
    assert wants_mysql.exists()
    old_pid = vm.pids_on_port(VM_SVC["mysql"][0])[0]
    (vm.state_dir / "restart_fail").write_text("", encoding="utf-8")
    r = vm.run("setup", "--force")
    assert r.returncode == 1, r.stdout + r.stderr
    calls = " ".join(vm.calls())
    assert "disable" not in calls, "回滚不得取消原有 enabled"
    assert wants_mysql.exists(), "回滚须保持原有 wants 链接（enabled）"
    assert vm.run_script.exists(), "回滚须保留（还原）原有 run 脚本"
    assert (vm.unit_dir / "cadence-relay@.service").exists()
    try:
        os.kill(old_pid, 0)
        old_dead = False
    except OSError:
        old_dead = True
    assert old_dead, "原监听必须被 --force 真实接管杀死（否则恢复断言无意义）"
    pids = []
    for _ in range(80):
        pids = vm.pids_on_port(VM_SVC["mysql"][0])
        if pids and old_pid not in pids:
            break
        time.sleep(0.1)
    assert pids and old_pid not in pids, \
        "回滚须以新进程恢复被接管监听（原 argv nohup）"


@needs_vm
def test_vm_boot_supervisor_restarts_listener(vm_factory):
    """boot 兜底监督：监听进程死亡后在巡检周期内自动重拉（非 systemd 自恢复）。"""
    vm = vm_factory(systemd_state="offline", wsl_conf="")
    vm.write_env(VM_SVC)
    assert vm.run("setup", "--force").returncode == 0
    pids = vm.pids_on_port(VM_SVC["mysql"][0])
    assert pids, "boot 模式监听须存在"
    os.kill(pids[0], signal.SIGKILL)
    back = False
    for _ in range(120):  # 监督 5 秒巡检一轮，12 秒余量
        new = vm.pids_on_port(VM_SVC["mysql"][0])
        if new and pids[0] not in new:
            back = True
            break
        time.sleep(0.1)
    assert back, "监督须自动重拉死亡监听"


@needs_vm
def test_vm_teardown_kills_boot_supervisor(vm_factory):
    """teardown 须先停 boot 监督循环再清监听，防止拆除后被重拉。"""
    vm = vm_factory(systemd_state="offline", wsl_conf="")
    vm.write_env(VM_SVC)
    assert vm.run("setup", "--force").returncode == 0
    assert vm.supervisor_pids(), "boot 模式须存在监督进程"
    r = vm.run("teardown")
    assert r.returncode == 0, r.stdout + r.stderr
    assert vm.supervisor_pids() == [], "teardown 须停监督循环"
    for name, (vp, _hp) in VM_SVC.items():
        assert vm.pids_on_port(vp) == [], f"{name} 兜底监听须全部停止"


# ---------------------------------------------------------------------------
# 2026-10-16 多服务改造：setup 前段自有实例回收（服务删除的 VM 侧对称清理）。


@needs_vm
def test_vm_setup_recycles_removed_own_instances(vm_factory):
    """env 删除某服务后重跑 setup：回收该自有实例（disable+清链接），未知不动。"""
    vm = vm_factory()
    vm.write_env({"mysql": VM_SVC["mysql"], "redis": VM_SVC["redis"]})
    r = vm.run("setup")
    assert r.returncode == 0, r.stdout + r.stderr
    assert vm.mainpid("redis") in vm.pids_on_port(VM_SVC["redis"][0])
    # 未知 wants 链接（指向外来 unit 文件）：一律不动
    foreign = vm.unit_dir / "other.service"
    foreign.write_text("[Unit]\nDescription=foreign\n", encoding="utf-8")
    stranger = vm.unit_dir / "multi-user.target.wants" / "cadence-relay@stranger.service"
    stranger.symlink_to(foreign)
    vm.write_env({"mysql": VM_SVC["mysql"]})  # redis 从服务表删除
    r2 = vm.run("setup", "--force")  # 沙箱内运行中实例无 cgroup 归属，重跑须 --force
    assert r2.returncode == 0, r2.stdout + r2.stderr
    calls = vm.calls()
    assert "disable --now cadence-relay@redis" in calls, "须回收被删服务的实例"
    assert vm.pids_on_port(VM_SVC["redis"][0]) == [], "redis 实例监听须停止"
    wants = vm.unit_dir / "multi-user.target.wants"
    assert not (wants / "cadence-relay@redis.service").exists(), "wants 链接须清除"
    assert stranger.is_symlink(), "未知 wants 链接不得动"
    assert not any("stranger" in c for c in calls), "未知实例不得被 disable"
    assert vm.mainpid("mysql") in vm.pids_on_port(VM_SVC["mysql"][0]), "mysql 须不受影响"


@needs_vm
def test_vm_setup_recycle_ownership_rejects_tampered_template(vm_factory):
    """回收所有权核验：模板 Description 非本方案产物时，指向模板的链接也不动。"""
    vm = vm_factory()
    vm.write_env({"mysql": VM_SVC["mysql"]})
    r = vm.run("setup")
    assert r.returncode == 0, r.stdout + r.stderr
    tmpl = vm.unit_dir / "cadence-relay@.service"
    body = tmpl.read_text(encoding="utf-8").replace(
        "Description=cadence relay", "Description=foreign relay")
    tmpl.write_text(body, encoding="utf-8")
    vm.write_env({"redis": VM_SVC["redis"]})  # mysql 不在新服务表
    r2 = vm.run("setup")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    wants = vm.unit_dir / "multi-user.target.wants"
    assert (wants / "cadence-relay@mysql.service").exists(), "所有权核验失败不得回收"
    joined = "\n".join(vm.calls())
    assert "disable --now cadence-relay@mysql" not in joined
    assert vm.pids_on_port(VM_SVC["mysql"][0]), "mysql 实例监听须保持"


@needs_vm
def test_vm_setup_boot_retires_removed_listeners(vm_factory):
    """boot 兜底删除回收：--retire 旧表对账杀被删服务的自有监听；未知不动。"""
    vm = vm_factory(systemd_state="offline", wsl_conf="")
    vm.write_env(VM_SVC)
    assert vm.run("setup", "--force").returncode == 0
    assert vm.pids_on_port(VM_SVC["redis"][0]), "boot 模式两监听须在"
    # 未知监听占第三个端口：argv 与 retire 表行对不上（hostport 差一），不得动
    stranger = vm.start_listener(
        f"TCP-LISTEN:45399,bind={VM_BIND},fork,reuseaddr", "TCP:127.0.0.1:47398")
    vm.write_env({"mysql": VM_SVC["mysql"]})  # redis 从服务表删除
    retire = vm.env_file.parent / "cadence-relay.retired.env"
    retire.write_text(
        f"redis {VM_SVC['redis'][0]} {VM_BIND} {VM_SVC['redis'][1]}\n"
        f"ghost 45399 {VM_BIND} 47399\n",
        encoding="utf-8")
    r = vm.run("setup", "--force", "--retire", str(retire))
    assert r.returncode == 0, r.stdout + r.stderr
    assert vm.pids_on_port(VM_SVC["redis"][0]) == [], "被删服务的 boot 监听须回收"
    assert stranger.pid in vm.pids_on_port(45399), "未知监听不得动"
    assert vm.pids_on_port(VM_SVC["mysql"][0]), "保留服务监听须仍在"
