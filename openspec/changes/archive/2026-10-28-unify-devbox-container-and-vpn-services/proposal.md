# Proposal

## Why

devbox 容器有两条互不清理的创建路径（安装器手工 `podman run --name devbox`，与 compose 的 devbox 服务），宿主上可能同时存在两个挂载同一 `/workspace` 的 devbox 容器——容器内并发 git 操作会撞 `index.lock`。同时 relay 脚本探测容器只判「存在」不判「在运行」，命中已停止容器时报出「所有通道全断」的假故障，把排查方向从「容器没起」带偏到「网络坏了」。

另一处是发现率：VPN 侧 HTTP 服务（如 GitLab）必须走同一条 relay 通道，但手册只讲远端中间件、问句也不告诉用户 IP 该从哪查，用户只能靠 git bundle 搬运绕路。

## What Changes

- **BREAKING**：devbox 容器名由 `devbox` / `cadence_devbox_1` 统一固定为 `cadence_devbox`；用户既有命令中的容器名需相应替换。
- devbox 容器的创建路径收口为 compose 一条路径：安装器不再手工 `podman run` 创建 devbox 容器，改为经 compose 创建。
- 安装器启动前清理历史遗留容器名（`devbox`、`cadence_devbox_1`），保证同名 devbox 容器至多一个，并在启动后断言。
- relay 脚本的容器探测改为按「在运行」判定；容器不存在、未运行、存在多个 devbox 容器，分别给出可执行提示，不再把未运行容器当成探测目标。
- relay 地址问句加入取值指引（`nslookup <域名>`，并说明 VPN/域名型服务常见为 `198.18.x.x` 假 IP）。
- relay 在 setup 与 `-Status` 时对已录域名的服务做解析一致性核对：解析结果与映射表 IP 不一致时明确报告两者并提示重跑 setup；解析失败与「远端不可达」区分报告。
- 手册增补「VPN 侧 HTTP 服务（GitLab 等）自助开通」指引与「假 IP 变化」排障路径。

## Capabilities

### New Capabilities
- `devbox-container`: devbox 容器的唯一创建路径、固定命名与运行态探测契约。

### Modified Capabilities
- `remote-middleware-relay`: 交互式配置新增「地址取值自助指引」与「域名解析一致性核对」两项要求。

## Impact

- `devbox/compose.yaml`：devbox 服务新增固定容器名。
- `devbox/install.ps1`：删除手工创建 devbox 容器的分支，改为经 compose 创建；新增遗留容器清理与唯一性断言；就绪检测与提示文案跟随新名。
- `devbox/relay.ps1`：容器探测逻辑（setup 与 `-Status` 两处）；地址问句提示语；域名解析核对。
- `devbox/README.md`：§2.3/§2.4/§2.6/§3/§3.1 的容器名与启动路径；新增 VPN 侧 HTTP 服务小节。
- `devbox/stack/docker-compose.no-sock.yml`：仅覆盖 volumes，固定容器名自动继承（不改内容，加测试钉住）。
- `tests/devbox/`：新增/调整结构与行为测试。
- 宿主一次性动作：现有容器改名或重建为 `cadence_devbox`（不破坏 `/workspace` 里的仓库）。
