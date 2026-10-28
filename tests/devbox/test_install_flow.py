# -*- coding: utf-8 -*-
"""devbox 容器唯一性与命名契约（compose / 安装器 / 手册）。

结构测试：无 Windows 与 pwsh 依赖——读 compose 与脚本文本断言契约。
行为层（容器真的建起来、探针真的打到在跑的容器）由 Windows 真机验证。
"""
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "devbox" / "compose.yaml"
NO_SOCK = REPO / "devbox" / "stack" / "docker-compose.no-sock.yml"
INSTALL_PS1 = REPO / "devbox" / "install.ps1"
README = REPO / "devbox" / "README.md"

CONTAINER_NAME = "cadence_devbox"
LEGACY_NAMES = ("devbox", "cadence_devbox_1")
# 手册里对旧容器的命令式用法（`podman exec -it devbox` 等）——固定名后必须清零
LEGACY_CMD = re.compile(r"podman\s+(?:exec|logs|start|stop|restart|rm)\s+(?:-\S+\s+)*devbox\b")


def test_compose_devbox_service_uses_fixed_container_name():
    """compose 的 devbox 服务必须固定容器名——不依赖 <项目>_<服务>_1 默认形式。"""
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    got = doc["services"]["devbox"].get("container_name")
    assert got == CONTAINER_NAME, f"devbox 服务容器名应为 {CONTAINER_NAME}，实际 {got!r}"


def test_no_sock_override_inherits_container_name():
    """no-sock 覆盖文件只该覆盖 volumes——容器名必须继承 compose.yaml（否则会变回默认名）。"""
    text = NO_SOCK.read_text(encoding="utf-8")
    assert "container_name" not in text, "no-sock 覆盖文件不得自带 container_name"


def _code_lines(text):
    """去掉注释行——断言只针对可执行代码，注释里提到旧命令不算违规。"""
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def test_install_does_not_create_devbox_container_by_hand():
    """安装器不得手工 run 容器——那正是「两个 devbox」的来源。"""
    code = _code_lines(INSTALL_PS1.read_text(encoding="utf-8"))
    assert "--name devbox" not in code, "安装器不得手工创建名为 devbox 的容器"
    assert not re.search(r"podman\s+run\s+-d", code), "安装器不得再手工 podman run 容器"


def test_install_starts_devbox_through_compose():
    """devbox 容器必须经 compose 创建（与中间件同一套定义、同一批状态卷）。"""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert re.search(r"podman-compose[^\n]*up\s+-d\s+devbox", text), \
        "安装器须经 podman-compose up -d devbox 创建 devbox 容器"


def test_install_cleans_legacy_devbox_container_names():
    """历史遗留容器名必须被清理——否则重跑安装器会留下第二个挂载同一 /workspace 的容器。"""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    for legacy in LEGACY_NAMES:
        assert f"'{legacy}'" in text, f"安装器须清理历史遗留容器名 {legacy}"


def test_install_asserts_single_devbox_container():
    """安装器须在启动后断言 devbox 类容器至多一个。"""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert re.search(r"Count\s*-gt\s*1", text), "安装器须断言 devbox 容器至多一个（Count -gt 1）"
    assert CONTAINER_NAME in text, f"安装器须使用固定容器名 {CONTAINER_NAME}"


def test_install_tips_use_actual_container_name():
    """收尾提示须用实际容器名变量——写死旧名会把用户带回老路。"""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    tips = text.split("第 5/5 步", 1)[-1]
    assert "$DevboxCtr" in tips, "安装器收尾提示须用 $DevboxCtr（compose 不支持 container_name 时名字会不同）"
    assert not re.search(r"podman\s+(?:exec|logs|start|stop|restart|rm)\s+(?:-\S+\s+)*devbox\b", tips), \
        "收尾提示不得再按旧容器名给命令"


def test_readme_uses_fixed_container_name_only():
    """手册里的容器命令一律指向固定名——旧名残留会把用户带回老路。"""
    text = README.read_text(encoding="utf-8")
    assert "cadence_devbox_1" not in text, "手册不得再出现 compose 默认名 cadence_devbox_1"
    hits = LEGACY_CMD.findall(text)
    assert not hits, f"手册不得再按旧容器名操作：{hits}"
    assert CONTAINER_NAME in text, f"手册须使用固定容器名 {CONTAINER_NAME}"