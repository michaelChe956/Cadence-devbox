# Spec Delta

## Purpose

在既有 relay 能力之上补充两件事：让用户自己就能取得远端地址的 IP（不必依赖他人告知），以及在域名解析结果与已录地址不一致时主动提示。

## ADDED Requirements

### Requirement: 地址取值的自助指引

远端地址问句 MUST 告知用户 IP 的取得方式（用 `nslookup <域名>` 查询），并 MUST 说明在 VPN 环境下域名型服务常解析为 `198.18.x.x` 一类的假 IP、而非真实内网地址段。手册 MUST 给出 VPN 侧 HTTP 服务（如 GitLab、Artifactory）自助开通的完整步骤，含 IP 查询、通道创建与容器内验证。

#### Scenario: 用户照指引取得 IP
- **WHEN** 用户在地址问句前按指引执行 `nslookup gitlab.example.internal`
- **THEN** 得到一个可填入问句的 IP，填入后通道正常建立

#### Scenario: 手册给出 HTTP 服务的完整步骤
- **WHEN** 用户在手册中查阅 VPN 侧 HTTP 服务的开通说明
- **THEN** 能照着完成 IP 查询、通道创建与容器内连通性验证三步，无需查阅其它资料

### Requirement: 域名解析一致性核对

对已录入域名的服务，setup 与状态查询 MUST 核对「该域名当前的解析结果」与「映射表中记录的 IP」。两者不一致时 MUST 明确报告两个值并提示重跑 setup 更新；域名解析失败时 MUST 报告为解析失败，MUST NOT 报成远端不可达。核对 MUST NOT 静默改写用户已录入的地址。

#### Scenario: 解析结果与已录地址一致
- **WHEN** 用户录入 `198.18.0.2:80 gitlab.example.internal`，setup 核对时该域名当前仍解析为 `198.18.0.2`
- **THEN** 核对通过，正常建立通道，不产生告警

#### Scenario: 解析结果漂移
- **WHEN** 该域名当前解析为 `198.18.0.7`，与映射表记录的 `198.18.0.2` 不一致
- **THEN** setup 与状态查询明确报告两个值并提示重跑 setup 更新，用户已录入的地址不被静默改写

#### Scenario: 域名解析失败
- **WHEN** 核对时该域名无法解析
- **THEN** 报告解析失败并说明先确认 VPN 连接，不报告为远端不可达
