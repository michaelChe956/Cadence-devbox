# Tasks

## 1. VM 侧脚本

- [x] 1.1 编写 `devbox/relay-vm.sh`（setup/status/teardown 三子命令：dnf 装 socat、按 `/etc/cadence-relay.env` 服务表启停监听、systemd 服务管理——`machine ssh` 非交互 enable 不可用时回退 wsl.conf boot 钩子，实施首日先验证），验证：`shellcheck devbox/relay-vm.sh` 零告警、`bash -n` 通过（映射 spec：一键建立通道、通道开机自恢复、对称拆除）
- [ ] 1.2 幂等性验证：连续两次执行 setup 后 `ss -tln` 中每个配置端口仍只有一个监听、wsl.conf 只有一行钩子（映射 spec：重复运行幂等；2026-10-14 起含每服务单元连续 setup 幂等——自有实例由 enable+restart 收敛）
- [ ] 1.3 （2026-10-14 固化修订，取代 1.1 的单 unit 形态；同日按审查 7 项 Important 补强）`relay-vm.sh` 每服务模板单元 `cadence-relay@.service`（`Restart=always`+`RestartSec=5`+`StartLimitIntervalSec=0`，run 脚本 `exec socat` 前台）+ 双重身份精确接管（`readlink /proc/<pid>/exe` basename==socat + NUL 边界 argv 恰三参，kill 前重核；exe 伪装/归属任何其它 unit 的占用者 `--force` 也 exit 3 拒绝；删 pkill -f/stop 任意 unit）+ setup 先校验（服务表含服务名与 vmport 重复预检/工具/bind 在网卡/systemd 可用性）后接管 + 事务快照回滚（被覆盖文件与各实例 enabled/active 快照；失败还原文件、恢复原实例、仅 disable 本轮新启、按原命令行恢复被接管监听）+ 旧版 `cadence-relay.service` 所有权核验安全迁移 + boot 兜底 5 秒巡检监督循环（等价自恢复；setup/teardown 精确停监督）+ status 校验监听 pid=MainPID 与 bind 在网卡（漂移提示重跑）+ teardown 只清自有痕迹（不卸 socat 包、不动未知监听/portproxy）。验证：`shellcheck devbox/relay-vm.sh` 零告警 + 沙箱真实行为回归全绿后勾选（VmSandbox 组，含 exe 伪装拒绝/重复预检/事务回滚保旧配置/监督重拉/teardown 停监督）
- [ ] 1.4 交互 WSL 窗口真机固化（维护者机，已验证链路上执行）：`wsl -d podman-machine-default -u root` → 手写两行 `/etc/cadence-relay.env` → `relay-vm.sh setup`（预期 exit 3 列可接管）→ `setup --force` 精确接管两前台 socat → `status` 各项 OK → 容器内 MySQL `SELECT 1`=1、Redis `PING`=PONG → `systemctl kill cadence-relay@redis` 验证 5 秒自恢复且 mysql MainPID 不变（映射 spec：单服务故障互不影响、--force 仅精确接管、通道开机自恢复）

## 2. 宿主侧脚本

- [ ] 2.1 编写 `devbox/relay.ps1` 骨架：参数解析（-Status/-Teardown/-MySQL/-Redis）、平台守卫（Provider=wsl 且 Running）、管理员自提升、`relay.json` 读写与交互问答（回显可改），验证：非管理员窗口运行时触发 UAC 重启自身；hyperv 场景报错且无副作用（映射 spec：交互式配置与留痕、平台守卫）
- [ ] 2.2 实现 setup 主流程：远端可达性预检（TcpClient 3s，失败即终止）→ 端口占用冲突检测（存量手搭中继识别与迁移引导）→ 网桥网关动态发现（podman network inspect）→ netsh portproxy 增删（仅 127.0.0.1）→ `machine cp` 传递 relay-vm.sh 与渲染的服务表 env → `machine ssh` 执行 setup → 容器内 `/dev/tcp` 逐服务探活 → 打印连接信息，验证：对真实远端全流程跑通并输出 `网关:23306` 形态地址（映射 spec：一键建立通道、存量通道冲突检测、前置自检）
- [ ] 2.3 实现 `-Status`：逐跳探测（portproxy 规则 / VM 监听 / 容器可达 / 宿主到远端）并输出差异化结论，VPN 断线时明确提示先连 VPN（映射 spec：状态查询）
- [ ] 2.4 实现 `-Teardown`：按 relay.json 精确移除两侧痕迹 + 回显清单二次确认，验证：拆除后 `netsh interface portproxy show all` 不含本脚本端口、VM 内无 cadence-relay 残留（映射 spec：对称拆除）

## 3. 集成与文档

- [ ] 3.1 `install.ps1` 第 5 步提示追加一行指引（有远端中间件需求运行 relay.ps1），验证：重跑安装器看到提示且不影响既有步骤
- [ ] 3.2 `devbox/README.md` 新增 §2.6「连接你自己的远端中间件（VPN）」：运行方式、连接串解读、VPN 在线前提、DHCP 静态绑定建议、平台限制说明，验证：按文档命令逐条可执行（映射 spec 全部需求的用户可见面）

## 4. 自动化测试

- [x] 4.1 pytest 结构测试：relay.ps1 / relay-vm.sh 存在且含关键结构（子命令、端口表）；端口表与 compose.yaml 发布端口无重叠断言；验证：`pytest tests/devbox` 全绿（含既有 46 项不回归）——实测 104 passed（含评审修复回归测试）
- [x] 4.2 CI 覆盖：确认 ShellCheck 已拾取 relay-vm.sh（无 .shellcheckrc 排除），验证：本地 `shellcheck` 与 CI 一致

## 5. 真机验收（维护者 Windows 机执行）

- [ ] 5.1 全生命周期验收（维护者机已还原至初始态）：setup（探活+连接信息）→ 应用以 `网关:23306/26379` 连通远端 mysql/redis → `-Status` 各跳正常 → 重启 Windows 验证自恢复 → `-Teardown` 还原干净，验证：spec 各项 requirement 场景逐条对上
