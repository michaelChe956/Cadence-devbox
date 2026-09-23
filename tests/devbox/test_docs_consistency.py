# -*- coding: utf-8 -*-
"""安装文档与实现的接线一致性：README 手工步骤 / Windows 安装器 / no-sock 覆盖与 compose 卷集合必须一致，
README 记录的 JDK 8 发行物（版本、SHA-256）必须与 devbox/jdk.sh 的清单逐字一致。

为什么值得测：external 卷 compose 不会自建——文档或安装器少写一个卷，用户就按文档走不动；
README 里的哈希若与实现漂移，用户会照着一个不存在的信任锚去核对。
"""
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
README = REPO / "devbox" / "README.md"
COMPOSE = REPO / "devbox" / "compose.yaml"
NO_SOCK = REPO / "devbox" / "stack" / "docker-compose.no-sock.yml"
INSTALL_PS1 = REPO / "devbox" / "install.ps1"
JDK_SH = REPO / "devbox" / "jdk.sh"

VOLUME_RE = re.compile(r"cadence-[a-z0-9-]+")


def compose_volumes() -> set:
    return set(yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["volumes"])


def compose_devbox_named_volumes() -> set:
    """compose 里 devbox 服务挂的命名卷（含路径形态 `name:/path`）。"""
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    names = set()
    for item in doc["services"]["devbox"]["volumes"]:
        if isinstance(item, str) and item.startswith("cadence-"):
            names.add(item.split(":", 1)[0])
    return names


# ---------- 卷集合：compose 是唯一事实来源 ----------

def test_readme_manual_volume_lists_match_compose():
    """README §2.2 第 4 步的两条手工命令（PowerShell 与 bash）都必须创建 compose 声明的全部卷。"""
    lines = [ln for ln in README.read_text(encoding="utf-8").splitlines()
             if "podman volume create" in ln and "cadence-claude" in ln]
    assert len(lines) == 2, f"应恰好有 PowerShell 与 bash 两条逐卷建卷命令，实际 {len(lines)} 条"
    want = compose_volumes()
    for ln in lines:
        got = set(VOLUME_RE.findall(ln))
        assert got == want, f"建卷命令与 compose 卷不一致：缺 {sorted(want - got)}；多 {sorted(got - want)}"


def test_readme_stated_volume_count_matches_compose():
    text = README.read_text(encoding="utf-8")
    count = len(compose_volumes())
    for phrase in (f"{count} 个卷", f"{count} 个数据卷"):
        assert phrase in text, f"README 卷数量描述过期：找不到「{phrase}」"


def test_windows_installer_precreates_exactly_compose_volumes():
    """install.ps1 的 $vols 必须与 compose 卷集合完全一致（Windows 路线全靠它预建）。"""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    block = re.search(r"\$vols\s*=\s*@\((?P<body>.*?)\)", text, re.S)
    assert block, "install.ps1 未找到 $vols 定义"
    got = set(re.findall(r"'([^']+)'", block.group("body")))
    want = compose_volumes()
    assert got == want, f"$vols 与 compose 卷不一致：缺 {sorted(want - got)}；多 {sorted(got - want)}"


def test_no_sock_override_mounts_same_named_volumes_without_socket():
    """no-sock 用 !override 整体替换 volumes：集合必须与 compose 的 devbox 命名卷一致，且不含 sock。"""
    text = NO_SOCK.read_text(encoding="utf-8")
    assert "!override" in text, "no-sock 覆盖必须用 !override 整体替换，否则 compose 会追加合并"
    mounted = set(re.findall(r"^\s*-\s*(cadence-[a-z0-9-]+):", text, re.M))
    want = compose_devbox_named_volumes()
    assert mounted == want, f"no-sock 挂载集合与 compose 不一致：缺 {sorted(want - mounted)}；多 {sorted(mounted - want)}"
    assert "docker.sock" not in text, "no-sock 覆盖不得保留运维 sock"


# ---------- README 记录的 JDK 8 发行物必须与清单逐字一致 ----------

def jdk8_manifest() -> dict:
    text = JDK_SH.read_text(encoding="utf-8")

    def value(key):
        found = re.search(rf'^{key}="([^"]*)"', text, re.M)
        assert found, f"jdk.sh 缺少清单项 {key}"
        return found.group(1)

    return {key: value(key) for key in
            ("JDK8_PACKAGE", "JDK8_VERSION", "JDK8_ARCH", "JDK8_SHA256", "JDK8_URL")}


def rendered_jdk8_url() -> str:
    """jdk.sh 里 URL 是模板（${JDK8_PACKAGE} 等），按清单渲染成用户实际会访问的地址。"""
    manifest = jdk8_manifest()
    url = manifest["JDK8_URL"]
    for key in ("JDK8_PACKAGE", "JDK8_VERSION", "JDK8_ARCH"):
        url = url.replace("${%s}" % key, manifest[key])
    return url


def test_readme_documents_shipped_jdk8_artifact():
    """README 写出的包名/版本/哈希/来源必须是实现里实际使用的那一份（避免文档给出不存在的信任锚）。"""
    manifest = jdk8_manifest()
    text = README.read_text(encoding="utf-8")
    for key in ("JDK8_PACKAGE", "JDK8_VERSION", "JDK8_SHA256"):
        assert manifest[key] in text, f"README 未记录 {key}={manifest[key]}（与 jdk.sh 清单漂移）"
    assert rendered_jdk8_url() in text, f"README 未记录实际下载地址 {rendered_jdk8_url()}"


def test_readme_documents_source_override_and_commands():
    text = README.read_text(encoding="utf-8")
    for needed in ("CADENCE_JDK8_URL", "CADENCE_JDK8_SHA256",
                   "jdk list", "jdk use 8", "jdk use 21",
                   "cadence-jdks:/home/dev/.cadence/jdks"):
        assert needed in text, f"README 缺少用户可见的 {needed}"
    assert "成对" in text, "README 必须说明来源与校验值成对配置（否则用户会以为能只换源）"
