# Windows 真机操作步骤：devbox 容器统一到 `cadence_devbox`

> 对应改动：OpenSpec change `unify-devbox-container-and-vpn-services`（2026-10-28）
> 前提：Windows 侧仓库已更新到本次改动（`D:\testpodman\Cadence-devbox`），安装目录 `D:\testpodman\cadence`

## 0. 先看现状

```powershell
podman ps -a --format "{{.Names}}  {{.Status}}"
```

预期能看到：运行中的 `cadence_devbox_1`（compose 建的那个，17 个挂载齐全）、以及历史遗留的 `devbox` / `devbox-old-20261027`（已停止）。**遗留容器先别删**，等第 2 步确认 compose 行为后再清。

## 0.5 换掉安装目录里的 compose.yaml（必做）

安装器**不覆盖**已存在的 `stack\compose.yaml`（为了保留你 `stack add` 自加的服务）——所以老安装目录拿不到新的 `container_name`，固定名不会自动生效：

```powershell
Copy-Item D:\testpodman\Cadence-devbox\devbox\compose.yaml D:\testpodman\cadence\stack\compose.yaml -Force
Select-String -Path D:\testpodman\cadence\stack\compose.yaml -Pattern "container_name"
#   → container_name: cadence_devbox
```

> 如果你用 `stack add` 追加过服务，改完记得检查 `stack\compose.yaml` 里那些自定义服务还在（复制是整文件覆盖，但源文件就是仓库最新版，不含你的自定义项 → **若你加过服务，请手工把自定义段落补回去**，或改用「只改一行 container_name」的方式）。

## 1. 改名（不杀容器内进程）

```powershell
podman rename cadence_devbox_1 cadence_devbox
podman ps -a --format "{{.Names}}  {{.Status}}"
```

容器里跑着的进程（config-server / authority-function）不受影响。

## 2. 确认 compose 用固定名、且不会新建第二个

```powershell
cd D:\testpodman\cadence\stack
podman-compose -f compose.yaml up -d devbox
podman ps -a --format "{{.Names}}  {{.Status}}"
```

| 现象 | 含义 | 处置 |
|---|---|---|
| 只有一个 `cadence_devbox`，Up | compose 认得被改名的容器 ✔ | 跳到第 4 步 |
| 报错「名字已被占用」 | compose 不认被改名的容器 | 走第 3 步 |
| 多出一个 `cadence_devbox_1` | 该版本 podman-compose 不支持 `container_name` | 走第 3 步 |

## 3. 退路：删旧容器、由 compose 重建（会杀掉容器内进程）

```powershell
podman rm -f cadence_devbox_1 cadence_devbox
podman-compose -f compose.yaml up -d devbox
```

`/workspace` 是宿主 bind mount → 仓库内容、Git 状态、工作区改动都不受影响；但容器内的进程会被杀，需要重新启动（config-server / authority-function 等）。

## 4. relay 通道重跑（重算映射表 + 刷新探活）

```powershell
cd D:\testpodman\Cadence-devbox
powershell -ExecutionPolicy Bypass -File .\devbox\relay.ps1 -InstallDir D:\testpodman\cadence
```

问答：保留已有序号（**回车**）→ 要修改的已有序号（**回车**）→ 新增（**回车**跳过）。

期望输出（16 条 OK）：

```text
[cadence-relay] mysql：容器 → 10.89.0.1:23306 OK
…
[cadence-relay] gitlab：容器 → 10.89.0.1:20080 OK
[cadence-relay] gitlab：容器 → 真实地址 198.18.0.2:80 OK（DNAT 生效）
```

> 本次还新增了「域名解析一致性核对」：已录域名的服务会拿当前解析结果与已录地址比对，不一致时告警（不阻断、不改写）。

## 5. 状态复核

```powershell
powershell -ExecutionPolicy Bypass -File .\devbox\relay.ps1 -InstallDir D:\testpodman\cadence -Status
```

期望：hop1 全 OK → VM 段每服务 `active` → DNAT 逐条 `OK` → hop3 容器 → 网关:vmPort 全 OK → hop4 远端可达。

## 6. 容器内 git 验证

```powershell
podman exec cadence_devbox bash -c "getent hosts gitlab.tianti.tg.unicom.local; timeout 5 bash -c 'echo > /dev/tcp/198.18.0.2/80' && echo CONNECT-OK"
```

期望 `198.18.0.2` + `CONNECT-OK`。然后：

```powershell
podman exec -it cadence_devbox bash
# 容器内：
cd /workspace/authority
git fetch origin --prune
git rev-list --left-right --count origin/dev_20261023...dev_20261023
```

## 7. 清理遗留容器（确认第 2 步没问题后再做）

```powershell
podman rm -f devbox devbox-old-20261027
podman ps -a --format "{{.Names}}  {{.Status}}"   # 确认只剩一个 devbox 容器
```

## 8. 安装器幂等性验证（可选，但建议做一次）

```powershell
powershell -ExecutionPolicy Bypass -File D:\testpodman\Cadence-devbox\devbox\install.ps1
```

期望：输出「已清理遗留 devbox 容器：…」，且结束后 `podman ps -a` 里**只有一行** devbox 容器（重跑安装器不再造出第二个）。

## 回滚

```powershell
git -C D:\testpodman\Cadence-devbox checkout -- devbox
podman rename cadence_devbox cadence_devbox_1
```

---

# 从零重测：两档，按你的需要选

## Level 1｜容器级重测（约 10 分钟，**推荐先做这个**）

不动数据卷、不动密钥、不动 `/workspace`——只把容器拆了重装，验证「安装器 + compose + 容器唯一性」这条链。

```powershell
# 1) 记录现状
podman ps -a --format "{{.Names}}  {{.Status}}"
podman volume ls --format "{{.Name}}" | Sort-Object

# 2) 拆掉所有 devbox 容器（卷保留！/workspace 是宿主 bind mount，不受影响）
podman rm -f cadence_devbox cadence_devbox_1 devbox devbox-old-20261027

# 3) 换掉安装目录的 compose.yaml（第 0.5 步，必做）

# 4) 重装（幂等：复用卷、保留 cadence-box.yaml 与 stack\.env）
powershell -ExecutionPolicy Bypass -File D:\testpodman\Cadence-devbox\devbox\install.ps1

# 5) 验收（见下表）
```

## Level 2｜全新安装目录级重测（约 30 分钟，测「首次安装」路径）

```powershell
powershell -ExecutionPolicy Bypass -File D:\testpodman\Cadence-devbox\devbox\install.ps1 `
  -InstallDir D:\testpodman\cadence-fresh
```

- 会要求重新填 `cadence-box.yaml`（密钥）→ 用你现有的那份复制过去即可。
- ⚠️ **卷名是固定的**（`cadence-claude` / `cadence-m2` / …），两套安装共用同一批卷 → 别让两套同时跑，否则 agent 登录态与 Maven 缓存互相覆盖。建议：Level 2 期间先 `podman stop cadence_devbox`，测完删掉 `D:\testpodman\cadence-fresh` 与它的容器。

## 验收清单（逐条打勾）

| # | 断言 | 命令 | 期望 |
|---|---|---|---|
| 1 | 只有一个 devbox 容器 | `podman ps -a --format "{{.Names}}" \| Select-String devbox` | 只出现 `cadence_devbox` 一行 |
| 2 | 名字是固定的 | `podman inspect cadence_devbox --format "{{.Name}}"` | `cadence_devbox` |
| 3 | 挂载齐全 | `podman inspect cadence_devbox --format "{{range .Mounts}}{{.Destination}}{{println}}{{end}}"` | 含 `/workspace`、`/cadence/stack`、`/cadence/auth.yaml`、`/var/run/docker.sock` + 13 个 `cadence-*` 卷 |
| 4 | 代码可见 | `podman exec cadence_devbox bash -c "ls /workspace"` | 能看到 `authority` 等仓库 |
| 5 | 仓库完好 | `podman exec cadence_devbox bash -c "cd /workspace/authority && git log --oneline -1 && git status --short"` | 提交正常、工作区符合预期 |
| 6 | 启动就绪 | `podman logs cadence_devbox \| Select-Object -Last 5` | 含「就绪」 |
| 7 | JDK 选择跨重建保留 | `podman exec cadence_devbox bash -c "jdk list"` | 仍是你选的主版本（状态卷持久） |
| 8 | agent 登录态保留 | `podman exec cadence_devbox bash -c "ls -d ~/.claude ~/.codex ~/.omp"` | 目录在、内容还在 |
| 9 | relay 通道全通 | `relay.ps1` setup（三个问句全回车） | 8 条「容器 → 桥接地址 OK」+ 8 条「容器 → 真实地址 OK（DNAT 生效）」 |
| 10 | relay 逐跳体检 | `relay.ps1 -Status` | hop1/hop2/hop3/hop4 全 OK；VM 段每服务 active；DNAT 逐条 OK |
| 11 | 容器内 git 可用 | 容器内 `git -C /workspace/authority fetch origin --prune` | 成功（`gitlab` 通道生效） |
| 12 | 安装器幂等 | 再跑一次 `install.ps1` | 结束仍只有一个 devbox 容器；不产生第二个 |

**快速判定**（一条命令看 1、2、12 三条）：
```powershell
podman ps -a --format "{{.Names}}" | Select-String devbox
```

