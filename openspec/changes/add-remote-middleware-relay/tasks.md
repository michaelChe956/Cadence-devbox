# Tasks

## 1. VM 侧脚本

- [ ] 1.1 编写 `devbox/relay-vm.sh`（setup/status/teardown 三子命令：dnf 装 socat、按 `/etc/cadence-relay.env` 服务表启停监听、管理 `/usr/local/bin/cadence-relay-boot` 与 wsl.conf 钩子行），验证：`shellcheck devbox/relay-vm.sh` 零告警、`bash -n` 通过（映射 spec：一键建立通道、通道开机自恢复、对称拆除）
- [ ] 1.2 幂等性验证：连续两次执行 setup 后 `ss -tln` 中每个配置端口仍只有一个监听、wsl.conf 只有一行钩子（映射 spec：重复运行幂等）

## 2. 宿主侧脚本

- [ ] 2.1 编写 `devbox/relay.ps1` 骨架：参数解析（-Status/-Teardown/-MySQL/-Redis）、平台守卫（Provider=wsl 且 Running）、管理员自提升、`relay.json` 读写与交互问答（回显可改），验证：非管理员窗口运行时触发 UAC 重启自身；hyperv 场景报错且无副作用（映射 spec：交互式配置与留痕、平台守卫）
- [ ] 2.2 实现 setup 主流程：远端可达性预检（TcpClient 3s，失败即终止）→ netsh portproxy 增删（仅 127.0.0.1）→ `machine cp` 传递 relay-vm.sh 与渲染的服务表 env → `machine ssh` 执行 setup → 容器内 `/dev/tcp` 逐服务探活 → 打印连接信息，验证：对真实远端全流程跑通并输出 `IP:23306` 形态地址（映射 spec：一键建立通道、前置自检）
- [ ] 2.3 实现 `-Status`：逐跳探测（portproxy 规则 / VM 监听 / 容器可达 / 宿主到远端）并输出差异化结论，VPN 断线时明确提示先连 VPN（映射 spec：状态查询）
- [ ] 2.4 实现 `-Teardown`：按 relay.json 精确移除两侧痕迹 + 回显清单二次确认，验证：拆除后 `netsh interface portproxy show all` 不含本脚本端口、VM 内无 cadence-relay 残留（映射 spec：对称拆除）

## 3. 集成与文档

- [ ] 3.1 `install.ps1` 第 5 步提示追加一行指引（有远端中间件需求运行 relay.ps1），验证：重跑安装器看到提示且不影响既有步骤
- [ ] 3.2 `devbox/README.md` 新增 §2.6「连接你自己的远端中间件（VPN）」：运行方式、连接串解读、VPN 在线前提、DHCP 静态绑定建议、平台限制说明，验证：按文档命令逐条可执行（映射 spec 全部需求的用户可见面）

## 4. 自动化测试

- [ ] 4.1 pytest 结构测试：relay.ps1 / relay-vm.sh 存在且含关键结构（子命令、端口表）；端口表与 compose.yaml 发布端口无重叠断言；验证：`pytest tests/devbox` 全绿（含既有 46 项不回归）
- [ ] 4.2 CI 覆盖：确认 ShellCheck 已拾取 relay-vm.sh（无 .shellcheckrc 排除），验证：本地 `shellcheck` 与 CI 一致

## 5. 真机验收（维护者 Windows 机执行）

- [ ] 5.1 全生命周期验收：setup（探活+连接信息）→ 应用以 `IP:23306/26379` 连通远端 mysql/redis → `-Status` 各跳正常 → 重启 Windows 验证自恢复 → `-Teardown` 还原干净，验证：spec 七项 requirement 场景逐条对上
