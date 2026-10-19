# 设计与排障背景见 openspec/changes/add-remote-middleware-relay/ 与手册 devbox/README.md §2.6。
# 用法（仓库根目录打开 PowerShell，管理员自动提升）：
#   powershell -ExecutionPolicy Bypass -File .\devbox\relay.ps1                       # 拉通/更新（问答）
#   powershell -ExecutionPolicy Bypass -File .\devbox\relay.ps1 -MySQL 10.x.x.x:3306  # 指定 mysql（新增/改址）
#   powershell -ExecutionPolicy Bypass -File .\devbox\relay.ps1 -Status               # 逐跳状态
#   powershell -ExecutionPolicy Bypass -File .\devbox\relay.ps1 -Teardown             # 对称拆除
#   存量安装目录（非仓库旁 cadence）须带 -InstallDir <目录>，如 -InstallDir D:\testpodman\cadence
param(
  [switch]$Status,
  [switch]$Teardown,
  [string]$MySQL = "",
  [string]$Redis = "",
  [string]$InstallDir = ""
)
$ErrorActionPreference = 'Stop'
Write-Host "PROBE-G-OK"
