<#
.SYNOPSIS
  构建 devbox 镜像，校验关键路径后（可选）推送到 ACR。

.DESCRIPTION
  构建在 podman machine（WSL 的 Linux VM）内执行——不是 Windows 原生。
  脚本会先去掉 Dockerfile 首行 `# syntax=docker/dockerfile:1`：那是 BuildKit 前端指令，
  podman/buildah 会尝试拉取 docker/dockerfile:1，构建机上不一定可达；Dockerfile 只用标准指令。

  推送前会做三项校验（不通过即中止，不会推坏镜像）：
    1) /home/dev/.cadence 存在且属 dev（老镜像缺它 → 容器启动即 Permission denied）
    2) /opt/cadence/stack/compose.yaml 存在且含 container_name（固定容器名随镜像模板分发）
    3) entrypoint 语法可解析

.PARAMETER Tag
  镜像 tag，如 2026.44（周版）或 latest。

.PARAMETER Image
  完整镜像名（不含 tag）。默认阿里云 ACR 个人实例路径。

.PARAMETER Push
  带上才会登录并推送；不带只构建 + 校验。

.PARAMETER AlsoLatest
  推送后额外把该 tag 打上 latest 并推送。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\devbox\publish-image.ps1 -Tag 2026.44

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\devbox\publish-image.ps1 -Tag 2026.44 -Push -AlsoLatest
#>
param(
  [Parameter(Mandatory = $true)][string]$Tag,
  [string]$Image = 'crpi-qzp491l6hpbyhd49.cn-hangzhou.personal.cr.aliyuncs.com/cadence/devbox',
  [switch]$Push,
  [switch]$AlsoLatest
)
$ErrorActionPreference = 'Stop'

function Info($m) { Write-Host "[publish] $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[publish][警告] $m" -ForegroundColor Yellow }
function Die($m) { Write-Host "[publish][错误] $m" -ForegroundColor Red; exit 1 }

$repoRoot = Split-Path $PSScriptRoot -Parent
$context = Join-Path $repoRoot 'devbox'
if (-not (Test-Path (Join-Path $context 'Dockerfile'))) { Die "构建上下文不对：$context（应含 Dockerfile）" }

$dockerfile = Join-Path $context 'Dockerfile'
$sanitized = Join-Path ([IO.Path]::GetTempPath()) 'devbox-Dockerfile.build'
# 去首行 syntax 指令 + 统一 LF + 无 BOM（nft/dockerfile 解析都对 CRLF/BOM 敏感，参见仓库既有约定）
$text = [IO.File]::ReadAllText($dockerfile)
$text = $text -replace '^# syntax=[^\r\n]*\r?\n', ''
[IO.File]::WriteAllText($sanitized, $text.Replace("`r`n", "`n"))
Info "已生成去 syntax 指令的 Dockerfile：$sanitized"

$full = "${Image}:${Tag}"
Info "开始构建：$full（首次约 10~25 分钟，取决于网络）"
podman build -f $sanitized -t $full $context
if ($LASTEXITCODE -ne 0) { Die "构建失败（exit $LASTEXITCODE）——若报 docker/dockerfile:1 相关错误，确认 $sanitized 首行已无 syntax 指令" }

# ---- 三项校验：不通过就不推 ----
Info '校验 1/3：/home/dev/.cadence 存在（老镜像缺它 → entrypoint Permission denied）'
podman run --rm --entrypoint test $full -d /home/dev/.cadence
if ($LASTEXITCODE -ne 0) { Die '镜像缺 /home/dev/.cadence——挂载点未预建，容器启动会失败' }

Info '校验 2/3：/opt/cadence/stack/compose.yaml 含 container_name（固定容器名随模板分发）'
$hit = podman run --rm --entrypoint grep $full -n 'container_name' /opt/cadence/stack/compose.yaml
if ($LASTEXITCODE -ne 0) { Die '镜像内置 compose.yaml 缺 container_name——安装目录会被旧模板覆盖回默认容器名' }
Write-Host ($hit -join "`n")

Info '校验 3/3：entrypoint 语法'
podman run --rm --entrypoint bash $full -c 'bash -n /usr/local/bin/cadence-entrypoint && echo entrypoint-ok'
if ($LASTEXITCODE -ne 0) { Die 'entrypoint 语法检查失败' }

if (-not $Push) {
  Info "构建与校验完成（未推送）。本地镜像：$full"
  Info "要推送加 -Push；也可先手工验证：podman run --rm -it --entrypoint bash $full"
  exit 0
}

# ---- 登录并推送 ----
$registry = ($Image -split '/')[0]
Info "登录 $registry（输入 ACR 用户名与密码/访问令牌）"
podman login $registry
if ($LASTEXITCODE -ne 0) { Die "登录失败（exit $LASTEXITCODE）" }

Info "推送 $full"
podman push $full
if ($LASTEXITCODE -ne 0) { Die "推送失败（exit $LASTEXITCODE）" }

if ($AlsoLatest -and $Tag -ne 'latest') {
  $latest = "${Image}:latest"
  Info "更新 latest：$latest"
  podman tag $full $latest
  podman push $latest
  if ($LASTEXITCODE -ne 0) { Warn "latest 推送失败（$Tag 已推送成功）" }
}

Info '完成。客户端升级方式：'
Write-Host @"
  1) 改 D:\testpodman\cadence\stack\.env 里的 CADENCE_DEVBOX_IMAGE → $full
  2) cd D:\testpodman\cadence\stack; podman-compose -f compose.yaml up -d devbox
  3) 可删除 stack\CHANGES.md（新镜像模板已自带 container_name），重启容器验证模板同步恢复正常
"@
