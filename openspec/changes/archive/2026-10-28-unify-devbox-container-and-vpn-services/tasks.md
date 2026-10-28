# Tasks

## 1. 容器命名与创建路径收口

- [x] 1.1 先写失败测试：compose 的 devbox 服务 MUST 带固定容器名 `cadence_devbox`；no-sock 覆盖文件 MUST NOT 自带 `container_name`；安装器 MUST NOT 手工 `podman run` 创建容器（断言只看可执行代码，注释提及不算）；安装器 MUST 经 `podman-compose … up -d devbox` 创建；MUST 清理遗留名 `'devbox'`/`'cadence_devbox_1'`；MUST 有「至多一个」断言（`tests/devbox/test_install_flow.py` 新增，先红后绿）
- [x] 1.2 `devbox/compose.yaml`：devbox 服务加 `container_name: cadence_devbox` + 由来注释
- [x] 1.3 `devbox/install.ps1`：删手工 `podman run` 分支 → `podman-compose -f stack/compose.yaml up -d devbox`；启动前清理遗留容器名并打印；启动后断言 devbox 类容器至多一个（>1 显式报错）；compose 不支持 `container_name` 时用实际容器名兜底并告警；就绪检测与第 5 步提示统一用 `cadence_devbox`
- [x] 1.4 `devbox/stack/docker-compose.no-sock.yml`：仅覆盖 volumes → 容器名自动继承（测试钉住该文件不含 `container_name`）

## 2. relay 容器探测按运行态

- [x] 2.1 先写失败测试：relay.ps1 MUST 以 `.State.Running` 判定运行态，MUST NOT 再用 `podman container inspect devbox` 存在性判定；pwsh 桩环境下三态行为（运行中→选中；仅有停止的→提示未运行；多个→报告并继续探测固定名）（新增用例先红）
- [x] 2.2 `devbox/relay.ps1`：新增 `Get-ContainerNames` / `Get-ContainerRunning` / `Resolve-DevboxContainer` / 重写 `Get-DevboxContainer`——枚举容器 → 取名字含 devbox 的 → 按运行态收敛；多余容器告警；未运行/不存在分别给可执行提示
- [x] 2.3 `devbox/relay.ps1` `Invoke-Status`：容器判定复用 `Resolve-DevboxContainer`（缺容器只告警、不终止状态查询）

## 3. 地址取值指引与域名解析核对

- [x] 3.1 先写失败测试：问句提示语 MUST 含 `nslookup` 取值指引；relay.ps1 MUST 含 `Resolve-DnsName` 解析核对（新增用例先红）
- [x] 3.2 `devbox/relay.ps1`：新增 `Write-AddressHint`（自定义/目录/修改三处问句前提示）与 `Resolve-DomainIp`；提示语说明 VPN 下域名型服务常解析为 `198.18.x.x` 假 IP
- [x] 3.3 `devbox/relay.ps1`：setup 收集服务后、动手改宿主前做解析核对——不一致报两个值并提示重跑 setup；解析失败报解析失败（与「远端不可达」区分）；**不阻断**通道建立、**不改写**用户已录地址；`-Status` 同样核对并报告

## 4. 手册

- [x] 4.1 `devbox/README.md` §2.3/§2.4/§2.6/§3/§3.1：容器名统一 `cadence_devbox`；§2.4 删手工 `podman run` 路线改 compose 一条路径并说明「至多一个容器」；§2.4 挂载清单改为 12 个 agent/缓存卷（原文「另外 16 个」数字不准）；§2.5 Linux rootless 改用 `containers.conf` 的 `userns = "keep-id"`
- [x] 4.2 `devbox/README.md` §2.6 新增「VPN 侧 HTTP 服务（GitLab / Artifactory）」小节：`nslookup` 查 IP → relay 新增 custom 服务 → 容器内 `git fetch` 三步；域名+IP 同填、Host 头不变、假 IP 变化的排障、凭据用 credential helper
- [x] 4.3 `devbox/README.md` §2.6 前提清单与排障表：容器名改固定名；补「容器未运行 / 容器不存在 / 多个容器 / 域名解析不一致 / 域名解析失败」五条处置

## 5. 统一验证

- [x] 5.1 `uv run --with pytest --with pyyaml python -m pytest tests/devbox -q` → **170 passed, 52 skipped**（跳过项为需要 pwsh 的行为用例，本机无 pwsh，须在 Windows 补跑）
- [x] 5.2 `shellcheck devbox/relay-vm.sh` 与 `bash -n devbox/relay-vm.sh` 通过（本次未改 relay-vm.sh）
- [x] 5.3 Windows 真机验证清单已产出：`cadence/plans/2026-10-28_Windows真机操作步骤_devbox容器统一_v1.0.md`（改名 → compose 复用确认 → relay 重跑 → 容器内 git 验证 → 遗留容器清理 → 安装器幂等）

## Workflow follow-up

- [ ] Windows 宿主执行上面的真机验证清单（用户执行）：重点确认 `podman-compose` 对 `container_name` 的支持，以及 compose 是否认得被 rename 的容器（两条都有退路，见清单第 2/3 步）
- [ ] Windows 宿主补跑 pwsh 行为用例（本机无 pwsh，52 个用例 skip）：`uv run --with pytest --with pyyaml python -m pytest tests/devbox -q`（Windows 侧装 pwsh 后不带 `--with` 依赖）
- [ ] 真机验证通过后再 `openspec archive unify-devbox-container-and-vpn-services`
