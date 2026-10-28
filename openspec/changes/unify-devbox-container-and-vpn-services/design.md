# Design

## Context

- devbox 容器有两条创建路径：安装器 `devbox/install.ps1` 里的 `podman run -d --name devbox …`（5 个挂载），与 `devbox/compose.yaml` 的 devbox 服务（17 个挂载，多 12 个 agent 登录态与语言缓存卷）。README §2.4 明写「中间件走 podman-compose，devbox 单独 `podman run`」。
- 这条分家有历史原因：Windows 盘符路径在 compose 短语法挂载下会被按 `:` 切坏（README §2.7 第 6 条），当年只能手工 `podman run`。compose.yaml 现已改用长语法 `type: bind` 绕开该坑，compose 路线实际已可用，但安装器与文档未收口。
- 两条路线互不清理：安装器只 `podman rm -f devbox`，删不掉 compose 默认名的容器；compose 也不管手工容器。宿主上因此可能同时存在两个挂载同一 `/workspace` 的 devbox 容器。
- relay 脚本 `Get-DevboxContainer` 用 `podman container inspect <name>` 判存在——对已停止的容器同样成功——因此可能选中僵尸容器，随后所有 `podman exec` 探针空跑，输出「所有服务 FAIL」，把排查方向误导为链路故障。
- VPN 侧 HTTP 服务（如 GitLab）的域名在宿主 DNS 上被解析为 `198.18.x.x` 假 IP，只有宿主发起的连接能走隧道；这类服务与远端中间件走同一条 relay 通道，但手册未提及、问句也不告诉用户 IP 从哪查。

## Goals / Non-Goals

**Goals:**
- 任何时刻至多一个 devbox 容器；创建路径唯一（compose）；名字恒定。
- 容器探测只认运行态容器，异常状态给出可执行提示。
- 用户能照手册自助开通 VPN 侧 HTTP 服务通道。
- 域名解析结果与已录地址不一致时主动提示，而非让用户撞「git fetch 神秘超时」。

**Non-Goals:**
- 不做「问句只收域名、脚本自动解析并持续跟随」的自动解析能力（假 IP 实测稳定，收益不足以支撑契约变更）。
- 不改 compose 服务的挂载集合、不动容器镜像与 entrypoint。
- 不改 relay 通道的端口推导规则、不改 VM 侧中继实现。

## Decisions

**D1｜容器名固定为 `cadence_devbox`，经 compose `container_name` 实现。**
备选：接受 compose 默认名 `cadence_devbox_1`（零额外配置，但用户命令要写全名）；保留 `devbox`（命令短，但用户明确否决）。选 `cadence_devbox` 是用户决定，语义上也比 `devbox` 更能自解释。

**D2｜保留 compose 路线，删除安装器的手工创建分支（不反向）。**
compose 侧挂载更全（多 12 个 agent 登录态与 `.m2`/`.gradle`/`.npm` 等缓存卷），手工路线只有 5 个。反向做法（删 compose 的 devbox 服务）会同时破坏 `docker-compose.no-sock.yml` 变体与这些状态卷，得不偿失。

**D3｜探测按运行态判定，且「多容器」显式报告。**
`podman container inspect` 的成功与否不能代表运行态，需读 `.State.Running`。假故障（对着僵尸容器探活得出「全断」）比真故障更误导排查，因此「容器不存在 / 未运行 / 存在多个」三态必须分别报出可执行的处置动作。

**D4｜域名解析核对只告警，不自动改写用户已录地址。**
用户可能故意填非解析地址（直连 IP、CDN 出口）。自动改写会掩盖用户意图并让通道指向未知的地址。告警 + 提示重跑即可。

**D5｜问句给指引，不做自动解析。**
假 IP 实测稳定：2026-10-21 与 2026-10-27 两次解析 `gitlab.tianti.tg.unicom.local` 均为 `198.18.0.2`（中间跨宿主重启）。自动跟随的收益只是省一次抄写，却要改问句契约并重写一批既有断言。故只在提示语与手册层解决「IP 从哪查」。

## Risks / Trade-offs

- **podman-compose 对 `container_name` 的支持未在真机验证** → 退路：compose 创建后由安装器执行一次 `podman rename` 到固定名（已是目标名则跳过，幂等）。
- **compose 是否认得被 rename 的容器未验证**（它靠 label 定位，rename 不改 label，理论上认得） → 退路：删除旧容器后由 compose 重建（会重启容器内进程）。
- **容器重建会杀掉容器内进程**（如 config-server / authority-function） → 迁移步骤优先用 rename（不杀进程），重建仅作退路。
- **手册中散落旧容器名**（§2.3/§2.4/§2.6/§3/§3.1） → 用文档一致性测试钉住：手册与脚本中不得再出现历史容器名。
- **域名解析核对新增一次 DNS 查询**（每个带域名的服务一次） → 仅在 setup 与 `-Status` 时执行，不在热路径上。

## Migration Plan

1. 宿主上把现有容器改名为固定名（不杀进程）：`podman rename cadence_devbox_1 cadence_devbox`。
2. 执行 compose 启动 devbox 服务，确认没有新建第二个容器（`podman ps -a` 只有一行 devbox 容器）。
3. 若 compose 因名字冲突失败：删除旧容器后由 compose 重建（会重启容器内进程，需手工重启容器内应用）。
4. 重跑一次 relay setup（重算映射表并刷新探活）。

回滚：还原安装器 / compose / 手册改动，容器改名回原名即可；`/workspace` 是宿主 bind mount，仓库内容与 Git 状态不受任何一步影响。

## Open Questions

- podman-compose 的 `container_name` 支持与 rename 后的容器识别，均属真机验证项；验证前按上述退路执行，不阻塞其余工作。
