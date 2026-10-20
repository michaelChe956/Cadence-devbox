# -*- coding: utf-8 -*-
"""relay 一键通道脚本的契约校验（relay.ps1 / relay-vm.sh / 文档集成）。

除源码结构契约外，用 pwsh 执行从 relay.ps1 AST 抽取的真实函数做行为回归
（守卫的平台/运行状态判定、原生错误显式传播、Die 退出码契约）。
"""
import json
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
        "[switch]$List",
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
    """推导规则钉住（2026-10-19 裁决）：hostPort=远端+10000、vmPort=hostPort+10000，
    占用（已分配端口或 stack 发布端口）时 host/vm 同步顺延 +1。"""
    text = RELAY_PS1.read_text(encoding="utf-8")
    assert "Get-RelayPorts" in text, "缺端口推导函数 Get-RelayPorts"
    assert "+ 10000" in text, "推导字面量 +10000 缺失（hostPort=远端+10000、vmPort=hostPort+10000）"
    assert "45535" in text, "缺远端端口上限校验（1-45535）"
    assert "55535" in text, "缺顺延后 hostPort 上限（55535，vmPort 恒 ≤65535）"
    assert "46535" not in text, "旧错值 46535 须清除"


def test_relay_stack_ports_guard_matches_compose():
    """顺延越过集合：Get-StackPortList 内置 stack 发布端口清单须与 compose 一致。"""
    text = RELAY_PS1.read_text(encoding="utf-8")
    m = re.search(r"\$stack = @\(([^)]*)\)", text)
    assert m, "relay.ps1 缺内置 stack 发布端口清单 $stack"
    assert "Get-StackPortList" in text, "stack 清单须以 Get-StackPortList 单一来源供推导/显式校验共用"
    got = {int(x) for x in re.findall(r"\d+", m.group(1))}
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    published = set()
    for spec in doc["services"].values():
        for p in spec.get("ports", []):
            published.add(int(str(p).split(":")[1]))
    assert got == published, f"$stack {sorted(got)} 与 compose {sorted(published)} 不一致"


def test_catalog_derived_ports_vs_compose():
    """目录各服务按常见远端端口推导并顺延后与 compose 发布端口零重叠
    （rabbitmq 5672 撞 15672 自动越过得 15673/25673）。"""
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    published = set()
    for spec in doc["services"].values():
        for p in spec.get("ports", []):
            published.add(int(str(p).split(":")[1]))
    for name, remote in CATALOG_REMOTE_PORTS.items():
        host = remote + 10000
        while host in published or (host + 10000) in published:
            host += 1
        assert host <= 55535, f"{name} 顺延越界（hostPort 上限 55535）"
        hit = {host, host + 10000} & published
        assert not hit, f"{name} 顺延后推导端口仍与 compose 重叠：{sorted(hit)}"
        if name == "rabbitmq":
            assert host == 15673, "rabbitmq 顺延假设失效（应越过 15672 得 15673），文档须复核"


def test_relay_ps1_edit_and_list_pins():
    """2026-10-19 裁决钉住：修改问答提示语、-List 入口、gateway 落盘与开关互斥。"""
    text = RELAY_PS1.read_text(encoding="utf-8")
    assert "要修改的已有序号（回车跳过，逗号分隔）" in text, "缺修改问答提示"
    assert "新本地端口（回车=自动推导；直接输入数字覆盖）" in text, "缺本地端口覆盖提示"
    assert "gateway = $gw" in text, "setup 落盘 relay.json 须记录网关（-List 免探测）"
    assert "Invoke-List" in text, "缺 -List 实现（Invoke-List）"
    assert "只能选一" in text, "-Status/-Teardown/-List 须互斥"
    assert "TcpClient" not in text, "终端管控红线：不得出现 TcpClient 字面量"


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
foreach ($n in @('Info','Warn','Die','Get-StackPortList','Get-RelayPorts')) {
  $fs = $ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true)
  if (-not $fs) { Write-Host "MISSING_FUNC: $n"; exit 3 }
  Invoke-Expression $fs[0].Extent.Text
}
foreach ($p in @(3306, 6379, 9092, 2181, 5672, 45535)) {
  $r = Get-RelayPorts $p @{}
  Write-Host "port $p host=$($r.hostPort) vm=$($r.vmPort)"
}
exit 0
"""

DERIVE_SHIFT_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { Write-Host "PARSE_ERROR: $($errors[0].Message)"; exit 2 }
foreach ($n in @('Info','Warn','Die','Get-StackPortList','Get-RelayPorts')) {
  $fs = $ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true)
  if (-not $fs) { Write-Host "MISSING_FUNC: $n"; exit 3 }
  Invoke-Expression $fs[0].Extent.Text
}
$t = @{}
$a = Get-RelayPorts 3306 $t
$t[$a.hostPort] = 'first'
$t[$a.vmPort] = 'first'
$b = Get-RelayPorts 3306 $t
Write-Host "first host=$($a.hostPort) vm=$($a.vmPort)"
Write-Host "second host=$($b.hostPort) vm=$($b.vmPort)"
exit 0
"""

DERIVE_OVERFLOW_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { Write-Host "PARSE_ERROR: $($errors[0].Message)"; exit 2 }
foreach ($n in @('Info','Warn','Die','Get-StackPortList','Get-RelayPorts')) {
  $fs = $ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true)
  if (-not $fs) { Write-Host "MISSING_FUNC: $n"; exit 3 }
  Invoke-Expression $fs[0].Extent.Text
}
$t = @{}
$t[55535] = 'blocker'
$null = Get-RelayPorts 45535 $t
Write-Host 'UNREACHABLE'
exit 0
"""

DERIVE_RANGE_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { Write-Host "PARSE_ERROR: $($errors[0].Message)"; exit 2 }
foreach ($n in @('Info','Warn','Die','Get-StackPortList','Get-RelayPorts')) {
  $fs = $ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true)
  if (-not $fs) { Write-Host "MISSING_FUNC: $n"; exit 3 }
  Invoke-Expression $fs[0].Extent.Text
}
$null = Get-RelayPorts 45536 @{}
Write-Host 'UNREACHABLE'
exit 0
"""

# 期望：远端端口 → (hostPort, vmPort)；mysql/redis 两行为存量兼容锚点（不得漂移）；
# 5672 验证自动越过 stack（15672→15673/25673）；45535 为上界锚点（+20000 恰 65535）
DERIVED_EXPECT = {
    3306: (13306, 23306), 6379: (16379, 26379),
    9092: (19092, 29092), 2181: (12181, 22181),
    5672: (15673, 25673), 45535: (55535, 65535),
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
        'if [ "$1" = "network" ] && [ "$2" = "inspect" ]; then\n'
        '  echo "${FAKE_NET_GW:-10.89.0.1}"\n'
        "  exit 0\n"
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
def test_relay_derivation_autoshifts_on_duplicate(tmp_path):
    """多实例同远端端口：占用表内二次推导 3306 自动顺延 +1（vmPort=hostPort+10000）。"""
    r = _run_harness(DERIVE_SHIFT_HARNESS, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "first host=13306 vm=23306" in r.stdout, r.stdout
    assert "second host=13307 vm=23307" in r.stdout, r.stdout


@needs_pwsh
def test_relay_derivation_shift_overflow_dies(tmp_path):
    """顺延越界：45535 的推导端口（55535/65535）被占后顺延须 Die 说明越界。"""
    r = _run_harness(DERIVE_OVERFLOW_HARNESS, tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "顺延越界" in r.stdout and "55535" in r.stdout
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
# 2026-10-19 端口自动顺延 + 交互修改 + -List：AST 抽取真实函数，
# Read-Host 用同名函数遮蔽回放预置答案，Collect-Services/Invoke-List 全流程行为回归。

COLLECT_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { Write-Host "PARSE_ERROR: $($errors[0].Message)"; exit 2 }
foreach ($n in @('Info','Warn','Die','Test-AddrFormat','Test-SvcName','Test-HostName','Split-AddrInput','Get-StackPortList','Get-RelayPorts','New-Service','New-ServiceWithHostPort','Collect-Services')) {
  $fs = $ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true)
  if (-not $fs) { Write-Host "MISSING_FUNC: $n"; exit 3 }
  Invoke-Expression $fs[0].Extent.Text
}
$ServiceCatalog = [ordered]@{
  mysql = 3306; redis = 6379; kafka = 9092; zookeeper = 2181; rabbitmq = 5672
}
$MySQL = $env:FAKE_MYSQL
$Redis = $env:FAKE_REDIS
$script:ansArr = @(($env:FAKE_ANSWERS -split "`n"))
$script:ansPos = 0
function Read-Host($prompt) {
  if ($script:ansPos -ge $script:ansArr.Count) { Write-Host 'NO_MORE_ANSWERS'; exit 4 }
  $v = $script:ansArr[$script:ansPos]
  $script:ansPos = $script:ansPos + 1
  Write-Host "ANSWER[$v]: $prompt"
  return $v
}
$cfg = $null
if ($env:FAKE_RELAY_JSON -and (Test-Path $env:FAKE_RELAY_JSON)) {
  $cfg = Get-Content -Raw $env:FAKE_RELAY_JSON | ConvertFrom-Json
}
$svcs = @(Collect-Services $cfg)
Write-Host 'RESULT-BEGIN'
foreach ($s in $svcs) {
  Write-Host ("SVC {0} {1}:{2} host={3} vm={4}" -f $s.name, $s.remoteHost, $s.remotePort, $s.hostPort, $s.vmPort)
  # 域名段单列一行：既有 SVC 行格式保持不变（存量断言依赖），DOM 行专测 host 贯通
  $dom = '-'
  if ($s.PSObject.Properties['host'] -and $s.host) { $dom = $s.host }
  Write-Host ("DOM {0} {1}" -f $s.name, $dom)
}
Write-Host 'RESULT-END'
exit 0
"""

LIST_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { Write-Host "PARSE_ERROR: $($errors[0].Message)"; exit 2 }
foreach ($n in @('Info','Warn','Die','RunQuiet','Probe','Load-Config','Get-BridgeGateway','Invoke-List')) {
  $fs = $ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true)
  if (-not $fs) { Write-Host "MISSING_FUNC: $n"; exit 3 }
  Invoke-Expression $fs[0].Extent.Text
}
$RelayJson = $env:FAKE_RELAY_JSON
Invoke-List
exit 0
"""


def _svc(name, host, remote_port, host_port, vm_port, *, domain=None):
    svc = {
        "name": name, "remoteHost": host, "remotePort": remote_port,
        "hostPort": host_port, "vmPort": vm_port,
    }
    if domain is not None:
        svc["host"] = domain
    return svc


def _run_collect(tmp_path, answers, relay_json=None, mysql=None, redis=None):
    rj = tmp_path / "relay.json"
    if relay_json is not None:
        rj.write_text(json.dumps(relay_json), encoding="utf-8")
    env = {
        "FAKE_ANSWERS": "\n".join(answers),
        "FAKE_RELAY_JSON": str(rj),
        "FAKE_MYSQL": mysql or "",
        "FAKE_REDIS": redis or "",
    }
    return _run_harness(COLLECT_HARNESS, tmp_path, env_extra=env)


def _collect_result_lines(stdout):
    return [l.strip() for l in stdout.splitlines() if l.startswith("SVC ")]


@needs_pwsh
def test_collect_kept_items_keep_ports_and_order(tmp_path):
    """全保留+不修改+不新增：端口与顺序零漂移（回车跳过=配置零变化）。"""
    cfg = {"version": 1, "services": [
        _svc("mysql", "10.1.1.1", 3306, 13306, 23306),
        _svc("mysql_log", "10.1.1.2", 3306, 13307, 23307),
    ]}
    r = _run_collect(tmp_path, ["", "", ""], relay_json=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _collect_result_lines(r.stdout) == [
        "SVC mysql 10.1.1.1:3306 host=13306 vm=23306",
        "SVC mysql_log 10.1.1.2:3306 host=13307 vm=23307",
    ], r.stdout


@needs_pwsh
def test_collect_autoshifts_same_remote_port(tmp_path):
    """多实例同远端端口：-MySQL 新增 mysql 后自定义 mysql_log（同 3306）顺延 +1。"""
    r = _run_collect(
        tmp_path, ["custom", "mysql_log", "10.1.1.2:3306"],
        mysql="10.1.1.1:3306",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert _collect_result_lines(r.stdout) == [
        "SVC mysql 10.1.1.1:3306 host=13306 vm=23306",
        "SVC mysql_log 10.1.1.2:3306 host=13307 vm=23307",
    ], r.stdout
    assert "自动顺延" in r.stdout, "顺延发生时输出须带标记：\n" + r.stdout


@needs_pwsh
def test_collect_catalog_rabbitmq_shifts_over_stack(tmp_path):
    """目录 rabbitmq 5672：推导撞 15672 自动越过得 15673/25673，不再显式拒绝。"""
    r = _run_collect(tmp_path, ["rabbitmq", "10.1.1.3:5672"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _collect_result_lines(r.stdout) == [
        "SVC rabbitmq 10.1.1.3:5672 host=15673 vm=25673",
    ], r.stdout


@needs_pwsh
def test_collect_edit_address_keeps_ports(tmp_path):
    """修改问答改 IP：relay.json 结果换地址、端口不变（无冲突时推导=旧值）。"""
    cfg = {"version": 1, "services": [_svc("mysql", "10.1.1.1", 3306, 13306, 23306)]}
    r = _run_collect(tmp_path, ["", "1", "10.2.2.2:3306", "", ""], relay_json=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _collect_result_lines(r.stdout) == [
        "SVC mysql 10.2.2.2:3306 host=13306 vm=23306",
    ], r.stdout


@needs_pwsh
def test_collect_edit_explicit_port_syncs_vm(tmp_path):
    """修改问答显式端口 14000：vmPort 恒为 hostPort+10000=24000。"""
    cfg = {"version": 1, "services": [_svc("mysql", "10.1.1.1", 3306, 13306, 23306)]}
    r = _run_collect(tmp_path, ["", "1", "", "14000", ""], relay_json=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _collect_result_lines(r.stdout) == [
        "SVC mysql 10.1.1.1:3306 host=14000 vm=24000",
    ], r.stdout


@needs_pwsh
def test_collect_edit_explicit_port_stack_clash_dies(tmp_path):
    """显式端口撞 stack 发布端口（3306）：Die 说明，不产出任何结果。"""
    cfg = {"version": 1, "services": [_svc("mysql", "10.1.1.1", 3306, 13306, 23306)]}
    r = _run_collect(tmp_path, ["", "1", "", "3306"], relay_json=cfg)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "撞本地 stack" in r.stdout, r.stdout
    assert "RESULT-BEGIN" not in r.stdout


@needs_pwsh
def test_collect_edit_explicit_port_duplicate_dies(tmp_path):
    """显式端口与其他保留服务端口重复：Die 指明冲突服务。"""
    cfg = {"version": 1, "services": [
        _svc("mysql", "10.1.1.1", 3306, 13306, 23306),
        _svc("redis", "10.1.1.4", 6379, 16379, 26379),
    ]}
    r = _run_collect(tmp_path, ["", "2", "", "13306"], relay_json=cfg)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "与服务 mysql" in r.stdout, r.stdout
    assert "RESULT-BEGIN" not in r.stdout


@needs_pwsh
def test_collect_edit_index_not_kept_dies(tmp_path):
    """修改序号未在保留列表：Die 说明（不能修改被删除的项）。"""
    cfg = {"version": 1, "services": [
        _svc("mysql", "10.1.1.1", 3306, 13306, 23306),
        _svc("redis", "10.1.1.4", 6379, 16379, 26379),
    ]}
    r = _run_collect(tmp_path, ["1", "2"], relay_json=cfg)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "未在保留列表" in r.stdout, r.stdout


def _run_list(tmp_path, relay_json=None, env_extra=None):
    rj = tmp_path / "relay.json"
    if relay_json is not None:
        rj.write_text(json.dumps(relay_json), encoding="utf-8")
    env = {"FAKE_RELAY_JSON": str(rj)}
    env.update(env_extra or {})
    return _run_harness(LIST_HARNESS, tmp_path, env_extra=env)


@needs_pwsh
def test_list_prints_table_from_relay_json(tmp_path):
    """-List：只读 relay.json 打印表格（含 gateway 容器连接地址），不探活。"""
    cfg = {"version": 1, "gateway": "10.89.0.1", "services": [
        _svc("mysql", "10.129.12.75", 3306, 13306, 23306),
    ]}
    r = _run_list(tmp_path, relay_json=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    for token in ("mysql", "10.129.12.75:3306", "127.0.0.1:13306",
                  "10.89.0.1:23306", "Windows portproxy", "容器连接地址"):
        assert token in r.stdout, f"-List 输出缺 {token}：\n{r.stdout}"
    for line in r.stdout.splitlines():
        assert len(line) <= 100, f"-List 输出行超 100 字符：{line}"


@needs_pwsh
def test_list_dies_without_relay_json(tmp_path):
    """-List 无 relay.json：Die「通道未建立」，非零退出。"""
    r = _run_list(tmp_path)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "通道未建立" in r.stdout, r.stdout


@needs_pwsh
def test_list_legacy_json_resolves_gateway_live(tmp_path):
    """旧版 relay.json 无 gateway 字段：现场解析 podman 网桥网关（fake stub）。"""
    cfg = {"version": 1, "services": [_svc("mysql", "10.1.1.1", 3306, 13306, 23306)]}
    r = _run_list(tmp_path, relay_json=cfg, env_extra={"FAKE_NET_GW": "10.99.0.1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "10.99.0.1:23306" in r.stdout, r.stdout


# ---------------------------------------------------------------------------
# 2026-10-20 域名同问句录入 + DNAT 映射表（设计 §5.3/§5.4/R-S1）：
# 地址问句扩展为 "IP:端口[ 域名]"（同一问句、无二次询问），relay.ps1 生成
# stack/data/relay-map.env 并下发 VM /etc/cadence-relay-dnat.env
# （5 段必填 + 可选第 6 段域名，缺省用 '-' 占位）。

VALID_HOSTS = ["mysql.corp.internal", "MySQL.Corp.Internal", "db2-prod", "a",
               "zk1.corp.internal", "10x.example.com"]
INVALID_HOSTS = ["", " ", "has space", ".leading", "trailing.", "-lead", "trail-",
                 "a..b", "a" * 64, "10.129.12.75", "-", "my_host",
                 ".".join(["a" * 60] * 5)]

HOSTNAME_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { Write-Host "PARSE_ERROR: $($errors[0].Message)"; exit 2 }
foreach ($n in @('Info','Warn','Die','Test-AddrFormat','Test-HostName','Split-AddrInput')) {
  $fs = $ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true)
  if (-not $fs) { Write-Host "MISSING_FUNC: $n"; exit 3 }
  Invoke-Expression $fs[0].Extent.Text
}
$cases = @($env:FAKE_HOST_CASES | ConvertFrom-Json)
# 用「数组 of (case, result)」而不是 hashtable：PowerShell 的 hashtable 键
# 大小写不敏感，"mysql.corp.internal" 与 "MySQL.Corp.Internal" 会互相覆盖，
# 导致合法/非法用例里的同名不同大小写条目丢失。
$res = @()
foreach ($c in $cases) { $res += ,@{ c = "$c"; r = [bool](Test-HostName "$c") } }
Write-Host ("RESULT=" + ($res | ConvertTo-Json -Compress -AsArray))
exit 0
"""

SPLIT_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { Write-Host "PARSE_ERROR: $($errors[0].Message)"; exit 2 }
foreach ($n in @('Info','Warn','Die','Test-AddrFormat','Test-HostName','Split-AddrInput')) {
  $fs = $ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true)
  if (-not $fs) { Write-Host "MISSING_FUNC: $n"; exit 3 }
  Invoke-Expression $fs[0].Extent.Text
}
$pairs = @($env:FAKE_SPLIT_CASES | ConvertFrom-Json)
$out = @()
foreach ($p in $pairs) {
  $sp = Split-AddrInput $p[0] $p[1]
  $out += ,@{ addr = "$($sp.addr)"; host = "$($sp.host)" }
}
Write-Host ("RESULT=" + ($out | ConvertTo-Json -Compress -AsArray))
exit 0
"""

SPLIT_DIE_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { Write-Host "PARSE_ERROR: $($errors[0].Message)"; exit 2 }
foreach ($n in @('Info','Warn','Die','Test-AddrFormat','Test-HostName','Split-AddrInput')) {
  $fs = $ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true)
  if (-not $fs) { Write-Host "MISSING_FUNC: $n"; exit 3 }
  Invoke-Expression $fs[0].Extent.Text
}
$sp = Split-AddrInput $env:FAKE_SPLIT_RAW $env:FAKE_SPLIT_OLD
Write-Host "UNREACHABLE addr=$($sp.addr) host=$($sp.host)"
exit 0
"""

DNAT_MAP_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { Write-Host "PARSE_ERROR: $($errors[0].Message)"; exit 2 }
foreach ($n in @('Info','Warn','Die','Test-SvcName','Test-AddrFormat','Test-IPv4','Test-HostName','New-DnatMapText','Write-DnatMapFile')) {
  $fs = $ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true)
  if (-not $fs) { Write-Host "MISSING_FUNC: $n"; exit 3 }
  Invoke-Expression $fs[0].Extent.Text
}
# 按 relay.json 的落盘形态构造服务对象：无 host 键时对象上根本没有该属性
$svcs = @($env:FAKE_MAP_SERVICES | ConvertFrom-Json | ForEach-Object {
  $o = [ordered]@{
    name = [string]$_.name; remoteHost = [string]$_.remoteHost
    remotePort = [int]$_.remotePort; hostPort = [int]$_.hostPort; vmPort = [int]$_.vmPort
  }
  if ($_.PSObject.Properties['host']) { $o['host'] = [string]$_.host }
  [pscustomobject]$o
})
$text = New-DnatMapText $svcs $env:FAKE_MAP_GW
Write-DnatMapFile $text $env:FAKE_MAP_OUT
Write-Host 'MAP_OK'
exit 0
"""


def _hostname_results(tmp_path, cases):
    r = _run_harness(HOSTNAME_HARNESS, tmp_path, env_extra={"FAKE_HOST_CASES": json.dumps(cases)})
    assert r.returncode == 0, r.stdout + r.stderr
    lines = [l for l in r.stdout.splitlines() if l.startswith("RESULT=")]
    assert lines, r.stdout
    pairs = json.loads(lines[0][len("RESULT="):])
    return {p["c"]: p["r"] for p in pairs}


def _collect_domains(stdout):
    out = {}
    for line in stdout.splitlines():
        if line.startswith("DOM "):
            _, name, dom = line.strip().split(" ", 2)
            out[name] = dom
    return out


def test_relay_ps1_addr_question_has_domain_slot():
    """地址问句扩展：提示语带 "[ 域名]"，且不再出现"域名暂不支持"的旧说法。"""
    text = RELAY_PS1.read_text(encoding="utf-8")
    assert "域名暂不支持" not in text, "地址问句仍宣称域名不支持（与同问句录域名矛盾）"
    assert text.count("[ 域名]") >= 3, "至少 3 个问答入口（修改/自定义新增/目录新增）须带 [ 域名]"
    for fn in ("Test-HostName", "Split-AddrInput", "New-DnatMapText", "Write-DnatMapFile"):
        assert f"function {fn}" in text, f"relay.ps1 缺函数 {fn}"


def test_relay_ps1_dnat_map_distribution_pins():
    """映射表：内存 $services + $gw 渲染（不读 relay.json），先落 stack/data 再下发 VM。"""
    ps1 = RELAY_PS1.read_text(encoding="utf-8")
    setup = ps1.split("function Invoke-Setup", 1)[1].split("# ---- 配置清单", 1)[0]
    assert "New-DnatMapText $services $gw" in setup, \
        "映射表须由内存中的 $services + $gw 渲染（此时 relay.json 尚未落盘）"
    assert "data\\relay-map.env" in setup, "映射表须落 <安装目录>\\stack\\data\\relay-map.env"
    assert "cadence-relay-dnat.env" in setup, "映射表须下发 VM /etc/cadence-relay-dnat.env"
    assert "Move-Item -Force" in ps1, "映射表须原子落盘（先 .tmp 再 Move-Item -Force）"
    assert "-Encoding ascii" in ps1, "映射表须无 BOM（沿用 -Encoding ascii）"
    # 锚点须用实际执行的 setup 命令（$setupCmd 赋值行），不能用子串
    # "relay-vm.sh setup"——它在函数头注释里也出现过，会命中更早的位置。
    assert setup.index("relay-map.env") < setup.index("$setupCmd = "), \
        "映射表须在 VM setup 之前就位（setup 末尾 dnat_apply 消费它）"
    assert setup.index("New-DnatMapText") < setup.index("ConvertTo-Json"), \
        "映射表生成须早于 relay.json 落盘（数据源为内存 $services）"
    assert "Undo-Portproxy" in setup, "映射表下发失败须回滚 portproxy（不留半套）"


@needs_pwsh
def test_host_name_accepts_valid_domains(tmp_path):
    """合法域名：多标签/单标签/数字开头/大写（Test-HostName 不做大小写归一化）。"""
    got = _hostname_results(tmp_path, VALID_HOSTS)
    assert got == {h: True for h in VALID_HOSTS}, got


@needs_pwsh
def test_host_name_rejects_invalid_and_ip_literal(tmp_path):
    """拒绝面：空串/空白/首尾点或横杠/连续点/单标签 >63/总长 >253/IP 字面量/-/_。"""
    got = _hostname_results(tmp_path, INVALID_HOSTS)
    for h in INVALID_HOSTS:
        assert got[h] is False, f"应被拒绝却放行：{h!r}"


SPLIT_CASES = [
    # raw, oldHost → (addr, host)
    ("10.1.1.1:3306", "mysql.corp.internal", "10.1.1.1:3306", "mysql.corp.internal"),
    ("10.1.1.1:3306", "", "10.1.1.1:3306", ""),
    ("10.2.2.2:3306 new.corp.internal", "old.corp.internal",
     "10.2.2.2:3306", "new.corp.internal"),
    ("  10.2.2.2:3306   New.Corp.Internal  ", "", "10.2.2.2:3306", "new.corp.internal"),
    ("10.1.1.1:3306 -", "old.corp.internal", "10.1.1.1:3306", ""),
    ("10.1.1.1:3306 none", "old.corp.internal", "10.1.1.1:3306", ""),
    ("10.1.1.1:3306 NONE", "old.corp.internal", "10.1.1.1:3306", ""),
]


@needs_pwsh
def test_split_addr_input_parses_domain_and_keeps_old(tmp_path):
    """IP:端口[ 域名]：域名可选、Trim、小写归一化；缺域名沿用旧值；-/none 清空。"""
    pairs = [[c[0], c[1]] for c in SPLIT_CASES]
    r = _run_harness(SPLIT_HARNESS, tmp_path, env_extra={"FAKE_SPLIT_CASES": json.dumps(pairs)})
    assert r.returncode == 0, r.stdout + r.stderr
    lines = [l for l in r.stdout.splitlines() if l.startswith("RESULT=")]
    got = json.loads(lines[0][len("RESULT="):])
    assert [(g["addr"], g["host"]) for g in got] == [(c[2], c[3]) for c in SPLIT_CASES], got


@needs_pwsh
@pytest.mark.parametrize("raw,old,msg", [
    ("", "old.corp.internal", "地址格式"),
    ("10.1.1.1", "", "地址格式"),
    ("10.1.1.1:3306 bad..host", "", "域名格式不合法"),
    ("10.1.1.1:3306 10.2.2.2", "", "域名格式不合法"),
])
def test_split_addr_input_rejects_bad_input(tmp_path, raw, old, msg):
    """非法输入：地址段走 Test-AddrFormat，域名段走 Test-HostName，均显式报错。"""
    r = _run_harness(SPLIT_DIE_HARNESS, tmp_path,
                     env_extra={"FAKE_SPLIT_RAW": raw, "FAKE_SPLIT_OLD": old})
    assert r.returncode != 0, r.stdout + r.stderr
    assert msg in r.stdout, r.stdout
    assert "UNREACHABLE" not in r.stdout, "非法输入不得返回结果"


@needs_pwsh
def test_collect_edit_records_domain_same_question(tmp_path):
    """修改分支：地址问句同时录入域名 → host 贯通到服务对象。"""
    cfg = {"version": 1, "services": [_svc("mysql", "10.1.1.1", 3306, 13306, 23306)]}
    r = _run_collect(tmp_path, ["", "1", "10.2.2.2:3306 mysql.corp.internal", "", ""], relay_json=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _collect_result_lines(r.stdout) == [
        "SVC mysql 10.2.2.2:3306 host=13306 vm=23306",
    ], r.stdout
    assert _collect_domains(r.stdout) == {"mysql": "mysql.corp.internal"}, r.stdout
    assert "ANSWER[10.2.2.2:3306 mysql.corp.internal]: 新远端地址（IP:端口[ 域名]" in r.stdout, r.stdout


@needs_pwsh
def test_collect_edit_empty_reply_keeps_domain(tmp_path):
    """修改分支回车：地址与域名都保持原值（不产生第二次询问）。"""
    cfg = {"version": 1, "services": [
        _svc("mysql", "10.1.1.1", 3306, 13306, 23306, domain="mysql.corp.internal"),
    ]}
    r = _run_collect(tmp_path, ["", "1", "", "", ""], relay_json=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _collect_result_lines(r.stdout) == [
        "SVC mysql 10.1.1.1:3306 host=13306 vm=23306",
    ], r.stdout
    assert _collect_domains(r.stdout) == {"mysql": "mysql.corp.internal"}, r.stdout


@needs_pwsh
@pytest.mark.parametrize("reply", ["-", "none"])
def test_collect_edit_clears_domain(tmp_path, reply):
    """清空语义：域名段输入 "-" 或 none → 已录域名被清除（否则永远删不掉）。"""
    cfg = {"version": 1, "services": [
        _svc("mysql", "10.1.1.1", 3306, 13306, 23306, domain="mysql.corp.internal"),
    ]}
    r = _run_collect(tmp_path, ["", "1", f"10.1.1.1:3306 {reply}", "", ""], relay_json=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _collect_domains(r.stdout) == {"mysql": "-"}, r.stdout


@needs_pwsh
def test_collect_param_override_keeps_old_domain(tmp_path):
    """-MySQL 覆盖同名服务：参数是纯 IP:端口，但旧域名不得丢。"""
    cfg = {"version": 1, "services": [
        _svc("mysql", "10.1.1.1", 3306, 13306, 23306, domain="mysql.corp.internal"),
    ]}
    r = _run_collect(tmp_path, ["", "", ""], relay_json=cfg, mysql="10.9.9.9:3306")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _collect_result_lines(r.stdout) == [
        "SVC mysql 10.9.9.9:3306 host=13306 vm=23306",
    ], r.stdout
    assert _collect_domains(r.stdout) == {"mysql": "mysql.corp.internal"}, r.stdout


@needs_pwsh
def test_collect_custom_and_catalog_record_domain(tmp_path):
    """自定义/目录新增：同一问句录入域名并写入服务对象。

    问答次序：「新增」是一次问句、逗号分隔多项（custom,kafka），
    随后按项追问——custom 先问服务名再问地址，目录项直接问地址。
    """
    r = _run_collect(tmp_path, [
        "custom,kafka",
        "zk1", "10.1.1.5:2181 zk1.corp.internal",
        "10.1.1.6:9092 Kafka.Corp.Internal",
        "",
    ])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _collect_domains(r.stdout) == {
        "zk1": "zk1.corp.internal",
        "kafka": "kafka.corp.internal",
    }, r.stdout


@needs_pwsh
def test_collect_legacy_json_without_host_key(tmp_path):
    """向后兼容：旧 relay.json 无 host 键 → 正常读取，域名视为未录入。"""
    cfg = {"version": 1, "services": [
        {"name": "mysql", "remoteHost": "10.1.1.1", "remotePort": 3306,
         "hostPort": 13306, "vmPort": 23306},
    ]}
    r = _run_collect(tmp_path, ["", "", ""], relay_json=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _collect_result_lines(r.stdout) == [
        "SVC mysql 10.1.1.1:3306 host=13306 vm=23306",
    ], r.stdout
    assert _collect_domains(r.stdout) == {"mysql": "-"}, r.stdout


@needs_pwsh
def test_collect_invalid_domain_dies(tmp_path):
    """问答里录入非法域名：显式报错，不产出任何结果。"""
    r = _run_collect(tmp_path, ["custom", "zk1", "10.1.1.5:2181 bad..host"])
    assert r.returncode != 0, r.stdout + r.stderr
    assert "域名格式不合法" in r.stdout, r.stdout
    assert "RESULT-BEGIN" not in r.stdout


def _run_dnat_map(tmp_path, services, gw="10.89.0.1"):
    out = tmp_path / "stack" / "data" / "relay-map.env"
    env = {
        "FAKE_MAP_SERVICES": json.dumps(services),
        "FAKE_MAP_GW": gw,
        "FAKE_MAP_OUT": str(out),
    }
    return _run_harness(DNAT_MAP_HARNESS, tmp_path, env_extra=env), out


MAP_SERVICES = [
    {"name": "mysql", "remoteHost": "10.129.12.75", "remotePort": 3306,
     "hostPort": 13306, "vmPort": 23306, "host": "MySQL.Corp.Internal"},
    {"name": "redis", "remoteHost": "10.129.20.31", "remotePort": 6379,
     "hostPort": 16379, "vmPort": 26379},
    {"name": "zk1", "remoteHost": "10.129.30.11", "remotePort": 2181,
     "hostPort": 12181, "vmPort": 22181, "host": "-"},
]
MAP_EXPECT = [
    ["mysql", "10.129.12.75", "3306", "10.89.0.1", "23306", "mysql.corp.internal"],
    ["redis", "10.129.20.31", "6379", "10.89.0.1", "26379", "-"],
    ["zk1", "10.129.30.11", "2181", "10.89.0.1", "22181", "-"],
]


@needs_pwsh
def test_dnat_map_content_order_and_lf_no_bom(tmp_path):
    """映射表：段数/顺序/域名小写/'-' 占位/无 BOM/纯 LF/原子落盘不留 .tmp。"""
    r, out = _run_dnat_map(tmp_path, MAP_SERVICES)
    assert r.returncode == 0 and "MAP_OK" in r.stdout, r.stdout + r.stderr
    raw = out.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "映射表不得带 BOM"
    assert b"\r" not in raw, "映射表须纯 LF"
    assert raw.endswith(b"\n"), "映射表须以 LF 结尾"
    lines = raw.decode("utf-8").splitlines()
    # 表头是 ASCII：映射表用 -Encoding ascii 落盘（无 BOM 的既有做法，
    # 仓库同族的 relay 服务表也用 ASCII 表头 "# name vmport bindaddr hostport"），
    # 中文表头在 WinPS 5.1 下会被 ascii 编码成 "?"。
    assert lines[0].startswith("# generated by relay.ps1"), lines[0]
    assert lines[1] == "# <name> <real_ip> <real_port> <target_ip> <target_port> [domain]", lines[1]
    data = [l.split(" ") for l in lines if not l.startswith("#")]
    assert data == MAP_EXPECT, data
    assert not Path(f"{out}.tmp").exists(), "原子落盘后不得残留 .tmp"


@needs_pwsh
def test_dnat_map_idempotent(tmp_path):
    """幂等：同一输入重跑生成字节级一致（不追加、不去重表）。"""
    r1, out = _run_dnat_map(tmp_path, MAP_SERVICES)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    first = out.read_bytes()
    r2, _ = _run_dnat_map(tmp_path, MAP_SERVICES)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert out.read_bytes() == first


@needs_pwsh
@pytest.mark.parametrize("services,msg", [
    ([{"name": "a1", "remoteHost": "10.1.1.1", "remotePort": 3306, "hostPort": 13306, "vmPort": 23306},
      {"name": "a2", "remoteHost": "10.1.1.1", "remotePort": 3306, "hostPort": 13307, "vmPort": 23307}],
     "(real_ip, real_port) 重复"),
    ([{"name": "bad name", "remoteHost": "10.1.1.1", "remotePort": 3306,
       "hostPort": 13306, "vmPort": 23306}], "服务名非法"),
    ([{"name": "mysql", "remoteHost": "10.1.1.999", "remotePort": 3306,
       "hostPort": 13306, "vmPort": 23306}], "真实 IP 非法"),
    ([{"name": "mysql", "remoteHost": "10.1.1.1", "remotePort": 0,
       "hostPort": 13306, "vmPort": 23306}], "real_port"),
    ([{"name": "mysql", "remoteHost": "10.1.1.1", "remotePort": 3306,
       "hostPort": 13306, "vmPort": 70000}], "target_port"),
    ([{"name": "mysql", "remoteHost": "10.1.1.1", "remotePort": 3306,
       "hostPort": 13306, "vmPort": 23306, "host": "bad..host"}], "域名非法"),
])
def test_dnat_map_rejects_invalid_generation(tmp_path, services, msg):
    """生成端校验：name/IP/端口范围/(ip,port) 唯一/域名 —— 任一不满足即显式报错。"""
    r, out = _run_dnat_map(tmp_path, services)
    assert r.returncode != 0, r.stdout + r.stderr
    assert msg in r.stdout, r.stdout
    assert "MAP_OK" not in r.stdout
    assert not out.exists(), "校验失败不得落盘半套映射表"


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
