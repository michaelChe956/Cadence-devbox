# Design: improve-relay-service-config

## Context

现行 `devbox/relay.ps1` 的 `Get-RelayPorts($remotePort)` 只按远端端口静态推导（hostPort=远端+10000、vmPort=远端+20000）：推导撞本地 stack 发布端口时 Die「撞点」（唯一实际撞点：远端 5672 → 15672）；`Collect-Services` 末尾的端口唯一性预检 Die「端口 X 同时属于 A 与 B」。两处 Die 都源于"端口由远端端口单值决定"的旧模型，多实例同远端端口场景（用户 2026-10-19 裁决）无法通过。relay.json 已是唯一留痕（version=1，services 动态数组）；`-Status` 是现有唯一查询入口且必做逐跳探活。测试侧已有成熟模式：AST 抽取 relay.ps1 真实函数后在桩环境（fake cmd/fake podman）跑行为回归（tests/devbox/test_relay.py），本设计沿用该模式扩展。

## Goals / Non-Goals

**Goals:**
- 端口分配从"单值静态推导"改为"占用感知的顺序分配 + 自动顺延"，vmPort 恒为 hostPort+10000
- 顺延同时越过已分配服务端口与本地 stack 发布端口（清单与 compose.yaml 一致，测试钉住）
- 分配顺序确定（保留→参数→新增，修改项按原序重分配），重跑幂等稳定
- `-List` 只读查询与交互修改（改址/改端口）补齐配置管理闭环
- 存量 relay.json 零迁移：保留项沿用旧端口；顺延只影响新分配

**Non-Goals:**
- 不做端口记忆协商或跨进程端口租约（单脚本顺序分配即可，无并发场景）
- 不改 relay-vm.sh：端口经 env 服务表（name vmport bindaddr hostport）传入，天然兼容
- 不支持域名远端、不自动改用户应用配置（既有边界不变）
- 不做 `-List` 的探活/漂移检测（`-Status` 职责）

## Decisions

### D1. 顺延算法：host/vm 同步 +1，占用集合单一来源

`Get-RelayPorts($remotePort, $taken)`：候选 hostPort 从 remotePort+10000 起，`vmPort = hostPort + 10000`；hostPort 或 vmPort 任一命中占用表 `$taken`（本次分配的端口→服务名哈希表）或 stack 发布端口清单时 hostPort+1 重试。远端端口 1-45535 前置校验保留；顺延后 hostPort>55535 即 Die（此时 vmPort 必 >65535，两条上界等价，报错同时说明）。

- 为何同步 +1 而非只挪撞的那一侧：保持 vmPort=hostPort+10000 不变量，容器侧连接地址可从 hostPort 推出，README/文档只有一条规则。
- 为何 stack 清单进顺延集合而非保留显式拒绝：唯一撞点 5672→15672 的"远端须换端口"要求把负担推给用户，而 15673/25673 完全可用（用户裁决）。stack 清单抽成 `Get-StackPortList` 函数（内含 `$stack = @(...)`，正则钉住与 compose 一致），供推导与显式覆盖校验共用——AST 抽取测试模式下函数必须自包含（script 作用域变量在 harness 里不可见），单函数即单一来源。

### D2. 分配顺序与三阶段流水（顺序确定的关键）

`Collect-Services` 重构为三阶段：
1. **占用表建立**：未修改的保留项端口先入 `$taken`（它们的端口是既成事实）。将被参数覆盖（-MySQL/-Redis 同名）或将被交互修改的保留项不入表——旧端口让位重分配。入表时发现 relay.json 自身端口重复（仅可能来自手改损坏）→ Die 指明双方，替代原唯一性预检的职责。
2. **交互修改**：修改问答（保留问答之后、新增之前）选出序号；逐项问新地址（回车保留原值）与新本地端口（回车=`New-Service` 走顺延推导；数字=显式覆盖，校验 1-55535/不撞 stack/不撞 `$taken`，vmPort=hostPort+10000 一并校验）。修改项按原序处理，新端口随即入表。
3. **参数项与新增项**：现有 -MySQL/-Redis 替换/追加与目录/自定义新增逻辑不变，仅改为带 `$taken` 分配并回显实际端口（含"自动顺延"标记）。

最终 result 按原相对顺序拼装（保留项原位、参数同名原位替换、新增追加），relay.json 顺序保持；全量重跑（全保留/不修改/不新增）时零端口漂移。

- 替代方案（否决）：冲突时全局重新编号所有服务——违反"保留项沿用旧端口"契约，存量 portproxy/VM 实例全要重建。
- 替代方案（否决）：修改项沿用旧端口优先——"回车=自动推导"语义（用户裁决原文）要求按新占用表现状分配；无冲突时推导值本就等于旧值，常见路径零变化。

### D3. relay.json 增加 gateway 字段（-List 免探测）

setup 落盘 `@{ version = 1; gateway = $gw; services = $services }`。`-List` 优先读 `cfg.gateway`；旧版文件无此字段时退回 `Get-BridgeGateway` 现取（网络缺失则沿用其既有 Die 文案）。增量字段对现有读方（teardown/status/relay-vm env 渲染）无影响。备选（否决）：`-List` 每次现取网关——依赖 stack 在线，违背"只读配置"定位。

### D4. `-List` 与交互修改的输出契约

表格列：序号/服务名/远端地址/Windows portproxy/容器连接地址，单行 ≤100 字符；无 relay.json Die「通道未建立，先运行 setup」（与 -Status 同款文案前缀）。入口互斥检查扩展为 -Status/-Teardown/-List 三选一。修改问答提示语按裁决原文：`要修改的已有序号（回车跳过，逗号分隔）`、`新远端地址（回车保留 原IP:端口）`、`新本地端口（回车=自动推导；直接输入数字覆盖）`；修改序号须已在保留列表中，否则 Die 说明。

### D5. 测试策略（沿用 AST 抽取 + 桩环境）

- 推导层：`Get-RelayPorts`/`Get-StackPortList` 抽取后直测——空占用表得旧锚点值（13306/23306 等，兼容不动）；5672 得 15673/25673；同表二次 3306 得 13307/23307；占满 55535 后 45535 顺延 Die。
- Collect-Services 层：harness 内用同名函数遮蔽（shadow）`Read-Host` 回放预置答案序列，全流程断言 result 的顺序/端口/地址；场景含全保留零变化、双 3306 顺延、改址端口不变、显式端口覆盖与冲突 Die。
- `-List`：抽取 `Invoke-List` + 写临时 relay.json（含/不含 gateway）断言表格字段与 Die 路径；fake podman 补 `network inspect` 分支支撑 legacy 网关现取。
- 钉住测试更新：`$stack` 清单与 compose 一致（正则改指 `Get-StackPortList` 内的 `$stack`）；推导字面量钉 `+ 10000` 与 `55535` 上界；目录推导 vs compose 改为"顺延后零重叠"（Python 侧模拟顺延）。

## Risks / Trade-offs

- 顺延改变"端口=远端+10000"的可预测性 → 输出与 relay.json 明示实际端口 + 顺延标记，`-List` 随时可查；接受。
- 修改项自动推导在极端占用下可能离开旧端口 → 语义即"按现状重新分配"，且无冲突时值不变；文档写明"想锁死端口请显式输入数字"。
- relay.json 增字段 → 纯增量，旧读方忽略未知字段；teardown/status 不受影响。
- WinPS 5.1 兼容：全部用 hashtable/@() 数组与既有风格，无 here-string、无类初始化语法；保持 UTF-8 BOM + LF、用户可见行 ≤100 字符、无 TcpClient 字面量（终端管控红线，测试钉住）。

## Migration Plan

存量 relay.json 不迁移：保留项端口原样沿用；首次重跑 setup 后自动补 gateway 字段。旧测试中钉住"+20000 字面量"与"5672 显式拒绝"的断言随契约更新（TDD 先红）。

## Open Questions

（无——规则已由用户 2026-10-19 裁决确认，不再开放）
