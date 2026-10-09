# Design: add-remote-middleware-relay

## Context

链路已于 2026-10-08/09 在维护者 Windows 真机（Podman Desktop + WSL 后端 + mirrored 网络 + 宿主 VPN）完整验证：容器 → VM socat(23306) → WSL mirrored 回环 127.0.0.1:13306 → Windows netsh portproxy → VPN → 远端 MySQL，端到端 `SELECT 1` 通过。实测约束（设计输入）：

- VM 直连 VPN 网段不通（mirrored 对 WiFi 网卡的已知缺陷，实测 FAIL）
- mirrored 模式下 VM 的 `127.0.0.1` 可达 Windows 的 loopback 监听（实测 M_OK），但容器无法直接使用宿主回环——必须有一跳 VM 内中继
- podman machine 的 WSL 发行版未运行 systemd（`systemctl` 报错，实测）
- PowerShell → wsl → sudo 多层引号会拆坏长复合命令（实测静默失败）；install.ps1 已确立"cp 文件进 VM 再 ssh 执行"的规避模式

## Goals / Non-Goals

见 proposal 非目标。设计层面补充边界：不修改 compose 模板与镜像；不进入 stack 命令体系（这是宿主侧网络工具，不是容器内中间件目录的一部分）；不管理 VPN 客户端本身。

## Decisions

### D1. 两跳中继 + 固定端口表

每个服务两跳：Windows `netsh portproxy`（仅监听 `127.0.0.1`，经 mirrored 回环被 VM 使用，不暴露局域网）+ VM 内 socat（监听 `0.0.0.0` 供容器经默认路由到达）。

服务端口表（v1）：

| 服务 | portproxy 监听 | VM 中继监听 | 远端（示例） |
|---|---|---|---|
| mysql | 127.0.0.1:13306 | 0.0.0.0:23306 | VPN 网段 IP:3306 |
| redis | 127.0.0.1:16379 | 0.0.0.0:26379 | VPN 网段 IP:6379 |

备选否决：单跳（VM 直连远端）——实测不通；中继做成 compose 服务（`network_mode: host`）——依赖 podman-compose 的 host 网络行为且把宿主网络工具混入容器 stack 体系，持久化也弱于 boot 钩子。

### D2. 宿主脚本 + VM 脚本，文件传递

`devbox/relay.ps1`（用户入口：setup 默认 / `-Status` / `-Teardown`；交互问答；参数 `-MySQL host:port` `-Redis host:port` 可覆盖）负责：平台守卫、前置自检、portproxy 管理、宿主 IP 探测、把 `devbox/relay-vm.sh` 与渲染出的服务表 env 文件 `podman machine cp` 进 VM 后 `podman machine ssh sudo bash` 执行、最后从 devbox 容器探活并打印连接信息。

`devbox/relay-vm.sh`（子命令 setup/status/teardown，ShellCheck 覆盖）负责 VM 内一切：dnf 装 socat、写中继脚本、管理 `/etc/wsl.conf` 开机钩子、按配置端口启停监听。全程不使用内嵌长命令字符串（规避引号拆坏，install.ps1 同款教训）。

### D3. 持久化：wsl.conf boot 钩子（非 systemd）

VM 内文件布局（`cadence-relay` 前缀即所有权标记）：`/usr/local/bin/cadence-relay-boot`（nohup 拉起 + 日志到 `/var/log/cadence-relay.log`）、`/etc/cadence-relay.env`（服务表：每行 `vm端口=127.0.0.1:portproxy端口`）、每服务一个监听进程。重启逻辑：boot 脚本先按配置端口 pkill 旧监听再拉起（幂等）。`/etc/wsl.conf` 的管理方式：移除含 `cadence-relay-boot` 的既有行后追加新行，保留其他 section 不动。

### D4. 配置留痕与拆除安全

配置存 `<InstallDir>/relay.json`（与 cadence-box.yaml 同目录，进安装器写好的本地 .gitignore 防误提交）：`{"version":1,"services":[{"name":"mysql","remoteHost":"10.x.x.x","remotePort":3306,"hostPort":13306,"vmPort":23306},...]}`。`-Teardown` 只删：配置中记录的端口对应的 portproxy 规则、`cadence-relay` 前缀的 VM 文件与 boot 钩子行、socat 包、relay.json 本体。用户/IT 手工创建的其他 portproxy 规则不动（按端口精确匹配）。teardown 前回显将删除项并二次确认。

### D5. 前置自检与顺序

setup 执行顺序固定：平台守卫（`podman machine inspect` Provider 必须 wsl 且 Running）→ 管理员检测（非管理员时自动 `-Verb RunAs` 重启自身，失败则 Die）→ 远端可达性探测（`TcpClient` 3 秒超时，逐服务）→ 任何失败在改动前终止，不留半配置。`-Status` 同样逐跳探测：portproxy 规则存在性（解析 `netsh interface portproxy show all`）→ VM 监听（`ss -tln` 按端口）→ 容器内 `/dev/tcp` 探活 → 宿主到远端可达性；远端不通而中继正常时报"VPN 未连"。

### D6. 宿主 IP 与应用连接信息

连接地址 = 宿主活动网卡的 IPv4（默认路由接口）。中继本身 IP 无关（portproxy 绑回环、socat 绑 0.0.0.0），IP 变化只影响应用配置里的地址——setup/status 均打印当前应使用的 `IP:vm端口`，README 建议 DHCP 静态绑定。

## Risks / Trade-offs

- [WSL/Podman 更新改变 mirrored 回环行为] → `-Status` 逐跳定位把"哪一跳坏了"变成一句话结论；README 记录依赖前提
- [VPN 掉线导致通道不可用] → 属预期；前置自检与 status 的差异化提示让用户能自行判断
- [DHCP 改变宿主 IP] → 通道不自愈但也不自毁；重跑 setup 重新验证并打印新地址
- [弱技术用户误删/误拆] → teardown 二次确认 + 回显清单
- [端口与 stack 自带中间件冲突] → 固定错开（13306/16379 vs 3306/6379），测试断言无重叠

## Migration Plan

纯新增文件 + 两处小改（install.ps1 尾部提示行、README §2.6 章节）。发布即生效，无存量迁移。回滚：删除新增文件；用户侧跑 `-Teardown` 清环境。

## Open Questions

（无）
