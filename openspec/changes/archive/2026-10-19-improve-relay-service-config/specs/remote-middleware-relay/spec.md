# Spec Delta

## MODIFIED Requirements

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

## ADDED Requirements

### Requirement: 配置清单查询

`-List` MUST 只读 relay.json 打印配置表格：每个服务一行，含序号、服务名、远端 IP:端口、Windows portproxy（127.0.0.1:hostPort）与容器连接地址（网关:vmPort），输出行 MUST ≤100 字符；MUST NOT 做任何探活（探活是 `-Status` 的职责）。relay.json 不存在时 MUST 显式报错「通道未建立」。容器连接地址的网关 MUST 优先取 relay.json 记录的 setup 时网关，旧版 relay.json 无此字段时 MUST 现场解析 podman 网桥网关。`-List` 与 `-Status`/`-Teardown` MUST 互斥。

#### Scenario: 列出已配置通道
- **WHEN** relay.json 记录 mysql（10.129.12.75:3306 → host 13306 / vm 23306）与网关 10.89.0.1 时运行 `-List`
- **THEN** 打印表格行含 `mysql`、`10.129.12.75:3306`、`127.0.0.1:13306`、`10.89.0.1:23306`，全程无任何端口探活

#### Scenario: 未建立通道时查询
- **WHEN** 安装目录无 relay.json 时运行 `-List`
- **THEN** 显式报错含「通道未建立」并以非零码退出，不做任何改动
