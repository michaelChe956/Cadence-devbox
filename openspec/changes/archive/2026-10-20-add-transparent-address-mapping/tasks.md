# Tasks

## 1. 地址问句支持可选域名（对应「地址问句支持可选域名」全部场景）

- [x] 1.1 TDD 先红：新增行为用例——同一次问句录入「IP:端口 域名」并贯通到服务对象（4 个录入点：交互修改/custom 新增/目录新增/-MySQL·-Redis 覆盖）、回车保持地址与域名原值、`-`/`none` 显式清空域名、非法域名当场报错、存量配置无域名键仍正常（向后兼容）。验证：`pytest tests/devbox/test_relay.py -k "host or domain or collect"` 全绿
- [x] 1.2 实现：`relay.ps1` 新增域名合法性校验与地址问句拆分函数（4 个录入点全部改路由）；服务对象增加可选域名属性；配置落盘仅在域名非空时写该键（存量文件无该键等价未录入）。验证：1.1 的用例全绿
- [x] 1.3 修复实现期发现的真机阻断缺陷：局部变量/形参名与 PowerShell 内置只读自动变量冲突（同名即抛"只读常量"错误，WinPS 5.1 上会让 setup 直接失败）。验证：装 pwsh 后 `pytest tests/devbox/` 201 passed（此前 45 个需 pwsh 的用例被 skip 掩盖）

## 2. 映射表生成与分发（对应「单一入口与配置面」场景）

- [x] 2.1 TDD 先红：新增映射表生成用例——段数与顺序、可选域名占位、域名小写归一化、无 BOM、纯 LF、原子落盘不留临时文件、重复 (真实IP,真实端口) 拒绝、非法服务名/IP/端口/域名拒绝、生成数据源为内存中的服务清单而非已落盘配置。验证：`pytest tests/devbox/test_relay.py -k dnat_map` 全绿
- [x] 2.2 实现：`relay.ps1` 新增映射表渲染与原子落盘（ASCII 无 BOM、纯 LF），在 VM 侧 setup 之前生成并下发（安装目录一份 + VM 一份），失败走既有回滚（清 portproxy）。验证：2.1 的用例全绿，且顺序断言证明映射表先于 VM setup 就位

## 3. VM 侧改写规则应用（对应「透明地址映射」场景）

- [x] 3.1 TDD 先红：新增 VM 侧 POSIX 沙箱用例（自建 nft/ip/ss/systemctl/dnf 桩，真实执行脚本）——规则内容与目标取自服务表实际监听地址、规则文件纯 LF、部分服务名缺失时跳过该条并报错而其余照写、全部缺失时失败退出、映射表与服务表不一致时按服务表写并告警、监听地址不在网卡时告警仍写、清除只删自有表且幂等。验证：`pytest tests/devbox/test_relay_dnat.py` 全绿
- [x] 3.2 实现：`relay-vm.sh` 新增映射表解析（含段数判定与误喂服务表防护、IP/端口/唯一性/域名校验）、规则应用（自建 nft 表 + 显式 priority，规则先写文件再加载，不使用命令行建链）、规则清除、状态诊断；新增子命令接线与文件头用法说明。验证：3.1 的用例全绿；`bash -n` 与 `shellcheck` 无告警
- [x] 3.3 接入 setup：VM 侧 setup 末尾应用改写规则，失败只告警不回滚已就绪的 relay 通道；状态查询增加改写诊断段（规则存在性、逐条齐全、目标一致性、监听地址是否在本机网卡、映射文件缺失提示）。验证：3.1 的用例全绿

## 4. 规则固化与对称拆除（对应「改写规则固化与重启恢复」「单一入口与配置面」拆除场景）

- [x] 4.1 TDD 先红：新增持久化用例——setup 写出单元文件并 enable、单元登记进快照/回滚集合（回滚后不残留 enabled 单元）、teardown 清除单元与映射文件并先停单元再清规则、无 systemd 时开机监督循环每轮幂等重放。验证：`pytest tests/devbox/test_relay_dnat.py` 全绿
- [x] 4.2 实现：`relay-vm.sh` 新增单元写出（oneshot + RemainAfterExit + ExecStart/ExecStop 均带解释器）与 enable 流程、登记进既有快照/回滚/新增集合、开机监督循环重放、拆除时对称清理。验证：4.1 的用例全绿

## 5. 文档

- [x] 5.1 `devbox/README.md` §2.6 新增「透明地址映射」子节（解决什么问题、怎么工作、开启方式、地址问句带域名、重启自动恢复、怎么确认生效、怎么撤销、真机踩过的坑），并同步「setup 运行顺序」与「问答流程」描述。验证：文档中的命令形态与实现实际字符串逐条一致；代码围栏成对

## 6. Controller 验收（已完成）

- [x] 6.1 `pytest tests/devbox/` → **201 passed**（controller 实跑，装 pwsh 7.4.6 后含全部 45 个需 pwsh 的用例）
- [x] 6.2 `bash -n devbox/relay-vm.sh` 通过；`shellcheck -S warning devbox/relay-vm.sh` rc=0 无告警（controller 实跑）
- [x] 6.3 维护者 Windows 真机确认通过（2026-10-20）：`nft list table ip cadence_dnat` 显示 3 条规则（目标全部为服务表 bindaddr+vmPort）；容器内 `mysql -h 10.129.12.75 -P 3306 -u <user> -p -e "SELECT 1"` 返回 1（协议级 + 认证通过）；redis 返回 NOAUTH（即连通、差密码）；`podman machine stop/start` 后规则自动重建（用户确认"重启了之后测了也没问题"）
- [x] 6.4 代码审查（k3-reviewer）结论已处理，遗留项记入设计文档风险节

## Workflow follow-up

- 延后项（触发后再排期，不在本 change 范围）：容器内域名解析异常时的 `/etc/hosts` 钉住（触发条件：容器内域名解析不到真实 IP 或解析到错 IP）——见设计文档 §6.2 步骤 2
- 延后项：UDP 服务代理、IPv6 目标地址
