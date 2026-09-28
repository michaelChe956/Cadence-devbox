# -*- coding: utf-8 -*-
"""devbox/compose.yaml 与 stack/catalog 的结构化契约校验。"""
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "devbox" / "compose.yaml"
CATALOG = REPO / "devbox" / "stack" / "catalog"
MW = ("mysql", "redis", "rabbitmq", "minio")


def test_yaml_parses_and_services_is_last_top_key():
    text = COMPOSE.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    assert list(doc.keys())[-1] == "services"
    assert doc["name"] == "cadence"


def test_all_ports_bind_loopback_only():
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    for svc, spec in doc["services"].items():
        for port in spec.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{svc} 端口未仅绑 127.0.0.1：{port}"


def test_all_volumes_are_external():
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert doc["volumes"], "缺少顶层卷声明"
    for name, spec in doc["volumes"].items():
        assert spec.get("external") is True, f"卷 {name} 未声明 external:true"


def test_profiles_matrix():
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert doc["services"]["rabbitmq"]["profiles"] == ["mq"]
    assert doc["services"]["minio"]["profiles"] == ["storage"]
    for service in ("mysql", "redis", "devbox"):
        assert "profiles" not in doc["services"][service], f"{service} 不应挂 profile"


def test_image_pins_and_devbox_mounts():
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = doc["services"]
    assert services["mysql"]["image"].startswith("mysql:8.4")
    assert services["redis"]["image"].startswith("redis:7")
    assert services["rabbitmq"]["image"].startswith("rabbitmq:3")
    assert services["minio"]["image"].startswith("minio/minio:RELEASE.")
    assert "${CADENCE_DEVBOX_IMAGE:" in services["devbox"]["image"]

    volumes = services["devbox"]["volumes"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in volumes
    assert ".:/cadence/stack" in volumes
    assert "../cadence-box.yaml:/cadence/auth.yaml:ro" in volumes
    assert any(
        isinstance(v, dict)
        and v.get("type") == "bind"
        and str(v.get("source", "")).startswith("${CADENCE_WORKSPACE")
        and v.get("target") == "/workspace"
        for v in volumes
    ), "workspace 挂载必须是长语法：podman-compose 短语法按':'切分，Windows 盘符路径 D:/x 会被切坏（unknown mount option）"
    for mount in (
        "cadence-claude:/home/dev/.claude", "cadence-codex:/home/dev/.codex",
        "cadence-pi:/home/dev/.pi/agent", "cadence-kimi:/home/dev/.kimi-code",
        "cadence-agents:/home/dev/.agents", "cadence-omp:/home/dev/.omp",
        "cadence-m2:/home/dev/.m2", "cadence-npm:/home/dev/.npm",
        "cadence-npm-global:/home/dev/.npm-global", "cadence-uv:/home/dev/.cache/uv",
        "cadence-pip:/home/dev/.cache/pip", "cadence-gradle:/home/dev/.gradle",
        "cadence-jdks:/home/dev/.cadence/jdks",
    ):
        assert mount in volumes, f"devbox 缺挂载 {mount}"
    for service in MW:
        assert any(volume.startswith(f"cadence-{service}-data:") for volume in services[service]["volumes"])


def test_middleware_healthchecks_present():
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    for service in MW:
        assert "healthcheck" in doc["services"][service], f"{service} 缺 healthcheck"


def test_catalog_layout_and_parseable_fragments():
    profiles = {"mysql": "default", "redis": "default", "rabbitmq": "mq", "minio": "storage"}
    for service in MW:
        directory = CATALOG / service
        assert (directory / "profile").read_text(encoding="utf-8").strip() == profiles[service]
        conn = (directory / "conn.txt").read_text(encoding="utf-8")
        assert f"{service}:" in conn.replace("://", "#")
        fragment = (directory / "service.fragment.yaml").read_text(encoding="utf-8")
        assert fragment.splitlines()[0].startswith(f"  {service}:")
        yaml.safe_load("services:\n" + fragment)
    assert "[mysqld]" in (CATALOG / "mysql" / "conf" / "my.cnf").read_text(encoding="utf-8")
    assert "appendonly" in (CATALOG / "redis" / "conf" / "redis.conf").read_text(encoding="utf-8")


def test_no_sock_override_replaces_volumes_without_socket():
    text = (REPO / "devbox" / "stack" / "docker-compose.no-sock.yml").read_text(encoding="utf-8")
    assert "/var/run/docker.sock" not in text
    assert "!override" in text
    assert "type: bind" in text and "target: /workspace" in text, "no-sock 的 workspace 挂载须同为长语法（Windows 盘符兼容）"
    assert "cadence-claude:/home/dev/.claude" in text
    assert "cadence-jdks:/home/dev/.cadence/jdks" in text, "no-sock 覆盖须同样挂载 JDK 状态卷"


def test_devbox_volume_mountpoints_precreated_in_image_as_dev():
    """回归：devbox 命名卷的每个挂载点都必须在 Dockerfile 预建且属 dev——
    podman 首挂空卷时卷根继承镜像内目录属主；目录缺失则由 runtime 以 root 建点，dev(1000) 无写权限
    （2026-09-28 Windows 实测 8 个卷 root 属主 → entrypoint 渲染/回填 PermissionError 拒启）。"""
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    targets = [item.split(":", 1)[1] for item in doc["services"]["devbox"]["volumes"]
               if isinstance(item, str) and item.startswith("cadence-")]
    assert targets, "devbox 服务应挂命名卷"
    text = (REPO / "devbox" / "Dockerfile").read_text(encoding="utf-8")
    logical = re.sub(r"\\\n\s*", " ", text)  # 合并 Dockerfile 续行后再取 install -d 行
    install_line = next(l for l in logical.splitlines()
                        if not l.lstrip().startswith("#") and "install -d" in l and "/home/dev/.cadence" in l)
    for target in targets:
        assert target in install_line, f"挂载点 {target} 未在 Dockerfile 预建为 dev 属主"
