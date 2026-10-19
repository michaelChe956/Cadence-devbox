# Tasks

## 1. 端口自动顺延（对应「交互式配置与留痕」推导/顺延/越界场景）

- [x] 1.1 TDD 先红：更新 tests/devbox/test_relay.py 推导层测试——`Get-RelayPorts` 带占用表签名、空表旧锚点值不变、5672 越过 stack 得 15673/25673、同表二次 3306 得 13307/23307、占满 55535 后顺延越界 Die、`$stack` 清单（移入 Get-StackPortList）与 compose 一致、目录推导顺延后与 compose 零重叠、字面量钉 `+ 10000`/`55535`
- [x] 1.2 relay.ps1：`Get-StackPortList` 单一来源 + `Get-RelayPorts($remotePort, $taken)` 顺延实现（host/vm 同步 +1、hostPort ≤55535 越界 Die），删除撞栈 Die 与 Collect-Services 旧唯一性预检（保留项自撞改为占用表入表时 Die 指明配置损坏）

## 2. 分配顺序与交互修改（对应保留项/修改/顺序确定场景）

- [x] 2.1 TDD 先红：Collect-Services 行为测试——Read-Host 回放：全保留零变化（顺序与端口稳定）、参数+自定义双 3306 顺延、修改问答改址后端口不变、显式端口覆盖（vmPort=hostPort+10000）、显式端口撞 stack/撞他服 Die、修改序号不在保留列表 Die、目录 rabbitmq 顺延越过 stack
- [x] 2.2 relay.ps1：Collect-Services 三阶段重构（占用表→交互修改→参数/新增），修改问答按裁决提示语，`New-Service` 带 `$taken` 分配并回显实际端口（含顺延标记），keepIdx/editIdx 排序保证按清单顺序确定，result 原序拼装

## 3. `-List` 与 relay.json gateway（对应「配置清单查询」需求）

- [x] 3.1 TDD 先红：`-List` 行为测试——含 gateway 的表格输出（序号/服务名/远端/portproxy/容器连接地址）、无 relay.json Die「通道未建立」、legacy 无 gateway 时现取网关（fake podman network inspect 桩）、开关/提示语/gateway 落盘钉住
- [x] 3.2 relay.ps1：`[switch]$List` 参数 + 入口三开关互斥 + `Invoke-List`（优先 cfg.gateway，缺省现取）；setup 落盘追加 gateway 字段

## 4. 文档与收尾

- [x] 4.1 devbox/README.md §2.6：推导表加 rabbitmq 顺延行与多实例顺延说明（显式拒绝改为自动顺延）、`-List` 用法与示例输出、改址/改端口交互流程示例、relay.json gateway 说明
- [x] 4.2 同步 cadence/plans/ 实施计划文档（含 controller 统一执行的 pytest 验证命令清单），自查 PS 硬约束（UTF-8 BOM+LF、新增行 ≤100 字符、无 here-string、无 TcpClient 字面量）

## 5. Controller 验收（已完成）

- [x] 5.1 `python -m pytest tests/devbox` → **150 passed**（controller 实跑，含 VmSandbox 与 pwsh 行为组；含修复 Int64/Int32 探针失配后复跑）
- [x] 5.2 `bash -n devbox/relay-vm.sh` 通过（未动）与 `openspec validate improve-relay-service-config` 通过（controller 实跑）
- [x] 5.3 维护者 Windows 真机确认通过（2026-10-19）：`-List`、双 3306 顺延 setup、改址重跑、`-Status`/`-Teardown` 回归
