# remote-middleware-relay Specification

## Purpose
让容器内应用经宿主 VPN 访问用户自己的远端中间件（MySQL/Redis 起步，可扩展）：一条命令建立、查询、拆除双向中继通道，幂等可重跑，全程留痕可还原。

## Requirements

### Requirement: 一键建立通道

脚本 MUST 按服务表（mysql/redis 起步，端口约定错开 stack 自带中间件）在两侧建立中继：Windows 侧 netsh portproxy（仅监听 127.0.0.1，不暴露局域网）+ VM 侧 socat 中继（仅绑定 podman 网桥网关地址，容器网段可达），并 MUST 在完成后输出容器内应用应使用的连接地址（podman 网桥网关 IP + 中继端口）。

#### Scenario: 首次拉通 mysql 与 redis
- **WHEN** 用户在已安装 devbox 的 Windows 宿主以管理员运行 setup，提供 mysql=10.129.12.75:3306、redis=10.129.20.31:6379
- **THEN** 两侧中继建成，脚本从 devbox 容器内对网桥网关 23306/26379 做 TCP 探活全部通过，并打印形如 `mysql → 10.89.0.1:23306`、`redis → 10.89.0.1:26379` 的连接信息（podman 网桥网关，动态发现）

#### Scenario: 单服务故障互不影响
- **WHEN** 两个中继服务已建成且某服务实例异常退出（如 redis 中继被 kill）
- **THEN** 该服务实例在 5 秒内自动重启恢复监听，另一服务（mysql）的监听进程不受影响（每服务独立 systemd 实例）

#### Scenario: 网关漂移后提示重跑
- **WHEN** podman 网络重建导致网桥网关地址变化后运行 status
- **THEN** 报告 bind 地址不在本机网卡并明确提示重跑 setup 重绑，而不是笼统的监听失败

#### Scenario: 宿主 IP 变化不影响通道
- **WHEN** Windows 网卡 IP 因 DHCP 发生变化
- **THEN** 中继、portproxy 与容器内连接地址均不受影响，无需任何操作

### Requirement: 存量通道冲突检测

setup MUST 在建立前检测配置端口上的既有监听；发现非本脚本创建的占用（如手工搭建的中继服务）时 MUST 报告占用者并提供处理路径（先拆除或由本脚本接管重建），MUST 不静默失败或与占用者抢端口。

#### Scenario: 端口被手搭中继占用
- **WHEN** VM 内网桥网关的 23306 已被名为 socat-mysql-relay 的手搭 systemd 服务监听时运行 setup
- **THEN** 脚本报告占用者与处理路径，用户确认拆除后 setup 接管该端口并正常完成

#### Scenario: --force 仅精确接管完全一致的监听
- **WHEN** 配置端口的占用者为命令行参数（bind+port+target）与服务表行完全一致、且 `/proc/<pid>/exe` 为 socat 本体的前台 socat，用户带 `--force` 运行 setup
- **THEN** 仅该 socat 进程被接管（kill 前重核身份，随后由本方案 systemd 实例重建监听），其余进程与 unit 一律不动；exe 不符的 argv 伪装者、或归属任何其它 systemd unit 的占用者，`--force` 也拒绝（exit 3），什么都不动

### Requirement: 交互式配置与留痕

首跑 MUST 以问答收集中间件远端地址并保存到安装目录的配置文件；重跑 MUST 回显已有通道（编号）并允许保留/删除/修改/新增；MUST 同时支持命令行参数覆盖问答。服务清单 MUST 为动态长度：新增服务从目录（mysql/redis/kafka/zookeeper/rabbitmq）多选或走自定义（服务名+IP:端口）。

通道端口 MUST 按远端端口推导：hostPort=远端端口+10000、vmPort=hostPort+10000（即未顺延时与存量 +20000 值一致）。候选端口（hostPort 或 vmPort 任一）命中已分配服务端口或本地 stack 发布端口时，两者 MUST 同步顺延 +1 重试直至无冲突（vmPort 恒为 hostPort+10000）。分配 MUST 按服务在清单中的顺序进行（保留项在前、参数项次之、新增项最后），同一输入序列的结果 MUST 确定且重跑稳定，relay.json 服务顺序 MUST 保持。远端端口合法范围 MUST 为 1-45535；顺延后 hostPort MUST ≤55535（vmPort 恒 ≤65535），越界 MUST 显式报错说明并终止。

未修改的保留项 MUST 沿用 relay.json 旧端口，不重新推导；新分配只影响新增项、参数项与交互修改项。setup 完成时输出与 relay.json MUST 明示每个服务实际分配的 host/vm 端口。重跑问答 MUST 在保留序号之后、新增之前支持交互修改保留项：逐项询问新远端地址（回车=保留原值）与新本地端口（回车=按当前占用自动顺延推导；输入数字则显式覆盖），显式覆盖 MUST 校验端口范围（1-55535）、不撞本地 stack 发布端口、不与其他服务端口重复，violation 时 MUST 显式报错说明冲突双方。

#### Scenario: 修改远端地址
- **WHEN** 用户重跑 setup 并将 mysql 远端地址改为新值（远端端口随之变化）
- **THEN** 配置文件更新，portproxy 目标与中继随之刷新，旧端口的 portproxy 规则与 VM 监听一并清除，旧目标不再残留

#### Scenario: 多实例同名远端端口自动顺延
- **WHEN** 清单已有 mysql（远端 3306 → host 13306 / vm 23306），用户再新增自定义服务 mysql_log（同为远端 3306）
- **THEN** mysql_log 自动顺延得到 host 13307 / vm 23307（vmPort=hostPort+10000），不报错、不占用 mysql 的端口，两者并存

#### Scenario: 推导自动越过本地 stack 发布端口
- **WHEN** 新增远端 rabbitmq 地址端口为 5672（推导 hostPort=15672 与本地 stack 发布的 15672 重叠）
- **THEN** 自动顺延越过 15672 得到 host 15673 / vm 25673，正常建立通道，不显式拒绝

#### Scenario: 顺延越界显式拒绝
- **WHEN** 远端端口 45535 的推导端口（host 55535 / vm 65535）已被占用，顺延将使 vmPort 超过 65535
- **THEN** setup 显式报错说明顺延越界（hostPort 须 ≤55535），不创建任何 portproxy 或 VM 内文件

#### Scenario: 保留项沿用旧端口且重跑稳定
- **WHEN** relay.json 已有多服务（含顺延后的端口），用户重跑 setup 全部保留、不新增不修改
- **THEN** 各服务端口与 relay.json 记录完全一致（不重新推导），服务顺序保持不变

#### Scenario: 新增目录服务并按远端端口推导
- **WHEN** 用户重跑 setup，保留已有 mysql/redis 并新增目录项 kafka=10.129.30.8:9092
- **THEN** kafka 通道端口按远端端口推导（hostPort=19092、vmPort=29092），relay.json 记录三项服务，连接信息打印 kafka → 网关:29092

#### Scenario: 目录外自定义服务
- **WHEN** 用户以自定义项新增服务 es7=10.129.30.9:9200（服务名限字母数字与 _-）
- **THEN** 以服务名 es7 建立通道（hostPort=19200、vmPort=29200），VM 侧 systemd 实例名为 cadence-relay@es7

#### Scenario: 交互修改保留项改地址
- **WHEN** 已有 mysql 通道（10.1.1.1:3306 → host 13306），用户重跑 setup 保留全部并在修改问答中对 mysql 输入新地址 10.2.2.2:3306、本地端口回车
- **THEN** relay.json 记录新地址且端口不变（仍 host 13306 / vm 23306），portproxy 目标刷新

#### Scenario: 交互指定本地端口覆盖推导
- **WHEN** 用户在修改问答中对某服务输入数字本地端口 14000
- **THEN** 该服务使用 hostPort=14000、vmPort=24000（恒 hostPort+10000），relay.json 与输出同步

#### Scenario: 推导端口撞本地 stack 时显式拒绝
- **WHEN** 用户在修改问答中显式指定本地端口为本地 stack 已发布端口（如 3306，替代推导值）
- **THEN** setup 显式报错说明撞 stack 发布端口并终止，不创建任何 portproxy 或 VM 内文件（推导路径已改为自动顺延，显式指定仍拒绝）

#### Scenario: 指定端口与其他服务冲突显式拒绝
- **WHEN** 用户在修改问答中输入的本地端口（或其 vmPort=hostPort+10000）与其他服务已分配端口重复
- **THEN** setup 显式报错说明与哪项服务冲突并终止，不创建任何 portproxy 或 VM 内文件

#### Scenario: 重跑 setup 删除服务
- **WHEN** 已有 mysql/redis 通道，重跑 setup 时仅保留 mysql（redis 被删除）
- **THEN** Windows 侧 redis 的 portproxy 规则被删除；VM 侧 systemd 实例被本次 setup 回收（停监听、清 wants 链接），无 systemd 的 boot 兜底模式下其 socat 监听由 setup 按旧服务表对账回收（三参核验，未知不动）；mysql 通道不受影响

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

`-Teardown` MUST 移除本脚本创建的全部痕迹（portproxy 规则、VM 内 systemd 单元（模板+实例）与脚本、开机钩子、服务表与日志、配置文件），且 MUST 不删除配置留痕之外的宿主网络配置（如用户或 IT 手工创建的 portproxy）与本方案边界之外的资源（未知监听进程、socat 软件包——2026-10-14 修订：包可能被其它用途共享，保留并在拆除输出中说明）。

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

### Requirement: 配置清单查询

`-List` MUST 只读 relay.json 打印配置表格：每个服务一行，含序号、服务名、远端 IP:端口、Windows portproxy（127.0.0.1:hostPort）与容器连接地址（网关:vmPort），输出行 MUST ≤100 字符；MUST NOT 做任何探活（探活是 `-Status` 的职责）。relay.json 不存在时 MUST 显式报错「通道未建立」。容器连接地址的网关 MUST 优先取 relay.json 记录的 setup 时网关，旧版 relay.json 无此字段时 MUST 现场解析 podman 网桥网关。`-List` 与 `-Status`/`-Teardown` MUST 互斥。

#### Scenario: 列出已配置通道
- **WHEN** relay.json 记录 mysql（10.129.12.75:3306 → host 13306 / vm 23306）与网关 10.89.0.1 时运行 `-List`
- **THEN** 打印表格行含 `mysql`、`10.129.12.75:3306`、`127.0.0.1:13306`、`10.89.0.1:23306`，全程无任何端口探活

#### Scenario: 未建立通道时查询
- **WHEN** 安装目录无 relay.json 时运行 `-List`
- **THEN** 显式报错含「通道未建立」并以非零码退出，不做任何改动
