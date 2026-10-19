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
if (-not $InstallDir) { $InstallDir = Join-Path (Split-Path $PSScriptRoot -Parent) 'cadence' }
$RelayJson = Join-Path $InstallDir 'relay.json'

function Info($m) { Write-Host "[cadence-relay] $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[cadence-relay][警告] $m" -ForegroundColor Yellow }
function Die($m)  {
  Write-Host "[cadence-relay][错误] $m" -ForegroundColor Red
  # 双击直接运行（父进程 explorer）时窗口随退出即关：停一拍让错误可见
  try {
    $parent = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID").ParentProcessId
    if ((Get-Process -Id $parent -ErrorAction SilentlyContinue).ProcessName -eq 'explorer') {
      Write-Host '按回车键关闭本窗口…' -ForegroundColor DarkGray
      [void](Read-Host)
    }
  } catch { }
  exit 1
}

# 原生命令静默探测（$ErrorActionPreference=Stop 时原生 stderr 会抛异常，经 cmd /c 绕开）
function RunQuiet($cmd) {
  $ErrorActionPreference = 'Continue'
  $out = cmd /c "$cmd 2>nul"
  $code = $LASTEXITCODE
  $ErrorActionPreference = 'Stop'
  return @{ ok = ($code -eq 0); out = (($out -join ' ').Trim()) }
}
function Probe($cmd) { (RunQuiet $cmd).ok }

# 探测并保留原生命令的 stderr 文本（cmd 内部 2>&1 合并，绕开 EAP=Stop 对原生 stderr 的干扰）：
# 守卫用它把 podman 的真实报错带回给用户，杜绝静默失败（旧守卫 2>nul 吞掉了报错）
function RunLoud($cmd) {
  $ErrorActionPreference = 'Continue'
  $out = cmd /c "$cmd 2>&1"
  $code = $LASTEXITCODE
  $ErrorActionPreference = 'Stop'
  $text = (@($out | Where-Object { $_ -is [string] }) -join ' ').Trim()
  return @{ ok = ($code -eq 0); code = $code; out = $text }
}

# ---- 管理员自提升（portproxy 需要管理员） ----
if (-not (([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
  ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))) {
  Info '需要管理员权限，弹出 UAC 后在新窗口继续；本窗口使命到此结束'
  $argList = @('-NoExit', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath))
  if ($Status)   { $argList += '-Status' }
  if ($Teardown) { $argList += '-Teardown' }
  if ($MySQL)    { $argList += @('-MySQL', ('"{0}"' -f $MySQL)) }
  if ($Redis)    { $argList += @('-Redis', ('"{0}"' -f $Redis)) }
  if ($InstallDir) { $argList += @('-InstallDir', ('"{0}"' -f $InstallDir)) }
  try { Start-Process powershell -Verb RunAs -ArgumentList $argList }
  catch { Die "UAC 提权被拒绝或失败：$($_.Exception.Message)" }
  exit 0
}

# ---- 平台守卫：仅 Windows + WSL 后端 podman machine ----
if (-not (Get-Command podman -ErrorAction SilentlyContinue)) {
  Die '未检测到 podman，请先安装并启动 Podman Desktop（手册 §2.1）'
}

# 从合并输出定位 JSON 文档起点：podman 日志行自带 WARN[0000]/LEVEL[0000] 这类含 [ 的
# 前缀，首个 [ 未必是 JSON——逐候选起点尝试解析，成功即用；全部失败返回 $null
function ConvertFrom-JsonText($text) {
  if (-not $text) { return $null }
  for ($i = 0; $i -lt $text.Length; $i++) {
    if ($text[$i] -ne '[' -and $text[$i] -ne '{') { continue }
    try { return (ConvertFrom-Json $text.Substring($i)) } catch { }
  }
  return $null
}

# 守卫数据源唯一：podman machine list --format json（官方稳定接口，podman≥3.4 提供
# Name/Default/Running/VMType）。machine inspect 的 InspectInfo 在任何版本都无
# Provider/VMType 字段（现行 man page 仅 State/Name 等），勿作回退源；
# --format "{{.Provider}}" 任何版本必模板报错。失败必须带原始 stderr 与退出码显式终止
function Resolve-Machine {
  $ml = RunLoud 'podman machine list --format json'
  if (-not $ml.ok) { Die "podman machine list 失败（exit $($ml.code)）：$($ml.out)" }
  $mach = $null
  $list = ConvertFrom-JsonText $ml.out
  if ($list) {
    $arr = @($list)
    if ($arr.Count -gt 0) {
      $mach = @($arr | Where-Object { $_.Default })[0]
      if (-not $mach) { $mach = $arr[0] }
    }
  }
  if (-not $mach) { Die "podman machine 不存在或 list 输出无法解析：$($ml.out)" }
  $vmType = ''
  if ($mach.PSObject.Properties['VMType']) { $vmType = [string]$mach.VMType }
  $running = $null
  if ($mach.PSObject.Properties['Running']) { $running = [bool]$mach.Running }
  if (-not $vmType -or $null -eq $running) {
    Die "无法从 podman machine list 读取 VMType/Running：$($ml.out)"
  }
  if ($vmType -ne 'wsl') { Die "仅支持 WSL 后端 podman machine（当前 $vmType），见手册 §2.6 平台限制" }
  if ($running -ne $true) { Die "podman machine 未运行：podman machine start 后重试" }
  $name = ''
  if ($mach.PSObject.Properties['Name']) { $name = [string]$mach.Name }
  if (-not $name) { Die '无法取得 podman machine 名称' }
  return $name
}
$mach = Resolve-Machine

# ---- 服务目录与端口推导（2026-10-16 裁决；目录值为常见远端端口，仅作问答示例） ----
$ServiceCatalog = [ordered]@{
  mysql = 3306; redis = 6379; kafka = 9092; zookeeper = 2181; rabbitmq = 5672
}

# 推导：hostPort=remotePort+10000、vmPort=remotePort+20000——存量 mysql 3306→13306/23306、
# redis 6379→16379/26379 不变，旧 relay.json 无需迁移。上限 45535（+20000 恰 65535）；
# 推导命中本地 stack 发布端口即显式拒绝（$stack 清单与 compose.yaml 一致，测试钉住。
# 唯一撞点：远端 rabbitmq 5672 → hostPort 15672 撞本地管理台，远端须换端口）
function Get-RelayPorts($remotePort) {
  if ($remotePort -lt 1 -or $remotePort -gt 45535) {
    Die "远端端口超出可推导范围（1-45535）：$remotePort"
  }
  $hostPort = $remotePort + 10000
  $vmPort = $remotePort + 20000
  $stack = @(3000, 3306, 5672, 6379, 8080, 9000, 9001, 15672)
  if ($stack -contains $hostPort -or $stack -contains $vmPort) {
    Die "推导端口 $hostPort/$vmPort 撞本地 stack 发布端口（远端端口 $remotePort）"
  }
  return @{ hostPort = $hostPort; vmPort = $vmPort }
}

function Load-Config {
  if (Test-Path $RelayJson) {
    try { return Get-Content -Raw $RelayJson | ConvertFrom-Json } catch { Warn 'relay.json 损坏，按重新问答处理'; return $null }
  }
  return $null
}

function Test-AddrFormat($s) { return ($s -match '^(\d{1,3}\.){3}\d{1,3}:\d{2,5}$') }

function Test-SvcName($s) { return ($s -match '^[A-Za-z0-9_-]+$') }

# 解析 IP:端口 → 服务对象（含推导端口）；格式/越界/撞栈在此显式报错
function New-Service($name, $val) {
  if (-not (Test-AddrFormat $val)) { Die "$name 地址格式应为 IP:端口，收到：$val" }
  $hp = $val -split ':'
  $ports = Get-RelayPorts ([int]$hp[1])
  return [pscustomobject]@{
    name = $name; remoteHost = $hp[0]; remotePort = [int]$hp[1]
    hostPort = $ports.hostPort; vmPort = $ports.vmPort
  }
}

# 问答/参数合并出服务清单（动态长度）：回显已有（保留/删除）→ 目录/自定义新增；
# -MySQL/-Redis 参数映射目录项 mysql/redis：清单中同名则更新地址，没有则直接新增
function Collect-Services($cfg) {
  $result = @()
  $names = @{}
  $old = @()
  if ($cfg) { $old = @($cfg.services) }

  if ($old.Count -gt 0) {
    Info '已有通道（编号）：'
    for ($i = 0; $i -lt $old.Count; $i++) {
      $o = $old[$i]
      $line = '  {0}. {1} → {2}:{3}（host {4} / vm {5}）' -f ($i + 1), $o.name, $o.remoteHost, $o.remotePort, $o.hostPort, $o.vmPort
      Write-Host $line
    }
    $ans = (Read-Host '保留已有序号（回车=全部保留，a=全部删除，逗号分隔=仅保留所列）').Trim()
    $keepIdx = @()
    if ($ans -ieq 'a') { $keepIdx = @() }
    elseif ($ans -eq '') { for ($i = 0; $i -lt $old.Count; $i++) { $keepIdx += $i } }
    else {
      foreach ($tok in ($ans -split '[,，]')) {
        $n = -1
        if (-not [int]::TryParse($tok.Trim(), [ref]$n) -or $n -lt 1 -or $n -gt $old.Count) {
          Die "序号无效：$tok（应为 1-$($old.Count)，或回车/a）"
        }
        if ($keepIdx -notcontains ($n - 1)) { $keepIdx += ($n - 1) }
      }
    }
    foreach ($k in $keepIdx) { $names[$old[$k].name] = $true; $result += $old[$k] }
    if ($keepIdx.Count -lt $old.Count) {
      $removed = @()
      for ($i = 0; $i -lt $old.Count; $i++) { if ($keepIdx -notcontains $i) { $removed += $old[$i].name } }
      Info ("将删除：" + ($removed -join '、') + "（清 portproxy，VM 实例随 setup 回收）")
    }
  }

  foreach ($pn in @('mysql', 'redis')) {
    $val = @{ mysql = $MySQL; redis = $Redis }[$pn]
    if (-not $val) { continue }
    $obj = New-Service $pn $val
    if ($names.ContainsKey($pn)) {
      for ($j = 0; $j -lt $result.Count; $j++) {
        if ($result[$j].name -eq $pn) { $result[$j] = $obj; break }
      }
      Info "参数更新 $pn → $val（host $($obj.hostPort) / vm $($obj.vmPort)）"
    } else {
      $names[$pn] = $true
      $result += $obj
      Info "参数新增 $pn → $val（host $($obj.hostPort) / vm $($obj.vmPort)）"
    }
  }

  $catNames = @($ServiceCatalog.Keys)
  Info ('可新增（目录：' + ($catNames -join ' ') + '；custom=自定义；回车跳过）')
  $pick = (Read-Host '新增（多项逗号分隔，如 kafka,zookeeper 或 custom）').Trim()
  foreach ($tok in ($pick -split '[,，]')) {
    $t = $tok.Trim()
    if (-not $t) { continue }
    if ($t -ieq 'custom' -or $t -eq '自定义') {
      $newName = (Read-Host '自定义服务名（限字母数字与 _-，如 es7、db2-prod）').Trim()
      if (-not $newName) { Warn '未输入服务名，跳过自定义项'; continue }
      if (-not (Test-SvcName $newName)) { Die "服务名限字母数字与 _-：$newName" }
      if ($names.ContainsKey($newName)) { Warn "$newName 已在清单中，跳过"; continue }
      $val = (Read-Host "远端 $newName 地址（IP:端口，域名暂不支持）").Trim()
      if (-not $val) { Warn "跳过 $newName（未提供地址）"; continue }
      $names[$newName] = $true
      $result += (New-Service $newName $val)
    } else {
      if ($catNames -notcontains $t) { Die "未知目录项：$t（目录：$($catNames -join ' ') 或 custom）" }
      if ($names.ContainsKey($t)) { Warn "$t 已在清单中，跳过（改地址：不保留该项后新增）"; continue }
      $val = (Read-Host "远端 $t 地址（例如 10.129.12.75:$($ServiceCatalog[$t])，IP:端口）").Trim()
      if (-not $val) { Warn "跳过 $t（未提供地址）"; continue }
      $names[$t] = $true
      $result += (New-Service $t $val)
    }
  }

  # 端口冲突预检：不同服务不得推导/沿用相同 host/vm 端口（同远端端口必撞）
  $seen = @{}
  foreach ($s in $result) {
    foreach ($p in @($s.hostPort, $s.vmPort)) {
      if ($seen.ContainsKey($p)) { Die "端口 $p 同时属于 $($seen[$p]) 与 $($s.name)，请调整其一" }
      $seen[$p] = $s.name
    }
  }
  if ($result.Count -eq 0) { Die '未配置任何服务（重跑 setup 须至少保留/新增一项；全部拆除请用 -Teardown）' }
  return $result
}

function Test-RemotePort($h, $p) {
  $client = New-Object System.Net.Sockets.TcpClient
  try {
    $iar = $client.BeginConnect($h, $p, $null, $null)
    if ($iar.AsyncWaitHandle.WaitOne(3000) -and $client.Connected) { $client.EndConnect($iar) | Out-Null; return $true }
    return $false
  } catch { return $false } finally { $client.Close() }
}

Write-Host "PROBE-P-OK"
