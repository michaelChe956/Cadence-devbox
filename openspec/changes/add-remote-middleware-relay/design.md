# Design: add-remote-middleware-relay

## Context

链路已于 2026-10-08/09 在维护者 Windows 真机（Podman Desktop + WSL 后端 + mirrored 网络 + 宿主 VPN）完整验证：容器 → VM socat(23306) → WSL mirrored 回环 127.0.0.1:13306 → Windows netsh portproxy → VPN → 远端 MySQL，端到端 `SELECT 1` 通过。实测约束（设计输入）：

- aTrust VPN 静默丢弃一切 NAT 转发流量，仅 Windows 本机进程发出的流量走隧道（2026-09-28/10-09 排查定论）——任何方案最后一跳必须由 Windows 本机发起，portproxy 是唯一入口
- mirrored 模式下 VM 的 `127.0.0.1` 可达 Windows 的 loopback 监听（实测 M_OK）；但镜像模式不把打到 Windows 自有 IP 的连接转交给 Windows 进程（一律 refused，实测）——容器侧必须由 VM 自建监听做中继
- VM 的 systemd 可用，但非交互调用受限：一次性 `wsl -u root systemctl ...` 报 `Transport endpoint is not connected`，交互式进入后正常（2026-10-09 实测）；`podman machine ssh` 路径可行性待实施时验证
- PowerShell → wsl → sudo 多层引号会拆坏长复合命令（实测静默失败）；install.ps1 已确立"cp 文件进 VM 再 ssh 执行"的规避模式
- 用户终端粘贴超约 120 字符的行会被折行截断；面向用户输出的命令每行 ≤100 字符，PowerShell 块用 @() 数组而非 here-string（实测教训）

## Goals / Non-Goals

见 proposal 非目标。设计层面补充边界：不修改 compose 模板与镜像；不进入 stack 命令体系（这是宿主侧网络工具，不是容器内中间件目录的一部分）；不管理 VPN 客户端本身。

## Decisions

### D1. 两跳中继 + 固定端口表

每个服务两跳：Windows `netsh portproxy`（仅监听 `127.0.0.1`，经 mirrored 回环被 VM 使用，不暴露局域网）+ VM 内 socat（仅绑定 podman 网桥网关地址——只有容器网段可达；网关地址经 `podman network inspect` 动态发现，不硬编码）。

服务端口表（v1）：

| 服务 | portproxy 监听 | VM 中继监听 | 远端（示例） |
|---|---|---|---|
| mysql | 127.0.0.1:13306 | 网关:23306 | VPN 网段 IP:3306 |
| redis | 127.0.0.1:16379 | 网关:26379 | VPN 网段 IP:6379 |

备选否决：单跳（VM 直连远端）——实测不通；中继做成 compose 服务（`network_mode: host`）——依赖 podman-compose 的 host 网络行为且把宿主网络工具混入容器 stack 体系，持久化也弱于 boot 钩子。

### D2. 宿主脚本 + VM 脚本，文件传递

`devbox/relay.ps1`（用户入口：setup 默认 / `-Status` / `-Teardown`；交互问答；参数 `-MySQL host:port` `-Redis host:port` 可覆盖）负责：平台守卫、前置自检、portproxy 管理、podman 网桥网关发现、把 `devbox/relay-vm.sh` 与渲染出的服务表 env 文件 `podman machine cp` 进 VM 后 `podman machine ssh sudo bash` 执行、最后从 devbox 容器探活并打印连接信息。

`devbox/relay-vm.sh`（子命令 setup/status/teardown，ShellCheck 覆盖）负责 VM 内一切：dnf 装 socat、写 `/etc/cadence-relay.env`、管理 systemd 服务（或兜底 wsl.conf 钩子）、按配置端口启停监听。全程不使用内嵌长命令字符串（规避引号拆坏，install.ps1 同款教训）。

### D3. 持久化：systemd 优先，boot 钩子兜底

首选 VM 内 systemd 服务（cadence-relay 前缀命名，`Restart=always` + `RestartSec=5`：网桥未就绪时绑定失败自动重试直到成功——维护者手搭链已验证此自愈模式）。实施时先验证 `podman machine ssh sudo systemctl enable --now` 是否可用；不可用则回退 `/etc/wsl.conf` [boot] 钩子（启动脚本带重试循环）。VM 内文件布局：`/etc/cadence-relay.env`（服务表：每行 `vm端口=127.0.0.1:portproxy端口`）+ 按表拉起的监听进程 + 日志 `/var/log/cadence-relay.log`。重启逻辑幂等：先按配置端口 pkill 旧监听再拉起；wsl.conf 管理（兜底路径）：移除含 cadence-relay-boot 的既有行后追加，保留其他 section。

### D4. 配置留痕与拆除安全

配置存 `<InstallDir>/relay.json`（与 cadence-box.yaml 同目录，进安装器写好的本地 .gitignore 防误提交）：`{"version":1,"services":[{"name":"mysql","remoteHost":"10.x.x.x","remotePort":3306,"hostPort":13306,"vmPort":23306},...]}`。`-Teardown` 只删：配置中记录的端口对应的 portproxy 规则、`cadence-relay` 前缀的 VM 文件与 boot 钩子行、socat 包、relay.json 本体。用户/IT 手工创建的其他 portproxy 规则不动（按端口精确匹配）。teardown 前回显将删除项并二次确认。

### D5. 前置自检与顺序

setup 执行顺序固定：平台守卫（`podman machine inspect` Provider 必须 wsl 且 Running）→ 管理员检测（非管理员时自动 `-Verb RunAs` 重启自身，失败则 Die）→ 远端可达性探测（`TcpClient` 3 秒超时，逐服务）→ 任何失败在改动前终止，不留半配置。`-Status` 同样逐跳探测：portproxy 规则存在性（解析 `netsh interface portproxy show all`）→ VM 监听（`ss -tln` 按端口）→ 容器内 `/dev/tcp` 探活 → 宿主到远端可达性；远端不通而中继正常时报"VPN 未连"。

### D6. 连接地址：podman 网桥网关

应用连接 `网桥网关:vm端口`（如 `10.89.0.1:23306`）——网关属 podman 网络对象，持久存在、与宿主 DHCP 无关，地址稳定（网络重建才可能漂移；setup/status 每次动态发现并打印，漂移后重跑即重绑）。不依赖 Windows 网卡 IP，无静态绑定要求。

## Risks / Trade-offs

- [WSL/Podman 更新改变 mirrored 回环行为] → `-Status` 逐跳定位把"哪一跳坏了"变成一句话结论；README 记录依赖前提
- [VPN 掉线导致通道不可用] → 属预期；前置自检与 status 的差异化提示让用户能自行判断
- [podman 网络重建导致网桥网关漂移] → 每次运行动态发现网关（podman network inspect），不硬编码；漂移后重跑 setup 重绑
- [弱技术用户误删/误拆] → teardown 二次确认 + 回显清单
- [端口与 stack 自带中间件冲突] → 固定错开（13306/16379 vs 3306/6379），测试断言无重叠
- [存量手搭中继占用端口] → setup 前置检测监听占用，识别占用者并引导拆除或由脚本接管（维护者机的 socat-mysql-relay 即实例）

## Migration Plan

纯新增文件 + 两处小改（install.ps1 尾部提示行、README §2.6 章节）。维护者机存在手搭同链路（socat-mysql-relay）：验收前先整体还原，由脚本全新搭建验证。其他用户机若存在手搭中继，setup 冲突检测引导迁移。回滚：删除新增文件；用户侧跑 `-Teardown` 清环境。

## Open Questions

（无）
