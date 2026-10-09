# Spec Delta

## Purpose

让容器内应用经宿主 VPN 访问用户自己的远端中间件（MySQL/Redis 起步，可扩展）：一条命令建立、查询、拆除双向中继通道，幂等可重跑，全程留痕可还原。

## ADDED Requirements

### Requirement: 一键建立通道

脚本 MUST 按服务表（mysql/redis 起步，端口约定错开 stack 自带中间件）在两侧建立中继：Windows 侧 netsh portproxy（仅监听 127.0.0.1，不暴露局域网）+ VM 侧 socat 中继（监听 0.0.0.0 供容器访问），并 MUST 在完成后输出容器内应用应使用的连接地址（宿主 IP + 中继端口）。

#### Scenario: 首次拉通 mysql 与 redis
- **WHEN** 用户在已安装 devbox 的 Windows 宿主以管理员运行 setup，提供 mysql=10.129.12.75:3306、redis=10.129.20.31:6379
- **THEN** 两侧中继建成，脚本从 devbox 容器内对 23306/26379 做 TCP 探活全部通过，并打印形如 `mysql → 192.168.50.206:23306`、`redis → 192.168.50.206:26379` 的连接信息

#### Scenario: 重复运行幂等
- **WHEN** 用户对同一配置再次运行 setup
- **THEN** 不产生重复的 portproxy 规则、中继进程或开机钩子，最终状态与首次运行后一致

### Requirement: 交互式配置与留痕

首跑 MUST 以问答收集中间件远端地址并保存到安装目录的配置文件；重跑 MUST 回显已有值并允许直接回车保留或输入新值；MUST 同时支持命令行参数覆盖问答。

#### Scenario: 修改远端地址
- **WHEN** 用户重跑 setup 并将 mysql 远端地址改为新值
- **THEN** 配置文件更新，portproxy 目标与中继随之刷新，旧目标不再残留

### Requirement: 通道开机自恢复

宿主或 WSL 重启后，VM 侧中继 MUST 无需人工干预自动恢复监听；Windows 侧 portproxy 规则本身随系统持久。

#### Scenario: 重启后通道仍可用
- **WHEN** 用户重启 Windows 后（VPN 重新连上）从容器内再次探活中继端口
- **THEN** 探活通过，无需重跑 setup

### Requirement: 状态查询

`-Status` MUST 逐跳报告每个服务的通道状态：Windows portproxy 规则是否存在、VM 中继是否监听、容器内是否可达、远端是否可达（区分"VPN 未连"与"中继故障"）。

#### Scenario: VPN 断线时查询
- **WHEN** VPN 掉线后运行 `-Status`
- **THEN** 报告指出远端不可达并提示先连 VPN，而不是笼统的连接失败

### Requirement: 对称拆除

`-Teardown` MUST 移除本脚本创建的全部痕迹（portproxy 规则、VM 内脚本、开机钩子、socat 软件包、配置文件），且 MUST 不删除配置留痕之外的宿主网络配置（如用户或 IT 手工创建的 portproxy）。

#### Scenario: 拆除后回到初始状态
- **WHEN** 用户运行 `-Teardown`
- **THEN** portproxy 中仅剩非本脚本创建的规则，VM 内无残留脚本与监听进程，再次 `-Status` 报告通道不存在

### Requirement: 平台守卫

在非 Windows 或 podman machine 非 WSL 后端的环境下运行时，脚本 MUST 在动手前明确拒绝并说明当前支持范围，MUST 不做任何半途修改。

#### Scenario: Hyper-V 后端被拒
- **WHEN** podman machine 后端为 hyperv 时运行 setup
- **THEN** 脚本报错说明仅支持 WSL 后端，且未创建任何 portproxy 或 VM 内文件

### Requirement: 前置自检

setup 与 status MUST 在执行前检测远端地址从宿主是否可达；不可达时 MUST 以明确指引终止（提示连接 VPN），MUST 不留下半配置状态。

#### Scenario: 未连 VPN 时运行 setup
- **WHEN** 远端地址从宿主探测不通时运行 setup
- **THEN** 脚本提示先连 VPN 并退出，两侧均无残留改动
