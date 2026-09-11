# Cadence-devbox —— 一体化 AI 开发环境容器（产物线仓库）

> 本仓库是 [Cadence-skills](https://github.com/michaelChe956/Cadence-skills) 的**独立产物线**：开发环境容器 devbox 的镜像与编排实现。
> 决策与设计历史在 Cadence-skills 仓库（设计 v1.6 / 实施计划 / OpenSpec change `add-devbox-env`），本仓库只承载代码与测试，单向依赖：镜像启动时经 install.sh 从 Cadence-skills 拉取 skills。

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

## 与 Cadence-skills 的关系

- skills 源与本产物的 skill（`devbox-stack`）在 Cadence-skills 仓库，经 install.sh 投影进容器
- 本仓库变更不触发 Cadence-skills 的 skills CI，反之亦然
