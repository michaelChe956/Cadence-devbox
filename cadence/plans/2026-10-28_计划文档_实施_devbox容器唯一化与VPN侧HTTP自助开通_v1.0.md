# 实施计划：devbox 容器唯一化与 VPN 侧 HTTP 自助开通

- **契约**：`openspec/changes/unify-devbox-container-and-vpn-services/`（proposal / specs / design / tasks）
- **范围**：只展开契约，不重新定义范围、架构与验收
- **规格**：容器名恒为 `cadence_devbox`；devbox 容器只经 compose 创建；探测只认运行态容器；地址问句给 nslookup 取值指引；已录域名做解析一致性核对（只告警不改写）

## 1. 容器命名与创建路径收口

**文件**：`devbox/compose.yaml`、`devbox/install.ps1`、`tests/devbox/test_install_flow.py`（新增）

| 步 | 改动 | 验证 |
|---|---|---|
| 1.1 | 先写失败测试：compose devbox 服务带 `container_name: cadence_devbox`；no-sock 覆盖文件不含 `container_name`；安装器无 `--name devbox`、无 `podman run -d`；安装器含 `podman-compose … up -d devbox`；含遗留名 `'devbox'`/`'cadence_devbox_1'` 清理；含「至多一个」断言 | 新用例先红 |
| 1.2 | `compose.yaml` devbox 服务加 `container_name` | 用例转绿 |
| 1.3 | `install.ps1`：删手工 `podman run` 分支 → `podman-compose -f stack/compose.yaml up -d devbox`；启动前清理遗留容器名并打印清理了哪些；启动后统计 devbox 类容器数，>1 显式报错；就绪检测与第 5 步提示统一用 `cadence_devbox` | 用例转绿 |
| 1.4 | 确认 `docker-compose.no-sock.yml` 只覆盖 volumes → 容器名继承 | 用例覆盖 |

## 2. relay 容器探测按运行态

**文件**：`devbox/relay.ps1`、`tests/devbox/test_relay.py`

| 步 | 改动 | 验证 |
|---|---|---|
| 2.1 | 先写失败测试：relay.ps1 含 `.State.Running` 运行态判定；不再用 `podman container inspect devbox` 存在性判定；pwsh 桩环境下三种状态各自的行为（运行中→选中；仅有停止的→提示未运行；多个→提示多容器） | 新用例先红（pwsh 桩用例无 pwsh 时 skip） |
| 2.2 | 新增 `Get-ContainerNames` / `Get-ContainerRunning` / `Resolve-DevboxContainer` / `Get-DevboxContainer`：枚举全部容器 → 取名字含 devbox 的 → 再按运行态收敛；多余容器告警；未运行/不存在分别给可执行提示 | 用例转绿 |
| 2.3 | `Invoke-Status` 的容器判定改为复用 `Resolve-DevboxContainer`（缺容器只告警、不终止状态查询） | 用例转绿 |

## 3. 地址取值指引与域名解析核对

**文件**：`devbox/relay.ps1`、`tests/devbox/test_relay.py`

| 步 | 改动 | 验证 |
|---|---|---|
| 3.1 | 先写失败测试：问句提示语含 `nslookup`；relay.ps1 含 `Resolve-DnsName` 解析核对；提示语与手册含假 IP 说明 | 新用例先红 |
| 3.2 | 问句提示语加取值指引（自定义与目录两处）；新增 `Resolve-DomainIp` | 用例转绿 |
| 3.3 | setup 收集服务后、动手改宿主前做解析核对：不一致→报两个值并提示重跑 setup；解析失败→报解析失败并提示确认 VPN；**不阻断**通道建立、不改写用户地址 | 用例转绿 |
| 3.4 | `-Status` 同样核对并报告 | 用例转绿 |

## 4. 手册

**文件**：`devbox/README.md`、`tests/devbox/test_docs_consistency.py`

| 步 | 改动 | 验证 |
|---|---|---|
| 4.1 | §2.3/§2.4/§2.6/§3/§3.1 容器名统一 `cadence_devbox`；§2.4 删手工 `podman run` 路线改 compose 一条路径；修正「另外 16 个卷」的数量表述 | 文档一致性用例转绿 |
| 4.2 | §2.6 新增「VPN 侧 HTTP 服务（GitLab / Artifactory）」小节：nslookup 查 IP → 新增 custom 服务 → 容器内验证 → 假 IP 变化的排障 | 文档一致性用例覆盖 |
| 4.3 | §2.6 前提清单与排障表：容器名改为固定名；补「容器未运行 / 存在多个容器」两条 | 文档一致性用例转绿 |

## 5. 统一验证与真机清单

- 本机：`uv run --with pytest --with pyyaml python -m pytest tests/devbox -q` 全绿；`shellcheck` 与 `bash -n devbox/relay-vm.sh` 保持全绿（本次未改 relay-vm.sh）
- 真机（Windows 宿主，用户执行）：compose `container_name` 生效 → 现有容器改名 → compose 复用不新建第二个 → 安装器重跑不产生第二个 → relay setup 探活全通 → 容器内 `git fetch` 可用

## 风险

- podman-compose 的 `container_name` 支持未验证 → 退路：compose 建完由安装器 `podman rename` 到固定名（幂等）
- compose 是否认得被 rename 的容器未验证 → 退路：删旧容器重建（会重启容器内进程）
- 容器重建会杀掉容器内进程 → 迁移优先用改名