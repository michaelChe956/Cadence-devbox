# Proposal

## Why

用户真实场景是**多套同类型远端环境并存**（如 mysql 与自定义 mysql_log 都是远端 3306）。现行端口推导只看远端端口（hostPort=远端+10000、vmPort=远端+20000），两个 3306 实例会撞同一本地端口，Collect-Services 的端口唯一性预检直接 Die「端口 13306 同时属于 mysql 与 mysql_log」，用户无法自助配置（2026-10-19 用户裁决）。同时，推导撞本地 stack 发布端口（远端 rabbitmq 5672 → 15672）靠显式拒绝处理，体验割裂——本可自动顺延解决。另外，查看现有配置要跑会探活的 `-Status`，仅想看配置/改一个地址时太重。

## What Changes

- **端口自动顺延**：推导端口被占用（已分配服务端口或本地 stack 发布端口）时 hostPort/vmPort 同步 +1 顺延（vmPort 恒为 hostPort+10000），如 mysql→13306/23306、第二个 3306 实例→13307/23307；远端 rabbitmq 5672 自动越过 15672 得 15673/25673，不再显式拒绝。
- **分配顺序确定**：按服务在清单中的顺序分配（保留项在前、参数项次之、新增项最后），结果确定、重跑稳定。
- **越界保护**：远端端口范围仍为 1-45535；顺延后 hostPort 须 ≤55535（vmPort 恒 ≤65535），越界 Die 并说明。
- **保留项契约不变**：沿用 relay.json 旧端口，不重新推导；新分配只影响新增/参数新增/交互修改项。
- **新增 `-List`**：只读 relay.json 打印配置表格（序号、服务名、远端 IP:端口、Windows portproxy、容器连接地址），不探活；无 relay.json 时 Die「通道未建立」。
- **交互修改**：保留序号问答之后新增「要修改的已有序号」，逐项改远端地址与本地端口（回车=自动推导/数字覆盖，覆盖时校验范围、撞栈、撞已分配端口）。
- **relay.json 增强**：setup 落盘时记录网关地址（gateway 字段，`-List` 免探测直接打印容器连接地址；旧文件无此字段时现取）。
- 同步更新 `devbox/README.md` §2.6（推导表、顺延规则、`-List` 用法、改址/改端口交互流程）。

**非目标**：不做跨 relay.json 重启的端口记忆协商；不改 VM 侧 relay-vm.sh 的监听/回收逻辑（端口由 env 表传入，天然兼容）；不支持域名远端；不自动修改用户应用配置。

## Capabilities

### New Capabilities

（无——全部落在既有 capability 上）

### Modified Capabilities

- `remote-middleware-relay`: 端口推导需求从「单远端端口确定推导+撞栈显式拒绝」改为「占用感知的自动顺延（含越过 stack 端口）+ 越界 Die」；交互式配置与留痕需求新增「交互修改保留项（改址/改端口）」场景并修订撞栈场景；新增「配置清单查询（-List）」需求。

## Impact

- 修改：`devbox/relay.ps1`（Get-RelayPorts 顺延、Collect-Services 修改问答与分配顺序、Invoke-List、入口互斥、relay.json 落盘 gateway）
- 修改：`tests/devbox/test_relay.py`（顺延/越界/越过 stack/顺序稳定/-List/改址改端口的行为与钉住测试）
- 修改：`devbox/README.md` §2.6（推导表与说明、`-List`、交互修改流程）
- 不动：`devbox/relay-vm.sh`、`devbox/compose.yaml`、stack 命令体系
- 兼容性：存量 relay.json 无需迁移（保留项沿用旧端口）；旧 relay.json 无 gateway 字段时 `-List` 现取网关
