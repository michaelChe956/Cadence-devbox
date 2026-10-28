# -*- coding: utf-8 -*-
"""entrypoint.sh 主线行为：失败路径（auth 缺字段 exit 2）/ 全链路 / 软失败 / 模板同步保护。"""
import os
import subprocess
from pathlib import Path

from test_jdk import make_jdk

REPO = Path(__file__).resolve().parents[2]
ENTRY = REPO / "devbox" / "entrypoint.sh"
SCRIPT = REPO / "devbox" / "render-auth.py"


def builtin_dir(home):
    """测试用内置 JDK 21 目录（entrypoint 与之同层，不在状态卷内）。"""
    return home.parent / "jvm" / "temurin-21-jdk-amd64"

VALID = """\
git:
  name: 张三
  email: zhangsan@corp.com
providers:
  relay1:
    base_url: http://relay1.internal:9527/v1
    api_key: sk-live-secret
    models:
      - { id: glm-5.3, ctx: 200k }
agents:
  claude: { provider: relay1, model: glm-5.3 }
  codex:  { provider: relay1, model: glm-5.3 }
  pi:     { provider: relay1 }
  kimi:   { provider: relay1 }
  omp:    { provider: relay1, model: glm-5.3 }
"""


def _env(home, stack, auth_file, offline=False):
    env = {**os.environ, "HOME": str(home),
           "CADENCE_STACK_DIR": str(stack),
           "CADENCE_STACK_TEMPLATE_DIR": str(stack.parent / "template"),
           "CADENCE_RENDER_AUTH": str(SCRIPT),
           "CADENCE_AUTH_FILE": str(auth_file),
           "CADENCE_JDK": str(REPO / "devbox" / "jdk.sh"),
           "CADENCE_JDK_ROOT": str(home / ".cadence" / "jdks"),
           "CADENCE_BUILTIN_JDK_DIR": str(builtin_dir(home)),
           "JAVA_HOME": str(home / ".cadence" / "jdks" / "current")}
    if offline:  # 让 skills 安装的三个镜像全部快速失败——验证软失败不阻断
        env["https_proxy"] = env["http_proxy"] = "http://127.0.0.1:1"
        env["HTTPS_PROXY"] = env["HTTP_PROXY"] = "http://127.0.0.1:1"
    return env


def _make(tmp_path, auth_body=VALID, with_changes=False):
    home = tmp_path / "home"
    stack = tmp_path / "mount" / "stack"
    tmpl = tmp_path / "mount" / "template"
    (stack).mkdir(parents=True)
    # 预置假 .git 标记：boot_skills 走 git pull 快速失败→软失败路径，使本测试离线确定性
    (home / ".agents/Cadence-skills/.git").mkdir(parents=True)
    (home / ".cadence/jdks").mkdir(parents=True)          # JDK 状态卷挂载点（测试用普通目录）
    make_jdk(builtin_dir(home))                           # 镜像内置 Temurin 21 替身
    (tmpl / "catalog/mysql/conf").mkdir(parents=True)
    (tmpl / "compose.yaml").write_text("name: cadence\nservices: {}\n", encoding="utf-8")
    (tmpl / "catalog/mysql/profile").write_text("default\n", encoding="utf-8")
    (tmpl / "docker-compose.no-sock.yml").write_text("services: {}\n", encoding="utf-8")
    auth = tmp_path / "auth.yaml"
    auth.write_text(auth_body, encoding="utf-8")
    if with_changes:
        (stack / "CHANGES.md").write_text("- 2026-09-11 add kafka\n", encoding="utf-8")
        (stack / "compose.yaml").write_text("# 用户改过版\n", encoding="utf-8")
    return home, stack, auth


def _run(env, argv=("sleep", "0")):
    return subprocess.run(["bash", str(ENTRY), *argv], env=env,
                          capture_output=True, text=True, timeout=300)


def test_exit2_on_missing_api_key(tmp_path):
    home, stack, auth = _make(tmp_path, auth_body=VALID.replace("api_key: sk-live-secret", 'api_key: ""'))
    r = _run(_env(home, stack, auth))
    assert r.returncode == 2
    assert "鉴权" in r.stderr
    assert not (home / ".claude").exists()      # 拒启：不落任何渲染产物


def test_full_boot_order_and_outputs(tmp_path):
    home, stack, auth = _make(tmp_path)
    r = _run(_env(home, stack, auth))
    assert r.returncode == 0, r.stderr
    # 步骤 2：git 对齐 + 轮询 env（bashrc 幂等块）+ 卷内源配置刷新
    gc = (home / ".gitconfig").read_text()
    assert "autocrlf = true" in gc and "filemode = false" in gc
    assert "cadence-entrypoint-env" in (home / ".bashrc").read_text()
    assert "aliyun" in (home / ".m2/settings.xml").read_text()
    assert (home / ".gradle/init.gradle").exists()
    # 步骤 3：五端渲染
    assert (home / ".claude/settings.json").exists()
    assert (home / ".kimi-code/region").read_text().strip() == "cn"
    # 步骤 4：stack 模板同步（无 CHANGES.md）
    assert "services" in (stack / "compose.yaml").read_text()
    assert (stack / "catalog/mysql/profile").exists()
    assert (stack / "docker-compose.no-sock.yml").exists()
    # 步骤 6：exec "$@"（entrypoint 自身日志走 stderr；skills 输出不参与断言）
    assert "就绪" in r.stderr


def test_skills_softfail_does_not_block(tmp_path):
    home, stack, auth = _make(tmp_path)
    r = _run(_env(home, stack, auth, offline=True))
    assert r.returncode == 0, r.stderr
    log = (home / ".cadence/logs/entrypoint.log").read_text()
    assert "警告" in log and "重试" in log
    assert (home / ".claude/settings.json").exists()   # 主链路不受影响


def test_stack_sync_preserves_user_version(tmp_path):
    home, stack, auth = _make(tmp_path, with_changes=True)
    r = _run(_env(home, stack, auth))
    assert r.returncode == 0, r.stderr
    assert (stack / "compose.yaml").read_text() == "# 用户改过版\n"   # CHANGES.md 存在→不覆盖
    assert "保留" in r.stderr or "保留" in (home / ".cadence/logs/entrypoint.log").read_text()

def test_stack_sync_backs_up_diverged_local_files(tmp_path):
    """回归：无 CHANGES.md 时，与模板分叉的本地文件先备份为 *.bak-<时间戳> 再覆盖——
    手工改动不得静默丢失（2026-09-28 Windows 实测 compose.yaml 手工挂载行被模板同步抹掉 2 次）。"""
    home, stack, auth = _make(tmp_path)
    (stack / "compose.yaml").write_text("# 我手工改过\n", encoding="utf-8")
    (stack / "catalog/mysql").mkdir(parents=True)
    (stack / "catalog/mysql/profile").write_text("mq\n", encoding="utf-8")            # 与模板分叉→备份后覆盖
    (stack / "catalog/mysql/conf").mkdir(parents=True)
    (stack / "catalog/mysql/conf/my.cnf").write_text("# 我加的\n", encoding="utf-8")  # 模板没有→原样保留
    r = _run(_env(home, stack, auth, offline=True))
    assert r.returncode == 0
    tmpl = stack.parent / "template"
    assert (stack / "compose.yaml").read_text(encoding="utf-8") == (tmpl / "compose.yaml").read_text(encoding="utf-8")
    assert (stack / "catalog/mysql/profile").read_text(encoding="utf-8") == (tmpl / "catalog/mysql/profile").read_text(encoding="utf-8")
    bak = list(stack.glob("*.bak-*"))
    assert len(bak) == 1 and bak[0].name.startswith("compose.yaml.bak-")
    assert bak[0].read_text(encoding="utf-8") == "# 我手工改过\n"
    pbak = list((stack / "catalog/mysql").glob("profile.bak-*"))
    assert len(pbak) == 1 and pbak[0].read_text(encoding="utf-8") == "mq\n"
    assert (stack / "catalog/mysql/conf/my.cnf").read_text(encoding="utf-8") == "# 我加的\n"  # 本地新增文件不动
    assert "备份" in r.stderr


def test_jdk_state_defaults_preserved_and_corruption_reported(tmp_path):
    """空卷→内置 21；重建保留已有有效选择；损坏→日志明确报错且不重置（容器仍可进，jdk use 21 可修复）。"""
    home, stack, auth = _make(tmp_path)
    env = _env(home, stack, auth)
    root = home / ".cadence/jdks"
    builtin = builtin_dir(home)

    r = _run(env)                                            # 空卷首启
    assert r.returncode == 0, r.stderr
    assert os.readlink(root / "current") == str(builtin)

    installed8 = make_jdk(root / "8", "1.8.0_504")
    (root / "current").unlink()
    (root / "current").symlink_to(installed8)
    r = _run(env)                                            # 重建容器
    assert r.returncode == 0, r.stderr
    assert os.readlink(root / "current") == str(installed8), "重建不得覆盖已有选择"

    broken = tmp_path / "gone" / "8"
    (root / "current").unlink()
    (root / "current").symlink_to(broken)
    r = _run(env)                                            # 状态损坏
    assert r.returncode == 0, r.stderr                       # 报错但不阻断启动：容器内可自行修复
    log = (home / ".cadence/logs/entrypoint.log").read_text()
    assert "损坏" in log and "未自动重置" in log
    assert os.readlink(root / "current") == str(broken), "损坏状态不得被悄悄重置"


def test_skills_installer_retries_with_diagnostics(tmp_path):
    """skills 安装脚本首跑失败 → 换 /bin/bash 重试；仍失败则打 shell 版本与脚本头（可定位）。"""
    text = ENTRY.read_text(encoding="utf-8")
    helper = text.split("run_skills_installer()", 1)[1].split("\nboot_skills()", 1)[0]
    good = tmp_path / "good.sh"
    good.write_text("#!/usr/bin/env bash\nset -euo pipefail\necho INSTALLER_OK\n", encoding="utf-8")
    bad = tmp_path / "bad.sh"
    bad.write_text("#!/usr/bin/env bash\necho 'set: pipefail: invalid option name' >&2\nexit 7\n",
                   encoding="utf-8")
    harness = tmp_path / "h.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "log() { printf '[log] %s\\n' \"$*\"; }\n"
        "run_skills_installer()" + helper + "\n"
        "run_skills_installer \"$1\"; r1=$?\n"
        "run_skills_installer \"$2\"; r2=$?\n"
        "echo \"RC=$r1,$r2\"\n",
        encoding="utf-8")
    r = subprocess.run(["bash", str(harness), str(good), str(bad)],
                       capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    assert "INSTALLER_OK" in out, out
    assert "用 /bin/bash 重试" in out, f"失败后须重试并留痕：{out}"
    assert "重试仍失败" in out, f"二次失败须打诊断：{out}"
    assert "RC=0,1" in out, out
