# Proposal

## Why

devbox 的目标用户（弱技术 Windows 业务开发/测试）把应用跑在容器里，但不少团队的真实中间件是"用户自己的、仅从 Windows 宿主经 VPN 可达的远端 MySQL/Redis"。容器直连 VPN 网段在 podman machine（WSL mirrored）下实测不通，当前没有任何受支持的通道，用户无法自助打通。2026-10-08 维护者在 Windows 真机完整验证了一条可行链路（容器 → VM socat → mirrored 回环 → Windows portproxy → VPN → 远端），需要产品化为一键脚本。

## What Changes

- 新增一键拉通脚本 `devbox/relay.ps1`（宿主侧）与 `devbox/relay-vm.sh`（VM 侧，经 `podman machine cp`/`ssh` 执行，遵循 install.ps1 已确立的"拷文件进 VM"模式，回避多层引号转义）
- 链路（每个服务两跳本地中继）：`容器 → VM socat(23306/26379) → WSL mirrored 回环 127.0.0.1 → Windows netsh portproxy(13306/16379) → VPN → 远端`
- 首跑交互问答收集中间件远端地址（mysql/redis 起步，服务表可扩展），存安装目录配置文件；重跑幂等，回显已有值可修改；Windows IP 变化后重跑即刷新
- 子命令：默认 setup、`-Status`（查链路各跳状态）、`-Teardown`（对称还原，清掉本脚本创建的全部痕迹）
- `install.ps1` 完成提示追加一行指引（有远端中间件需求才跑，不打扰其他用户）
- `devbox/README.md` 新增使用章节：运行方式、连接串含义、VPN 在线前提、路由器 DHCP 静态绑定建议

**非目标**：不支持 macOS 与 Hyper-V 后端（明确报错提示）；不做 VPN 内部域名解析（v1 用 IP）；不自动修改用户应用配置（只输出连接信息）；不替代 stack 自带中间件（端口错开并存）。

## Capabilities

### New Capabilities

- `remote-middleware-relay`: 容器内应用访问宿主 VPN 侧远端中间件的一键通道管理——setup/status/teardown 全生命周期、幂等重跑、配置留痕。

### Modified Capabilities

（无——不改动现有 spec 的需求）

## Impact

- 新增：`devbox/relay.ps1`、`devbox/relay-vm.sh`
- 修改：`devbox/install.ps1`（尾部提示一行）、`devbox/README.md`（新章节）、`tests/devbox/`（新增结构与脚本静态检查）
- 运行前提：Windows 管理员权限（portproxy 写入）、VM 可联网（dnf 装 socat）、WSL mirrored 网络（Podman Desktop 默认）、VPN 客户端在线
- 不动：compose 模板、devbox 镜像、stack 命令体系
