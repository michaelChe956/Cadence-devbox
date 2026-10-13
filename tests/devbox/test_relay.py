# -*- coding: utf-8 -*-
"""relay 一键通道脚本的契约校验（relay.ps1 / relay-vm.sh / 文档集成）。

除源码结构契约外，用 pwsh 执行从 relay.ps1 AST 抽取的真实函数做行为回归
（守卫的平台/运行状态判定、原生错误显式传播、Die 退出码契约）。
"""
import os
import shutil
import subprocess
import sys
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
    assert "TcpClient" in text, "缺远端预检 TcpClient"


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


def test_relay_vm_sh_retry_and_override():
    text = RELAY_VM.read_text(encoding="utf-8")
    assert "RELAY_ENV_FILE" in text, "env 路径须可覆盖（可测性）"
    assert "while :" in text, "run 脚本须带重试循环（网桥未就绪自愈）"


def test_install_ps1_gitignore_covers_relay_json():
    text = INSTALL_PS1.read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if "stack/data/" in l]
    assert lines and "relay.json" in lines[0], "安装器写出的 .gitignore 未含 relay.json"


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
