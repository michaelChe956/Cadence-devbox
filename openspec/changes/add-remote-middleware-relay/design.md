# Design: add-remote-middleware-relay

## Context

链路已于 2026-10-08/09 在维护者 Windows 真机（Podman Desktop + WSL 后端 + mirrored 网络 + 宿主 VPN）完整验证：容器 → VM socat(23306) → WSL mirrored 回环 127.0.0.1:13306 → Windows netsh portproxy → VPN → 远端 MySQL，端到端 `SELECT 1` 通过。2026-10-14 端到端复验（容器内 MySQL `SELECT 1` 返回 1、Redis 认证 `PING` 返回 `PONG`）并按「每服务一个 systemd 模板实例」交付固化实现；脚本本身的真机固化执行与验收 = tasks 1.4（待办，未声称已验证）。实测约束（设计输入）：

- aTrust VPN 静默丢弃一切 NAT 转发流量，仅 Windows 本机进程发出的流量走隧道（2026-09-28/10-09 排查定论）——任何方案最后一跳必须由 Windows 本机发起，portproxy 是唯一入口
- mirrored 模式下 VM 的 `127.0.0.1` 可达 Windows 的 loopback 监听（实测 M_OK）；但镜像模式不把打到 Windows 自有 IP 的连接转交给 Windows 进程（一律 refused，实测）——容器侧必须由 VM 自建监听做中继
- WSL 嵌套命名空间下 systemd running，但交互式 `wsl -d podman-machine-default -u root` 进入后 systemctl 全部可用；一次性 `wsl -u root systemctl ...` 报 `Transport endpoint is not connected`（2026-10-09/10-14 实测）——固化交付走交互窗口，不经非交互链路
- PowerShell → wsl → sudo 多层引号会拆坏长复合命令（实测静默失败）；install.ps1 已确立"cp 文件进 VM 再 ssh 执行"的规避模式
- 用户终端粘贴超约 120 字符的行会被折行截断；面向用户输出的命令每行 ≤100 字符，PowerShell 块用 @() 数组而非 here-string（实测教训）

## Goals / Non-Goals

见 proposal 非目标。设计层面补充边界：不修改 compose 模板与镜像；不进入 stack 命令体系（这是宿主侧网络工具，不是容器内中间件目录的一部分）；不管理 VPN 客户端本身。

## Decisions

### D1. 两跳中继 + 端口推导规则（2026-10-16 修订：固定端口表 → 目录 + 推导）

每个服务两跳：Windows `netsh portproxy`（仅监听 `127.0.0.1`，经 mirrored 回环被 VM 使用，不暴露局域网）+ VM 内 socat（仅绑定 podman 网桥网关地址——只有容器网段可达；网关地址经 `podman network inspect` 动态发现，不硬编码）。

端口推导（用户 2026-10-16 裁决，与存量通道向后兼容）：`hostPort = remotePort + 10000`、`vmPort = remotePort + 20000`，remotePort 合法范围 [1, 45535]（上限 45535+20000 恰为 65535，vmPort 不越界；评审修正：初版误写 46535——那是 hostPort 的边界，vmPort 会算出 66535）。存量锚点：mysql 3306→13306/23306、redis 6379→16379/26379 与 v1 固定表一致，旧 relay.json 保留值无需迁移（保留项沿用旧值，不重新推导）。推导值恒 ≥10001，与 stack 自带发布端口（3306/6379/5672/15672/9000/9001/3000/8080）零重叠——经全域枚举核实存在唯一撞点：远端 rabbitmq 5672 → hostPort 15672 撞本地 rabbitmq 管理台。处置：`Get-RelayPorts` 内置 stack 发布端口清单（与 compose.yaml 一致，测试断言），推导命中即显式拒绝并说明原因（远端须换非 5672 端口）；测试钉住 5672 必拒、45535 通过/45536 拒绝、目录其余项零重叠。

服务目录（新增服务的选择面，值为常见远端端口、仅作问答示例提示）：mysql(3306)、redis(6379)、kafka(9092)、zookeeper(2181)、rabbitmq(5672)；目录外走 `custom` 自定义（服务名限 `[A-Za-z0-9_-]`，与 systemd 实例名约束一致 + IP:端口）。不同服务推导出相同 host/vm 端口（同远端端口）在问答合并后统一预检拒绝。

备选否决：单跳（VM 直连远端）——实测不通；中继做成 compose 服务（`network_mode: host`）——依赖 podman-compose 的 host 网络行为且把宿主网络工具混入容器 stack 体系，持久化也弱于 boot 钩子。

### D2. 宿主脚本 + VM 脚本，文件传递

`devbox/relay.ps1`（用户入口：setup 默认 / `-Status` / `-Teardown`；交互问答；参数 `-MySQL host:port` `-Redis host:port` 可覆盖）负责：平台守卫、前置自检、portproxy 管理、podman 网桥网关发现、把 `devbox/relay-vm.sh` 与渲染出的服务表 env 文件 `podman machine cp` 进 VM 后 `podman machine ssh sudo bash` 执行、最后从 devbox 容器探活并打印连接信息。

`devbox/relay-vm.sh`（子命令 setup/status/teardown，ShellCheck 覆盖）负责 VM 内一切：dnf 装 socat、写 `/etc/cadence-relay.env`、管理 systemd 服务（或兜底 wsl.conf 钩子）、按配置端口启停监听。全程不使用内嵌长命令字符串（规避引号拆坏，install.ps1 同款教训）。

交互与多服务（2026-10-16 改造）：重跑 setup 先回显 relay.json 已有通道（编号），
「回车=全部保留，a=全部删除，逗号分隔=仅保留所列」；再目录多选新增（mysql/
redis/kafka/zookeeper/rabbitmq 或 custom=服务名+IP:端口），逐项问远端地址；
`-MySQL`/`-Redis` 参数继续映射 mysql/redis（清单中同名则更新地址，没有则直接
新增）。`relay.json` services 为动态长度。**被删/改端口服务两侧清理**（陈旧判定
按「旧 hostPort 不在新清单」，同时覆盖服务删除与同名换端口——按服务名判定会
漏掉后者，旧 portproxy 将永久残留，评审 P2 修正；同端口重建仍由 add 前的
delete 兜底）：Windows 侧 portproxy 在预检通过后、新建前按旧记录精确删除；
VM 侧自有实例由本次 setup 按新 env 回收（relay-vm.sh 前段
`recycle_stale_instances`：wants 目录扫描，仅链接解析到本方案模板且模板
Description 匹配的自有 `cadence-relay@*` 才 disable --now + 清链接；未知/外来
链接一律不动；回收表达用户删除意图，不参与事务回滚；置于占用分析之前，以便
改名/重推导端口后旧实例先腾出新端口）。**boot 兜底模式**（无 systemd，socat
由监督 nohup 拉起、非 systemd 实例）下由 `setup --retire <旧服务表>` 对账回收
（relay.ps1 每次渲染旧表文件，无变更则仅表头）：对旧 vmport 上 is_expected_socat
三参核验一致的自有监听精确 kill，未知不动；systemd 模式下删服务场景为幂等 no-op（recycle 已先行停实例），同名换端口场景会提前终止该实例 MainPID，由 Restart=always 在 5 秒内自恢复并绑新端口、⑥ 再 restart，收敛语义不变；
对账 bind 用当前网关，网关漂移时旧监听 argv 对不上会被显式跳过（评审 P3 补）。

### D3. 持久化：每服务 systemd 模板实例，boot 钩子兜底（2026-10-14 修订）

VM 侧每个服务一个 systemd 模板实例 `cadence-relay@<name>.service`：`ExecStart=/usr/local/bin/cadence-relay-run %i`，run 脚本按服务名查 `/etc/cadence-relay.env` 后 `exec socat`（前台，MainPID 即监听进程）；单元配 `Restart=always` + `RestartSec=5` + `StartLimitIntervalSec=0`（[Unit] 段）——开机网桥未就绪时绑定失败每 5 秒自动重试直到成功，且单服务故障互不影响（取代 v1 的单 unit + run 脚本内 pkill 重试循环：旧方案一个 socat 挂掉后无人拉起、端口级 pkill 会误杀无关进程）。

setup 顺序（任何停止/接管之前完成全部校验）：读服务表（服务名限 `[A-Za-z0-9_-]`；**服务名与 vmport 重复即拒绝**，防第二行静默失配）→ 工具检查（ss/socat/dnf）→ bind 地址在本机网卡（网桥漂移即显式失败）→ systemd 可用性（`is-system-running`；不可用回退 wsl.conf [boot] 兜底，存在外来 `[boot]` 段则显式失败）→ 只读占用分析 → **事务快照**（被覆盖文件备份 + 各实例 enabled/active 状态）→ 写文件 → 旧版 `cadence-relay.service` 安全迁移（Description 所有权核验通过才停用移除，非本方案同名文件拒绝）→ 精确接管（kill 前重核身份）→ enable+restart → 逐服务验证监听。失败按快照事务回滚：disable 仅本轮新启实例、还原被覆盖文件、恢复原实例 enabled/active（restart 原监听）、按原命令行恢复被接管监听；不卸载 socat 包。

精确接管（取代 v1 的 kill_port/pkill -f）：双重身份核验——`readlink /proc/<pid>/exe` 的 basename 必须是 socat（exe 内核级身份，argv 伪造无效），且按 NUL 边界读取的完整 argv 恰为三参（socat + `TCP-LISTEN:<vmport>,bind=<bindaddr>,fork,reuseaddr` + `TCP:127.0.0.1:<hostport>`；额外选项/地址均判不一致），kill 前重核 pid 身份。占用者归属任何非本方案 systemd unit（如 socat-mysql-relay）时一律 exit 3 拒绝，不杀不停不删（本方案自有实例/旧版单 unit 除外，走 enable+restart 幂等收敛）。接管有 1-5 秒空档、会断现有连接（文档要求用户空闲时明确执行）；已建立连接的 fork 子进程随断连退出，不在本方案进程树清理范围（既有边界）。

boot 兜底（systemd 不可用时）：`/usr/local/bin/cadence-relay-boot` 为监督循环——每 5 秒按服务表巡检，监听消失即重拉对应 run 脚本（等价 `Restart=always` 的自恢复，含开机网桥晚就绪场景）；监督进程身份（argv 含 BOOT_SCRIPT 路径）可被 setup/teardown 精确停止，禁止双监督竞拉。

交付路线（用户在已验证链路上执行；tasks 1.4 为真机固化验收项）：用户在交互 root WSL 窗口（`wsl -d podman-machine-default -u root`）手工写两行服务表 env 后直接跑 `relay-vm.sh setup`/`setup --force`；不经 relay.ps1、不经非交互 systemctl（Transport 错，见 Context）。podman 网络重建导致网关漂移：status 检测 bind 不在网卡即提示重跑 setup 重绑（监听仍指向旧网桥，必须重跑）。

### D4. 配置留痕与拆除安全

配置存 `<InstallDir>/relay.json`（与 cadence-box.yaml 同目录，进安装器写好的本地 .gitignore 防误提交）：`{"version":1,"services":[{"name":"mysql","remoteHost":"10.x.x.x","remotePort":3306,"hostPort":13306,"vmPort":23306},...]}`。`-Teardown` 只删：配置中记录的端口对应的 portproxy 规则、VM 侧自有痕迹（cadence-relay@ 实例与模板、run/boot 脚本、服务表、日志、boot 钩子行）、relay.json 本体。**socat 包保留不卸**（2026-10-14 修订：包可能被其它用途共享，卸载超出"本脚本创建的痕迹"边界；teardown 输出明确说明未动项）。用户/IT 手工创建的其他 portproxy 规则与未知监听不动（按端口/argv 精确匹配）。teardown 前回显将删除项并二次确认。

### D5. 前置自检与顺序

setup 执行顺序固定：平台守卫（`podman machine inspect` Provider 必须 wsl 且 Running）→ 管理员检测（非管理员时自动 `-Verb RunAs` 重启自身，失败则 Die）→ 远端可达性探测（`TcpClient` 3 秒超时，逐服务）→ 任何失败在改动前终止，不留半配置。`-Status` 同样逐跳探测：portproxy 规则存在性（解析 `netsh interface portproxy show all`）→ VM 监听（`ss -tln` 按端口）→ 容器内 `/dev/tcp` 探活 → 宿主到远端可达性；远端不通而中继正常时报"VPN 未连"。

### D6. 连接地址：podman 网桥网关

应用连接 `网桥网关:vm端口`（如 `10.89.0.1:23306`）——网关属 podman 网络对象，持久存在、与宿主 DHCP 无关，地址稳定（网络重建才可能漂移；setup/status 每次动态发现并打印，漂移后重跑即重绑）。不依赖 Windows 网卡 IP，无静态绑定要求。

## Risks / Trade-offs

- [接管 1-5 秒空档断现有连接] → 属 `--force` 的固有代价；先以不带 --force 的 setup 列出"可接管"清单，用户空闲时明确执行；失败回滚按原命令行恢复监听（原前台窗口不再持有进程，以 nohup 方式恢复）
- [被接管 socat 的 fork 子进程残留] → kill 监听主 pid 后子连接进程随断连退出；验证阶段若监听未按期出现则整体回滚
- [维护者机已有 0.0.0.0:13306 的 portproxy 规则把 MySQL 暴露到局域网] → 与 D1"仅 127.0.0.1"冲突但属用户自建规则，本方案不动（D4 边界）；文档与交付说明中明确提醒用户自行处理
- [重启后 systemd 能否拉起 enabled 实例] → 交互窗口内 is-system-running=running 支持该推断，但未做重启实测；留给真机验收 5.1（重启 Windows 后容器内探活）
- [is-system-running 返回 starting/degraded 以外状态时走 wsl.conf 兜底，而机器 wsl.conf 已有 [boot] 段会显式失败] → 显式失败而非静默；提示改走交互 systemd 路线
- [WSL/Podman 更新改变 mirrored 回环行为] → `-Status` 逐跳定位把"哪一跳坏了"变成一句话结论；README 记录依赖前提
- [VPN 掉线导致通道不可用] → 属预期；前置自检与 status 的差异化提示让用户能自行判断
- [podman 网络重建导致网桥网关漂移] → 每次运行动态发现网关（podman network inspect），不硬编码；VM 侧 status 检测 bind 不在网卡即提示重跑 setup 重绑
- [弱技术用户误删/误拆] → teardown 二次确认 + 回显清单；VM 侧 teardown 不动未知监听/socat 包/portproxy
- [端口与 stack 自带中间件冲突] → 固定错开（13306/16379 vs 3306/6379），测试断言无重叠
- [存量手搭中继占用端口] → setup 只读占用分析：argv 完全一致才可在 --force 下精确接管，未知占用者 exit 3 报告并拒绝（维护者机的两个前台 socat 即实例，已按此规则接管固化）

## Migration Plan

纯新增文件 + 两处小改（install.ps1 尾部提示行、README §2.6 章节）。维护者机存在手搭同链路（socat-mysql-relay）：验收前先整体还原，由脚本全新搭建验证。其他用户机若存在手搭中继，setup 冲突检测引导迁移。回滚：删除新增文件；用户侧跑 `-Teardown` 清环境。

## Open Questions

（无）
