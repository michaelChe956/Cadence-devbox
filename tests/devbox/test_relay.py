# -*- coding: utf-8 -*-
"""relay 一键通道脚本的结构化契约校验（relay.ps1 / relay-vm.sh / 文档集成）。"""
import subprocess
from pathlib import Path

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
