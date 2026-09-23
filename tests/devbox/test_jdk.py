# -*- coding: utf-8 -*-
"""devbox/jdk.sh 行为契约：持久状态初始化（空卷→内置 21、已有选择保留、损坏报错不重置）、
current 原子切换、jdk list / jdk use；以及 jdk use 8 的按需下载（包类型/身份/独立哈希校验、
安全解包、真实 java/javac 版本检查、原子发布）与全部失败路径保持原选择。
镜像层与 Windows 安装器的接线（稳定 JAVA_HOME/PATH、卷预建与显式挂载）也在此覆盖。
测试用替身 JDK 目录与本机伪造的 Debian 包（不依赖 dpkg-deb，也不依赖网络）。"""
import hashlib
import io
import os
import subprocess
import tarfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
JDK = REPO / "devbox" / "jdk.sh"
SHIM = REPO / "devbox" / "jdk-shim.sh"
DOCKERFILE = REPO / "devbox" / "Dockerfile"
INSTALL_PS1 = REPO / "devbox" / "install.ps1"

BUILTIN_MAJOR = "21"

# jdk use 8 的可信发行物（Plan Task 1 实测；与 devbox/jdk.sh 清单保持一致）
JDK8_PACKAGE = "temurin-8-jdk"
JDK8_VERSION = "8.0.504.0.0+1-0"
JDK8_DIR_IN_PKG = "usr/lib/jvm/temurin-8-jdk-amd64"
# 单元测试不触网：默认来源与校验值必然失败；正例由用例显式覆盖为本地伪造包
NO_SOURCE_URL = "file:///nonexistent/cadence-jdk8.deb"
NO_SOURCE_SHA256 = "0" * 64


def make_jdk(path, java_version="21.0.12", javac_version=None, with_javac=True):
    """造最小可校验 JDK 目录：bin/java 版本走 stderr（与真实 java 一致），bin/javac 走 stdout。"""
    javac_version = javac_version or java_version
    bin_dir = path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    java = bin_dir / "java"
    java.write_text(
        f'#!/usr/bin/env bash\nprintf \'openjdk version "{java_version}" 2026-01-01\\n\' >&2\n',
        encoding="utf-8",
    )
    java.chmod(0o755)
    if with_javac:
        javac = bin_dir / "javac"
        javac.write_text(
            f"#!/usr/bin/env bash\nprintf 'javac {javac_version}\\n'\n", encoding="utf-8"
        )
        javac.chmod(0o755)
    return path


def env_for(root, builtin, **extra):
    env = {**os.environ,
           "JAVA_HOME": str(root / "current"),
           "CADENCE_JDK_ROOT": str(root),
           "CADENCE_BUILTIN_JDK_DIR": str(builtin),
           "CADENCE_BUILTIN_JDK_MAJOR": BUILTIN_MAJOR,
           # 单元测试一律不触网：默认来源指向必然失败的本地路径，正例由用例显式覆盖
           "CADENCE_JDK8_URL": NO_SOURCE_URL,
           "CADENCE_JDK8_SHA256": NO_SOURCE_SHA256}
    env.update(extra)
    return env


def jdk_run(env, *args):
    return subprocess.run(["bash", str(JDK), *args], env=env,
                          capture_output=True, text=True, timeout=60)


def current_target(root):
    link = root / "current"
    return os.readlink(link) if link.is_symlink() else None


def state(root, tmp_path, installed8=True):
    """返回 (JDK_ROOT, 内置 21 目录, 可选已安装 8 目录)。"""
    root.mkdir(parents=True, exist_ok=True)
    builtin = make_jdk(tmp_path / "jvm" / "temurin-21-jdk-amd64")
    return root, builtin, (make_jdk(root / "8", "1.8.0_504") if installed8 else None)


# ---------- jdk use 8 的测试替身：本机伪造 Debian 包（不需要 dpkg-deb，也不触网） ----------

def ar_archive(entries):
    """按 ar 格式拼 Debian 包容器（成员名 16 字节、长度 10 字节、成员头 60 字节、偶数字节对齐）。"""
    blob = bytearray(b"!<arch>\n")
    for name, data in entries:
        header = "%-16s%-12d%-6d%-6d%-8s%-10d`\n" % (name, 0, 0, 0, "100644", len(data))
        assert len(header) == 60, len(header)
        blob += header.encode("ascii") + data
        if len(data) % 2:
            blob += b"\n"
    return bytes(blob)


def tar_xz(entries):
    """entries: (名称, 权限, 类型, 内容)；类型 ∈ file/dir/symlink（内容对链接即目标）。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        for name, mode, kind, payload in entries:
            info = tarfile.TarInfo(name)
            info.mode = mode
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = payload
                tar.addfile(info)
            else:
                data = payload.encode("utf-8")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def fake_jdk_entries(prefix=JDK8_DIR_IN_PKG, java_version="1.8.0_504", javac_version=None,
                     with_javac=True):
    """包内 JDK 树：bin/java、bin/javac 打印真实格式版本（java 走 stderr，与真实 java 一致）。"""
    javac_version = javac_version or java_version
    entries = [
        (prefix, 0o755, "dir", None),
        ("%s/bin" % prefix, 0o755, "dir", None),
        ("%s/bin/java" % prefix, 0o755, "file",
         "#!/usr/bin/env bash\nprintf 'openjdk version \"%s\" 2026-01-01\\n' >&2\n" % java_version),
    ]
    if with_javac:
        entries.append(("%s/bin/javac" % prefix, 0o755, "file",
                        "#!/usr/bin/env bash\nprintf 'javac %s\\n'\n" % javac_version))
    return entries


def make_deb(path, data_entries=None, package=JDK8_PACKAGE, arch="amd64", version=JDK8_VERSION,
             data_member="data.tar.xz"):
    """造最小 Debian 包：debian-binary + control.tar.xz + data.tar.xz。"""
    if data_entries is None:
        data_entries = fake_jdk_entries()
    control = ("Package: %s\nVersion: %s\nArchitecture: %s\nMaintainer: 测试 <t@example.com>\n"
               "Depends: \n" % (package, version, arch))
    path.write_bytes(ar_archive([
        ("debian-binary", b"2.0\n"),
        ("control.tar.xz", tar_xz([("./control", 0o644, "file", control)])),
        (data_member, tar_xz(data_entries)),
    ]))
    return path


def source_env(pkg):
    """显式配置的可信来源（自建镜像/离线导入/测试）：URL 与独立校验值成对给出。"""
    return {"CADENCE_JDK8_URL": "file://" + str(pkg),
            "CADENCE_JDK8_SHA256": hashlib.sha256(pkg.read_bytes()).hexdigest()}


def assert_state_unchanged(root, before):
    """失败路径契约：当前选择不变、不发布新 JDK 目录、不残留临时目录。"""
    assert current_target(root) == before, "失败路径不得改变当前选择"
    assert not (root / "8").exists(), "失败路径不得留下已发布的 JDK 目录"
    assert list(root.glob(".install*")) == [], "失败路径必须清理临时目录"


# ---------- 初始化：空卷默认 21 / 已有选择保留 / 损坏报错不重置 ----------

def test_init_empty_state_selects_builtin_21(tmp_path):
    root, builtin, _ = state(tmp_path / "jdks", tmp_path, installed8=False)
    r = jdk_run(env_for(root, builtin), "init")
    assert r.returncode == 0, r.stderr
    assert current_target(root) == str(builtin)
    assert (root / "current" / "bin" / "java").is_file()   # 经稳定路径可达内置 JDK


def test_init_preserves_existing_valid_selection(tmp_path):
    root, builtin, installed8 = state(tmp_path / "jdks", tmp_path)
    (root / "current").symlink_to(installed8)
    r = jdk_run(env_for(root, builtin), "init")
    assert r.returncode == 0, r.stderr
    assert current_target(root) == str(installed8), "已有有效选择不得被覆盖为内置版本"
    assert "保留" in r.stderr


def test_init_reports_corrupt_state_without_reset(tmp_path):
    builtin = make_jdk(tmp_path / "jvm" / "temurin-21-jdk-amd64")
    cases = {}

    dangling_root = tmp_path / "dangling"
    dangling_root.mkdir()
    (dangling_root / "current").symlink_to(tmp_path / "gone" / "jdk-8")   # 额外安装被删/卷损坏
    cases["悬空目标"] = dangling_root

    broken_root = tmp_path / "broken"
    (broken_root / "8" / "bin").mkdir(parents=True)                       # 目标在但不是 JDK
    (broken_root / "current").symlink_to(broken_root / "8")
    cases["无 bin/java"] = broken_root

    for name, root in cases.items():
        before = current_target(root)
        r = jdk_run(env_for(root, builtin), "init")
        assert r.returncode != 0, f"{name}：损坏状态必须非零退出（stderr={r.stderr}）"
        assert "损坏" in r.stderr, f"{name}：须明确报错（stderr={r.stderr}）"
        assert current_target(root) == before, f"{name}：损坏状态不得被悄悄重置"


def test_init_migrates_stale_builtin_path(tmp_path):
    root, builtin, _ = state(tmp_path / "jdks", tmp_path, installed8=False)
    stale = tmp_path / "old-image" / "lib" / "jvm" / "temurin-21-jdk-amd64"   # 镜像升级后旧内置路径
    (root / "current").symlink_to(stale)
    r = jdk_run(env_for(root, builtin), "init")
    assert r.returncode == 0, r.stderr
    assert current_target(root) == str(builtin), "内置路径随镜像变化时应迁移，而非重置为任意版本"
    assert "迁移" in r.stderr


# ---------- jdk use：切回内置 / 切到已安装额外版本 / 各失败路径保持原选择 ----------

def test_use_21_switches_back_atomically(tmp_path):
    root, builtin, installed8 = state(tmp_path / "jdks", tmp_path)
    (root / "current").symlink_to(installed8)
    r = jdk_run(env_for(root, builtin), "use", "21")
    assert r.returncode == 0, r.stderr
    assert current_target(root) == str(builtin)
    assert list(root.glob(".current*")) == [], "原子替换不得残留临时链接"


def test_use_switches_to_existing_extra_install(tmp_path):
    # 已安装的额外版本走同一 use 路径直接切换，不重新下载
    root, builtin, installed8 = state(tmp_path / "jdks", tmp_path)
    (root / "current").symlink_to(builtin)
    r = jdk_run(env_for(root, builtin), "use", "8")
    assert r.returncode == 0, r.stderr
    assert current_target(root) == str(installed8)


def test_use_rejects_extra_arguments(tmp_path):
    """多余参数必须按用法错误拒绝（不静默忽略），且不得切换。"""
    root, builtin, installed8 = state(tmp_path / "jdks", tmp_path)
    (root / "current").symlink_to(installed8)
    for extra in (("21", "8"), ("21", "--force")):
        r = jdk_run(env_for(root, builtin), "use", *extra)
        assert r.returncode == 2, "多余参数必须拒绝（stderr=%s）" % r.stderr
        assert "用法" in r.stderr, r.stderr
        assert current_target(root) == str(installed8), "多余参数不得触发切换"


# ---------- jdk use 8：按需下载安装（可信源、包校验、安全解包、原子发布）与失败安全 ----------

def test_use8_installs_verified_package_and_switches(tmp_path):
    """正例：包类型/身份 → 独立哈希 → 安全解包 → 真实 java/javac 版本 → 原子发布 → 切换。"""
    root, builtin, _ = state(tmp_path / "jdks", tmp_path, installed8=False)
    (root / "current").symlink_to(builtin)
    pkg = make_deb(tmp_path / "temurin-8-jdk.deb")
    r = jdk_run(env_for(root, builtin, **source_env(pkg)), "use", "8")
    assert r.returncode == 0, r.stderr
    assert current_target(root) == str(root / "8"), "成功后 current 必须指向卷内新装目录"
    assert "下载" in r.stderr and str(pkg) in r.stderr, "必须显式报告实际使用的来源"
    java = subprocess.run([str(root / "8" / "bin" / "java"), "-version"],
                          capture_output=True, text=True, timeout=60)
    javac = subprocess.run([str(root / "8" / "bin" / "javac"), "-version"],
                           capture_output=True, text=True, timeout=60)
    assert "1.8.0_504" in java.stderr, java.stderr
    assert "1.8.0_504" in javac.stdout, javac.stdout
    assert list(root.glob(".install*")) == [], "安装完成后必须清理临时目录"


def test_use8_reuses_installed_copy_without_download(tmp_path):
    """已安装且校验通过：不得重复下载（来源不可用也必须成功）。"""
    root, builtin, installed8 = state(tmp_path / "jdks", tmp_path)
    (root / "current").symlink_to(builtin)
    r = jdk_run(env_for(root, builtin), "use", "8")
    assert r.returncode == 0, r.stderr
    assert current_target(root) == str(installed8)
    assert "下载" not in r.stderr, "已安装的版本不得再下载"


def test_use8_download_failure_keeps_selection(tmp_path):
    root, builtin, _ = state(tmp_path / "jdks", tmp_path, installed8=False)
    (root / "current").symlink_to(builtin)
    r = jdk_run(env_for(root, builtin,
                        CADENCE_JDK8_URL="file://%s/missing.deb" % tmp_path,
                        CADENCE_JDK8_SHA256="0" * 64), "use", "8")
    assert r.returncode != 0, "下载失败不得报成功"
    assert "下载失败" in r.stderr, r.stderr
    assert_state_unchanged(root, str(builtin))


def test_use8_hash_mismatch_keeps_selection(tmp_path):
    """包可下载但哈希不符（镜像被替换/损坏）：必须拒绝，且不发布、不切换。"""
    root, builtin, _ = state(tmp_path / "jdks", tmp_path, installed8=False)
    (root / "current").symlink_to(builtin)
    pkg = make_deb(tmp_path / "temurin-8-jdk.deb")
    r = jdk_run(env_for(root, builtin, CADENCE_JDK8_URL="file://" + str(pkg),
                        CADENCE_JDK8_SHA256="b" * 64), "use", "8")
    assert r.returncode != 0, "哈希不符不得报成功"
    assert "SHA-256" in r.stderr, r.stderr
    assert_state_unchanged(root, str(builtin))


def test_use8_rejects_non_deb_payload(tmp_path):
    """哈希相符但不是 Debian 包（换成 tar 归档）：必须按包类型拒绝。"""
    root, builtin, _ = state(tmp_path / "jdks", tmp_path, installed8=False)
    (root / "current").symlink_to(builtin)
    pkg = tmp_path / "payload.tar.xz"
    pkg.write_bytes(tar_xz([("jdk/bin/java", 0o755, "file", "#!/bin/sh\n")]))
    r = jdk_run(env_for(root, builtin, **source_env(pkg)), "use", "8")
    assert r.returncode != 0, "非 Debian 包不得被安装"
    assert "不是 Debian 包" in r.stderr, r.stderr
    assert_state_unchanged(root, str(builtin))


def test_use8_rejects_package_identity_mismatch(tmp_path):
    """控制字段身份不符（包名/架构/版本）必须在解包前拒绝。"""
    cases = {"架构": {"arch": "arm64"},
             "包名": {"package": "temurin-9-jdk"},
             "版本": {"version": "8.0.999.0.0+1-0"}}
    for label, overrides in cases.items():
        root, builtin, _ = state(tmp_path / ("jdks-" + label), tmp_path, installed8=False)
        (root / "current").symlink_to(builtin)
        pkg = make_deb(tmp_path / ("pkg-%s.deb" % label), **overrides)
        r = jdk_run(env_for(root, builtin, **source_env(pkg)), "use", "8")
        assert r.returncode != 0, "%s：身份不符必须失败（stderr=%s）" % (label, r.stderr)
        assert "不符" in r.stderr, "%s：%s" % (label, r.stderr)
        assert_state_unchanged(root, str(builtin))


def test_use8_rejects_path_escape_member(tmp_path):
    """data 成员携 .. 逃出解包目录：拒绝，且不得在解包目标之外写出文件。"""
    root, builtin, _ = state(tmp_path / "jdks", tmp_path, installed8=False)
    (root / "current").symlink_to(builtin)
    outside = tmp_path / "escaped.txt"
    pkg = make_deb(tmp_path / "escape.deb", data_entries=[
        ("%s/" % JDK8_DIR_IN_PKG, 0o755, "dir", None),
        ("../../../escaped.txt", 0o644, "file", "owned"),
    ])
    r = jdk_run(env_for(root, builtin, **source_env(pkg)), "use", "8")
    assert r.returncode != 0, "解包逃逸必须失败"
    assert "逃逸" in r.stderr, r.stderr
    assert not outside.exists(), "不得写出解包目标目录之外"
    assert_state_unchanged(root, str(builtin))


def test_use8_rejects_link_traversal_member(tmp_path):
    """先放符号链接指向解包根之外，再借该链接写入：必须拒绝。"""
    root, builtin, _ = state(tmp_path / "jdks", tmp_path, installed8=False)
    (root / "current").symlink_to(builtin)
    hijacked = tmp_path / "hijacked"
    pkg = make_deb(tmp_path / "hook.deb", data_entries=[
        ("%s/" % JDK8_DIR_IN_PKG, 0o755, "dir", None),
        ("hook", 0o777, "symlink", str(hijacked)),
        ("hook/pwned", 0o644, "file", "owned"),
    ])
    r = jdk_run(env_for(root, builtin, **source_env(pkg)), "use", "8")
    assert r.returncode != 0, "经由符号链接写入必须失败"
    assert "逃逸" in r.stderr, r.stderr
    assert not hijacked.exists(), "不得经链接写出解包目标目录之外"
    assert_state_unchanged(root, str(builtin))


def test_use8_rejects_package_without_javac(tmp_path):
    """只有 JRE（缺 javac）的包不得被发布或激活。"""
    root, builtin, _ = state(tmp_path / "jdks", tmp_path, installed8=False)
    (root / "current").symlink_to(builtin)
    pkg = make_deb(tmp_path / "jre-only.deb", data_entries=fake_jdk_entries(with_javac=False))
    r = jdk_run(env_for(root, builtin, **source_env(pkg)), "use", "8")
    assert r.returncode != 0, "缺 javac 的包不得被激活"
    assert "javac" in r.stderr, r.stderr
    assert_state_unchanged(root, str(builtin))


def test_use8_rejects_wrong_java_version(tmp_path):
    """包内 java/javac 实际主版本不是 8：不得发布或切换。"""
    root, builtin, _ = state(tmp_path / "jdks", tmp_path, installed8=False)
    (root / "current").symlink_to(builtin)
    pkg = make_deb(tmp_path / "wrong-version.deb",
                   data_entries=fake_jdk_entries(java_version="21.0.12"))
    r = jdk_run(env_for(root, builtin, **source_env(pkg)), "use", "8")
    assert r.returncode != 0, "实际版本不是 8 时不得切换"
    assert "不一致" in r.stderr, r.stderr
    assert_state_unchanged(root, str(builtin))


def test_use8_requires_paired_source_override(tmp_path):
    """只配置来源而不同时给出独立校验值：拒绝，避免静默放宽校验。"""
    root, builtin, _ = state(tmp_path / "jdks", tmp_path, installed8=False)
    (root / "current").symlink_to(builtin)
    pkg = make_deb(tmp_path / "temurin-8-jdk.deb")
    r = jdk_run(env_for(root, builtin, CADENCE_JDK8_URL="file://" + str(pkg),
                        CADENCE_JDK8_SHA256=""), "use", "8")
    assert r.returncode != 0, "来源与校验值不成对时不得安装"
    assert "成对" in r.stderr, r.stderr
    assert_state_unchanged(root, str(builtin))


def test_use8_interrupt_cleans_tmp_and_keeps_selection(tmp_path):
    """下载中途被中断：清理临时目录、不发布、当前选择不变。"""
    root, builtin, _ = state(tmp_path / "jdks", tmp_path, installed8=False)
    (root / "current").symlink_to(builtin)
    pkg = make_deb(tmp_path / "temurin-8-jdk.deb")
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        "out=''; previous=''\n"
        "for arg in \"$@\"; do [ \"$previous\" = '-o' ] && out=\"$arg\"; previous=\"$arg\"; done\n"
        "printf 'partial download' > \"$out\"\n"
        "kill -TERM \"$PPID\"\n"
        "exit 143\n",
        encoding="utf-8")
    curl.chmod(0o755)
    env = env_for(root, builtin, **source_env(pkg))
    env["PATH"] = "%s:%s" % (bin_dir, os.environ["PATH"])
    r = jdk_run(env, "use", "8")
    assert r.returncode != 0, "被中断的安装不得报成功"
    assert_state_unchanged(root, str(builtin))


def test_use8_prunes_stale_tmp_dirs_but_keeps_fresh(tmp_path):
    """SIGKILL/断电残留（走不到 trap）：过期 `.install.*` 在下次安装前清理；
    新鲜目录（可能是并发进行的安装）必须保留。"""
    root, builtin, _ = state(tmp_path / "jdks", tmp_path, installed8=False)
    (root / "current").symlink_to(builtin)
    stale = root / ".install.stale01"
    fresh = root / ".install.fresh01"
    for path in (stale, fresh):
        path.mkdir()
        (path / "package.deb").write_text("partial download", encoding="utf-8")
    old = time.time() - 2 * 3600
    os.utime(stale, (old, old))
    assert {p.name for p in root.glob(".install*")} == {stale.name, fresh.name}

    pkg = make_deb(tmp_path / "temurin-8-jdk.deb")
    r = jdk_run(env_for(root, builtin, **source_env(pkg)), "use", "8")
    assert r.returncode == 0, r.stderr
    assert current_target(root) == str(root / "8")
    assert not stale.exists(), "超过过期阈值的残留临时目录必须被清理"
    assert fresh.is_dir(), "新鲜临时目录（可能是并发安装）不得被删除"
    assert str(stale) in r.stderr, "清理残留目录必须留痕（stderr=%s）" % r.stderr


def test_use8_does_not_overwrite_broken_existing_install(tmp_path):
    """卷内已有 8 目录但校验失败：明确报错、不覆盖、不重复下载、不改选择。"""
    root, builtin, installed8 = state(tmp_path / "jdks", tmp_path)
    (installed8 / "bin" / "javac").unlink()
    marker = installed8 / "marker"
    marker.write_text("keep", encoding="utf-8")
    (root / "current").symlink_to(builtin)
    pkg = make_deb(tmp_path / "temurin-8-jdk.deb")
    r = jdk_run(env_for(root, builtin, **source_env(pkg)), "use", "8")
    assert r.returncode != 0, "损坏的已有安装不得被当作可用版本"
    assert "已存在" in r.stderr, r.stderr
    assert marker.is_file(), "已有安装目录不得被覆盖"
    assert current_target(root) == str(builtin), "失败时当前选择不得改变"


def test_use_unsupported_major_rejected(tmp_path):
    root, builtin, _ = state(tmp_path / "jdks", tmp_path, installed8=False)
    (root / "current").symlink_to(builtin)
    r = jdk_run(env_for(root, builtin), "use", "17")
    assert r.returncode != 0
    assert "不支持" in r.stderr, r.stderr
    assert current_target(root) == str(builtin)


def test_use_rejects_incomplete_jdk_without_javac(tmp_path):
    root, builtin, installed8 = state(tmp_path / "jdks", tmp_path)
    (builtin / "bin" / "javac").unlink()          # 内置 21 只剩 JRE：不得被激活
    (root / "current").symlink_to(installed8)
    r = jdk_run(env_for(root, builtin), "use", "21")
    assert r.returncode != 0, "缺 javac 的安装不得被激活"
    assert "javac" in r.stderr, r.stderr
    assert current_target(root) == str(installed8)


def test_use_rejects_version_mismatch(tmp_path):
    root, _, installed8 = state(tmp_path / "jdks", tmp_path)
    builtin = make_jdk(tmp_path / "jvm" / "temurin-21-jdk-amd64", java_version="1.8.0_504")
    (root / "current").symlink_to(installed8)
    r = jdk_run(env_for(root, builtin), "use", "21")
    assert r.returncode != 0, "实际版本与请求主版本不符时不得切换"
    assert "不一致" in r.stderr, r.stderr
    assert current_target(root) == str(installed8)


def test_use_21_reports_missing_builtin_dir_not_unsupported(tmp_path):
    """21 是支持的主版本：内置目录缺失（镜像/配置不一致）时须报内置缺失，不得报“不支持”。"""
    root = tmp_path / "jdks"
    root.mkdir()
    missing_builtin = tmp_path / "jvm" / "temurin-21-jdk-amd64"      # 故意不创建
    r = jdk_run(env_for(root, missing_builtin), "use", "21")
    assert r.returncode != 0
    assert "不支持" not in r.stderr, f"21 是支持的主版本，不得报不支持：{r.stderr}"
    assert "内置" in r.stderr and str(missing_builtin) in r.stderr, r.stderr


# ---------- 兜底入口（/usr/local/bin 软链接）：损坏状态不得回退到系统 Java ----------

def make_echo_jdk(path, banner='openjdk version "21.0.12"'):
    """替身 JDK：bin/<工具> 回显参数（stdout）与版本横幅（stderr），用于验证 exec 与参数透传。"""
    bin_dir = path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for tool in ("java", "javac"):
        binary = bin_dir / tool
        binary.write_text(
            f"""#!/usr/bin/env bash
printf 'argv:%s\\n' "$*"
printf '{banner}\\n' >&2
""",
            encoding="utf-8",
        )
        binary.chmod(0o755)
    return path


def shim_link(tmp_path, name):
    """模拟 /usr/local/bin/<工具> → 兜底入口（PATH 中位于 current/bin 之后、/usr/bin 之前）。"""
    bin_dir = tmp_path / "local-bin"
    bin_dir.mkdir(exist_ok=True)
    link = bin_dir / name
    if not link.exists():
        link.symlink_to(SHIM)
    return link


def test_shim_execs_selected_jdk_through_stable_symlink(tmp_path):
    """有效状态：直接执行工具（含绝对路径）经稳定入口拿到所选 JDK，参数原样透传。"""
    root = tmp_path / "jdks"
    root.mkdir()
    builtin = make_echo_jdk(tmp_path / "jvm" / "temurin-21-jdk-amd64")
    (root / "current").symlink_to(builtin)
    env = env_for(root, builtin)
    r = subprocess.run([str(shim_link(tmp_path, "java")), "-version"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "argv:-version" in r.stdout
    assert "21.0.12" in r.stderr


def test_shim_fails_closed_when_state_broken(tmp_path):
    """损坏状态（current 悬空 / JAVA_HOME 未设置）：明确失败，不执行任何 Java。"""
    root = tmp_path / "jdks"
    root.mkdir()
    builtin = make_echo_jdk(tmp_path / "jvm" / "temurin-21-jdk-amd64")
    for label, java_home in (("current 悬空", str(root / "current")), ("JAVA_HOME 未设置", "")):
        env = env_for(root, builtin, JAVA_HOME=java_home)
        r = subprocess.run([str(shim_link(tmp_path, "java")), "-version"], env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode != 0, f"{label}：必须非零退出"
        assert "状态不可用" in r.stderr, f"{label}：须明确报错（stderr={r.stderr}）"
        assert r.stdout == "", f"{label}：不得有任何 Java 输出"


def test_path_resolution_stops_at_shim_not_system_java(tmp_path):
    """损坏状态：`java` 必须命中兜底入口并失败——不得落到 PATH 后面的系统 Java（容器内 = /usr/bin/java 21）。"""
    root = tmp_path / "jdks"
    root.mkdir()
    builtin = make_echo_jdk(tmp_path / "jvm" / "temurin-21-jdk-amd64")
    shim_dir = shim_link(tmp_path, "java").parent
    sys_dir = tmp_path / "sys-bin"
    sys_dir.mkdir()
    system_java = sys_dir / "java"
    system_java.write_text('#!/usr/bin/env bash\nprintf \'SYSTEM-JAVA %s\\n\' "$*"\n', encoding="utf-8")
    system_java.chmod(0o755)
    env = env_for(root, builtin)
    env["PATH"] = f"{shim_dir}:{sys_dir}:{os.environ['PATH']}"
    r = subprocess.run(["bash", "-c", "java -version"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0, "损坏状态必须失败，而不是给出系统 Java 的版本"
    assert "SYSTEM-JAVA" not in r.stdout + r.stderr, "不得回退到系统 Java"
    assert "状态不可用" in r.stderr, r.stderr


# ---------- jdk list ----------

def test_init_fails_loudly_when_state_volume_not_writable(tmp_path):
    """状态卷属主不对（如 root 拥有）时必须明确报错，而不是在不可持久的位置“成功”。"""
    root = tmp_path / "jdks"
    root.mkdir()
    builtin = make_jdk(tmp_path / "jvm" / "temurin-21-jdk-amd64")
    root.chmod(0o500)
    try:
        r = jdk_run(env_for(root, builtin), "init")
        assert r.returncode != 0
        assert "不可写" in r.stderr, r.stderr
        assert not (root / "current").exists()
    finally:
        root.chmod(0o700)


def test_list_marks_current_and_sources(tmp_path):
    root, builtin, installed8 = state(tmp_path / "jdks", tmp_path)
    (root / "current").symlink_to(installed8)
    r = jdk_run(env_for(root, builtin), "list")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert any(line.startswith("* 8") for line in lines), f"当前版本未标记：{r.stdout}"
    assert any(line.startswith("- 21") for line in lines), f"内置版本未列出：{r.stdout}"
    assert "内置" in r.stdout and "已安装" in r.stdout
    assert "当前选择：8" in r.stdout


def test_list_flags_corrupt_current(tmp_path):
    root, builtin, _ = state(tmp_path / "jdks", tmp_path, installed8=False)
    (root / "current").symlink_to(tmp_path / "gone" / "jdk-8")
    r = jdk_run(env_for(root, builtin), "list")
    assert r.returncode == 0, r.stderr
    assert "损坏" in r.stdout, r.stdout
    assert "21" in r.stdout


def test_unknown_subcommand_and_usage(tmp_path):
    root, builtin, _ = state(tmp_path / "jdks", tmp_path, installed8=False)
    r = jdk_run(env_for(root, builtin), "bogus")
    assert r.returncode != 0
    assert "list" in r.stdout + r.stderr and "use" in r.stdout + r.stderr


# ---------- 镜像层与 Windows 安装器接线 ----------

def test_image_pins_stable_java_home_and_jdk_command():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "JAVA_HOME=/home/dev/.cadence/jdks/current" in text, "镜像必须固定 JAVA_HOME 到稳定路径"
    assert "CADENCE_JDK_ROOT=/home/dev/.cadence/jdks" in text
    assert "PATH=/home/dev/.cadence/jdks/current/bin:" in text, "current/bin 必须优先于系统 Java"
    assert "CADENCE_BUILTIN_JDK_MAJOR=${JDK_VERSION}" in text
    assert "COPY --chmod=0755 jdk.sh /usr/local/bin/jdk" in text
    # 空卷首挂继承镜像内属主：目录必须镜像内预建且属 dev，否则 dev 无法写入状态。
    # install -d 只把 -o/-g 施加到每个路径的最后一级，故父目录 /home/dev/.cadence 必须一并列出——
    # 否则它以 root 拥有，entrypoint 建 ~/.cadence/logs 会 Permission denied（2026-09-23 真机实测）。
    assert "install -d -m 0755 -o dev -g dev /home/dev/.cadence /home/dev/.cadence/jdks" in text


def test_image_installs_fail_closed_jdk_tool_shims():
    """损坏状态不得落回 /usr/bin 的系统 Java：/usr/local/bin 内置入按工具名分发的兜底入口。"""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY --chmod=0755 jdk-shim.sh /usr/local/lib/cadence/jdk-shim.sh" in text
    assert '"$CADENCE_BUILTIN_JDK_DIR"/bin/*' in text, "兜底入口须覆盖内置 JDK 的全部工具名"
    assert 'ln -s /usr/local/lib/cadence/jdk-shim.sh "/usr/local/bin/$name"' in text
    assert "test -L /usr/local/bin/java" in text and "test -L /usr/local/bin/javac" in text


def test_windows_installer_precreates_and_mounts_jdk_volume():
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "'cadence-jdks'" in text, "安装器必须预建 JDK 状态卷（external 卷 compose 不创建）"
    assert "-v cadence-jdks:/home/dev/.cadence/jdks" in text, "直接 podman run 必须显式挂载该卷"
