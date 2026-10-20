# Proposal

## Why

容器内应用（尤其从配置中心 `ibos-config-server` 拉配置的 Spring 工程）里写的是公司内网**真实地址**（如 `10.129.12.75:3306`），而容器直连这类地址不通——VPN（aTrust）丢弃一切 NAT 转发流量，只有 Windows 宿主发起的连接被放行。既有 relay 通道已经解决「容器 → 宿主 → VPN → 远端」，但它要求应用把地址换成**桥接地址**（`10.89.0.1:23306`）。对配置驱动的工程，这意味着要么改配置中心数据、要么改工程配置——两者都不可接受（配置是团队共用的，工程代码也不该为本地环境改）。

## What Changes

- **透明地址映射（核心能力）**：容器内进程按**真实 IP:端口**连接时，VM 侧用一条 nft DNAT 规则把目的地址无状态改写为 relay 通道的桥接监听地址，改写后完全复用既有 relay 链路。应用侧**零改动**：不改工程代码、不改配置中心数据、不改容器镜像/compose、不改启动参数。
- **DNAT 目标恒等于 socat 实际监听地址**：规则目标取自 relay 服务表的 bindaddr/vmPort（而非现查网桥网关），保证「改写后有人接」；映射表与实际监听不一致时告警并按实际监听写规则，不静默改指别处。
- **规则固化**：nft 表是内核态、VM 重启即丢，因此由 VM 侧 systemd oneshot 单元在开机时重放重建；无 systemd 时复用既有 wsl.conf [boot] 监督循环重放。容器重启完全不影响（规则在 VM 根 netns，不在容器 netns）。除用户主动改配置外，规则不因重启消失或改变。
- **地址问句扩展为「IP:端口[ 域名]」**：域名是同一问句的可选第二段，不二次询问；回车保持原值；`-`/`none` 显式清空已录域名。域名仅落盘留痕与诊断（relay.json 的可选 `host` 键），**不参与 DNAT 规则生成**（规则只按 IP 匹配）。
- **单一入口单配置面**：用户只运行 `relay.ps1`、只配置 `relay.json` 一处；DNAT 映射表（安装目录 `stack/data/relay-map.env` 与 VM `/etc/cadence-relay-dnat.env`）全部是派生物，由 setup 自动生成下发，用户不编辑。
- **纯 nft 实现**：目标 VM（podman machine 的 Fedora 44）不带 `iptables`，故用 nft 自建表（`table ip cadence_dnat`，base chain `prerouting`，`priority dstnat - 1`，早于 netavark 的标准 dstnat 链，顺序确定）。规则先写 `.nft` 文件（LF 行尾）再 `nft -f`，不用命令行 `nft add chain`（其含空格分号的规格经 Windows→bash 引号层会被吃掉）。
- **对称拆除**：`relay.ps1 -Teardown` 连同 DNAT 规则、VM 侧单元与映射表一并清除。
- 同步更新 `devbox/README.md` §2.6（新增「透明地址映射」子节 + 问答流程与 setup 运行顺序同步）。

**非目标**：不做域名解析异常的 `/etc/hosts` 钉住（延后项，触发条件：容器内域名解析不到真实 IP 或解析到错 IP）；不做 UDP 服务代理；不自动修改用户应用配置或配置中心数据；不给容器加 `NET_ADMIN`（不改 compose/Dockerfile/entrypoint）；不支持 IPv6 目标地址。

## Capabilities

### New Capabilities

（无——全部落在既有 capability 上）

### Modified Capabilities

- `remote-middleware-relay`: 新增「透明地址映射」需求（容器内按真实地址直连、目标恒等于实际监听、规则固化与重启恢复、单入口单配置、地址问句带域名、对称拆除）；「交互式配置与留痕」需求新增域名录入与清空语义；「状态查询」需求新增 DNAT 段诊断；「通道开机自恢复」需求扩展到 DNAT 规则。

## Impact

- 修改：`devbox/relay.ps1`（域名录入、映射表生成与下发、relay.json 增可选 `host` 键）
- 修改：`devbox/relay-vm.sh`（`read_dnat_env`/`dnat_apply`/`dnat_flush`/`dnat_status`/`write_dnat_unit`、子命令 `dnat-apply`/`dnat-flush`、systemd 单元与 boot 兜底重放、teardown 对称清理）
- 修改：`devbox/README.md` §2.6
- 修改：`tests/devbox/test_relay.py`（域名录入、映射表生成、生成端校验用例）
- 新增：`tests/devbox/test_relay_dnat.py`（VM 侧 nft 行为 POSIX 沙箱测试）
- 不动：`devbox/compose.yaml`、`devbox/Dockerfile`、`devbox/entrypoint.sh`、`devbox/stack/`、用户工程与配置中心数据
- 兼容性：存量 relay.json 无需迁移（无 `host` 键 = 未录入）；既有 relay 通道行为不变（DNAT 是附加能力，其失败只告警、不回滚已就绪的通道）
- 依赖前提：目标 VM 需有 `nft`（podman machine 的 Fedora 基底自带）；不要求也不建议安装 `iptables`（会随 podman machine 升级丢失）
