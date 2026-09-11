# Cadence-devbox —— 一体化 AI 开发环境容器（产物线仓库）

> 本仓库是 [Cadence-skills](https://github.com/michaelChe956/Cadence-skills) 的**独立产物线**：开发环境容器 devbox 的镜像与编排实现。
> 决策与设计历史在 Cadence-skills 仓库（设计 v1.7 / 实施计划 / OpenSpec change `add-devbox-env`），本仓库只承载代码与测试，单向依赖：镜像启动时经 install.sh 从 Cadence-skills 拉取 skills。

## 背景与目的

**给谁的**：技术能力较弱的业务开发 / 测试人员（主要在 Windows/macOS），想用 AI coding agent 但装不动环境的人。

**解决什么**：

| 痛点 | devbox 的答案 |
|---|---|
| 环境安装是弃用 AI agent 的第一死因（JDK/Node/中间件/五端 CLI 逐个装） | 一次装好 Podman Desktop，之后 `podman-compose up -d` 即得到全套环境 |
| Cadence-skills 不做 Windows 原生适配 | 容器即兼容层——Linux 容器内跑全套，宿主只管浏览器和 IDE |
| 每人环境漂移、报障无法复现 | 镜像版本 pin（`versions.txt` 可对账）+ 官方目录中间件 tag pin，千机一面 |
| agent 乱跑 docker 命令、中间件配置各搞各的 | `devbox-stack` skill 统一运维入口：先查目录→自加留痕→反馈固化 |
| 换 key/换模型要重配各家 CLI | 一份 `cadence-box.yaml` 配置文件，改完重启生效 |

**一句话**：装一次 podman，之后每天的开发环境从一条 `up -d` 开始；代码留在宿主自己的盘上，构建、运行、中间件、AI agent 全在容器里。

## 内容

| 路径 | 说明 |
|---|---|
| `devbox/` | Dockerfile（4 层）、compose.yaml、stack CLI 与 catalog、entrypoint、鉴权渲染器、install.ps1、**使用手册（devbox/README.md）** |
| `tests/devbox/` | 46 项 pytest（渲染器 / stack CLI / compose 结构 / entrypoint / pipefail 回归） |

## 快速开始

```bash
podman pull ghcr.io/michaelche956/cadence-devbox:latest
```

完整手册（初次使用 / 日常使用 / 更新 / 真机验收）：[`devbox/README.md`](devbox/README.md)

## 镜像

- `ghcr.io/michaelche956/cadence-devbox`：`latest`（滚动）/ `1.0.0`（里程碑）/ `YYYY.WW`（周版，CI 化后提供）
- 版本 pin 唯一来源：`devbox/versions.env`

## CI

push/PR 触发 pytest + ShellCheck；镜像自动构建与签名在二期接入。

## 贡献：新增官方目录组件

用户侧三条路（本地自用 / 提 Issue / 提 PR）见 [devbox/README.md §4.2](devbox/README.md#42-用户想增加中间件怎么办两条路推荐-a)。贡献 catalog 条目的四件套清单与 PR 要求同节。

## 与 Cadence-skills 的关系

- skills 源与本产物的 skill（`devbox-stack`）在 Cadence-skills 仓库，经 install.sh 投影进容器
- 本仓库变更不触发 Cadence-skills 的 skills CI，反之亦然
