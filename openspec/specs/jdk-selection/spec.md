# jdk-selection Specification

## Purpose
为同一 devbox 中需要不同 Java 版本的用户提供单一命令入口：在不维护多张镜像的情况下按需安装完整 OpenJDK 8 JDK、手动选择当前版本，并在容器重建后持续使用上次选择，兼顾中国大陆网络环境中的下载可靠性。

## Requirements

### Requirement: 默认版本与持久选择
系统 MUST 在用户尚未选择版本时使用镜像内置 Java 21。用户通过 `jdk use <主版本>` 成功选择版本后，系统 MUST 记住该选择，容器重新进入或重建后未指定版本时仍使用上次选择，不得弹出选择菜单。

#### Scenario: 首次使用
- **WHEN** 用户在未曾选择 JDK 的新 devbox 中运行 `java -version`
- **THEN** 使用内置 Java 21，且进入容器时无交互菜单

#### Scenario: 切换后重建
- **WHEN** 用户成功执行 `jdk use 8` 后重建并重新进入容器
- **THEN** 当前 Java 仍为 8，无需重新下载或再次指定

#### Scenario: 切回内置版本
- **WHEN** 用户执行 `jdk use 21`
- **THEN** 后续命令使用内置 Java 21，该选择也在重建后保留

### Requirement: 新命令的统一版本环境
系统 MUST 让后续新启动的交互 shell 和非交互容器命令（包括直接执行 `java`、Maven 和 Gradle）使用已选择的 JDK，并提供一致的 `JAVA_HOME`。系统 MUST NOT 声称切换会改变已经运行的 JVM。

#### Scenario: 非交互调用
- **WHEN** 用户选择 Java 8 后分别以交互 shell 和非交互容器命令运行 `java -version`、Maven 或 Gradle
- **THEN** 新命令看到相同的 Java 8 和一致的 `JAVA_HOME`

#### Scenario: 已运行进程
- **WHEN** 一个 Java 21 进程已经运行且用户切换到 Java 8
- **THEN** 已运行进程继续使用启动时的 Java 21，之后启动的 Java 命令使用 Java 8

### Requirement: 版本查询和按需安装
系统 MUST 提供 `jdk list` 显示已安装版本及当前选择；`jdk use <主版本>` 在目标版本未安装时 MUST 按需安装并在安装成功后切换。选择版本 8 时安装的 MUST 是完整 OpenJDK 8 JDK，`java` 与 `javac` 均为 8；不得只提供 JRE 或要求用户手工修改镜像。

#### Scenario: 查看当前版本
- **WHEN** 用户运行 `jdk list`
- **THEN** 可辨认内置与额外安装的版本，并明确标记当前版本

#### Scenario: 首次选择 OpenJDK 8
- **WHEN** OpenJDK 8 尚未安装且用户运行 `jdk use 8`
- **THEN** 下载并校验完整 OpenJDK 8 JDK，安装后 `java -version` 与 `javac -version` 均显示 8，切换生效且重建后无需重复下载

### Requirement: 下载源与失败安全
系统 MUST 支持配置适合中国大陆网络的 OpenJDK 8 下载源，MUST 在激活前验证下载内容的完整性、来源及 JDK 实际版本，并在下载、校验、安装或不支持版本失败时报告明确错误、保持原有 JDK 选择不变；MUST NOT 默默回退至未验证的代理。

#### Scenario: 镜像源下载成功
- **WHEN** 配置的可信国内源提供所请求版本及可信校验信息且下载成功
- **THEN** 完成校验、安装和切换

#### Scenario: 下载或校验失败
- **WHEN** 下载不可达、校验不匹配或安装中断
- **THEN** 返回失败并说明原因，`jdk list` 和新启动命令继续使用原有版本

#### Scenario: 请求不可用版本
- **WHEN** 用户请求无可信下载来源或不支持的版本
- **THEN** 返回明确错误且不改变当前选择
