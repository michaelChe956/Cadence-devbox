# devbox-container Specification

## Purpose
规定 devbox 容器在宿主上的创建路径、命名与运行态探测契约：任何时刻至多存在一个 devbox 容器，所有脚本与文档指向同一个名字，探测只认「在运行」的容器。

## Requirements

### Requirement: 容器创建路径唯一

devbox 容器 MUST 只经由 compose 创建；安装器 MUST NOT 再用 `podman run` 手工创建 devbox 容器。安装器 MUST 在启动前清理历史遗留的 devbox 容器名，并在启动后断言 devbox 容器至多一个。

同一 `/workspace` MUST NOT 同时被两个 devbox 容器挂载——这是容器内并发 git 操作撞 `index.lock` 的直接成因。

#### Scenario: 全新安装只产生一个容器
- **WHEN** 用户在未装过 devbox 的宿主上执行安装器
- **THEN** 安装结束后宿主上存在且仅存在一个 devbox 容器，compose 管理的中间件容器不受影响

#### Scenario: 重跑安装器不产生第二个容器
- **WHEN** 用户在已有 devbox 容器的宿主上重跑安装器
- **THEN** 安装结束后仍只存在一个 devbox 容器，挂载 `/workspace` 的 devbox 容器数量为 1

#### Scenario: 遗留旧名容器被清理
- **WHEN** 宿主上存在以历史命名留下的 devbox 容器（安装器手工创建路线留下的同名容器，或 compose 默认名形式的容器），用户运行安装器
- **THEN** 安装器删除这些遗留容器后再创建正式容器，并在输出中说明清理了哪些容器

#### Scenario: 唯一性不满足时显式失败
- **WHEN** 安装器启动后检测到仍存在多于一个挂载 `/workspace` 的 devbox 容器
- **THEN** 安装器明确报错指出容器名清单与处置方式，不静默继续

### Requirement: 容器命名固定

devbox 容器的名字 MUST 恒为 `cadence_devbox`，MUST NOT 依赖 compose 默认的 `<项目>_<服务>_1` 命名形式。脚本、手册与提示文案 MUST 统一引用这一个名字。

#### Scenario: compose 创建后的容器名固定
- **WHEN** 用户执行 compose 启动 devbox 服务
- **THEN** 容器名为 `cadence_devbox`，与手工创建路线留下的其它名字无关

#### Scenario: 脚本与文档使用同一名字
- **WHEN** 用户按手册或按脚本提示操作 devbox 容器
- **THEN** 手册与提示中出现的容器名与实际创建的容器名一致，无需用户自行替换

### Requirement: 容器探测按运行态

探测 devbox 容器时，MUST 只把「在运行」的容器当作探测目标。容器不存在、容器存在但未运行、同时存在多个 devbox 容器，MUST 分别给出可执行的处置提示；MUST NOT 把已停止的容器当作探测目标并据此报告链路故障。

#### Scenario: 已停止的容器不被当作探测目标
- **WHEN** 宿主上存在一个已停止的 devbox 容器，且不存在其它在运行的 devbox 容器
- **THEN** 探测明确报告「容器未运行」并提示启动命令，而不是报告所有通道均不可达

#### Scenario: 容器不存在
- **WHEN** 宿主上不存在任何 devbox 容器
- **THEN** 探测明确报告容器不存在并提示创建命令，不报告通道故障

#### Scenario: 存在多个 devbox 容器
- **WHEN** 宿主上同时存在多个 devbox 容器（其中至多一个在运行）
- **THEN** 探测明确报告检测到多个容器并列出容器名，对在运行的那个继续探测，同时提示清理多余容器

### Requirement: 容器重建不丢工作区

容器重建（改名、删除后重建、镜像升级）MUST NOT 影响 `/workspace` 内用户仓库的内容与 Git 状态。

#### Scenario: 改名后仓库仍在
- **WHEN** 用户把现有 devbox 容器改名为固定名后重新进入容器
- **THEN** `/workspace` 下的仓库、其工作区改动与 Git 历史与改名前一致
