# cadence relay 宿主侧一键脚本：容器 → VM socat → mirrored 回环 → Windows portproxy → VPN → 远端中间件
# 设计与排障背景见 openspec/changes/add-remote-middleware-relay/ 与手册 devbox/README.md §2.6。
# 用法（仓库根目录打开 PowerShell，管理员自动提升）：
#   powershell -ExecutionPolicy Bypass -File .\devbox\relay.ps1                       # 拉通/更新（问答）
#   powershell -ExecutionPolicy Bypass -File .\devbox\relay.ps1 -MySQL 10.x.x.x:3306  # 参数覆盖问答
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

# ---- 服务表（与 stack 自带中间件端口错开：compose 发布 3306/6379/5672/15672/9000/9001/3000/8080） ----
$ServiceDefaults = [ordered]@{
  mysql = @{ hostPort = 13306; vmPort = 23306 }
  redis = @{ hostPort = 16379; vmPort = 26379 }
}

function Load-Config {
  if (Test-Path $RelayJson) {
    try { return Get-Content -Raw $RelayJson | ConvertFrom-Json } catch { Warn 'relay.json 损坏，按重新问答处理'; return $null }
  }
  return $null
}

function Test-AddrFormat($s) { return ($s -match '^(\d{1,3}\.){3}\d{1,3}:\d{2,5}$') }

# 问答/参数合并出服务清单：参数覆盖 > 回显旧值回车保留 > 新问答
function Collect-Services($cfg) {
  $paramBySvc = @{ mysql = $MySQL; redis = $Redis }
  $result = @()
  foreach ($name in $ServiceDefaults.Keys) {
    $old = $null
    if ($cfg) { $hit = @($cfg.services | Where-Object { $_.name -eq $name }); if ($hit.Count -gt 0) { $old = $hit[0].remoteHost + ':' + $hit[0].remotePort } }
    $val = ''
    if ($paramBySvc[$name]) { $val = $paramBySvc[$name] }
    else {
      $tip = if ($old) { "回车保留 $old" } else { "例如 10.129.12.75:3306（IP:端口，域名暂不支持）" }
      $val = Read-Host "远端 $name 地址（$tip）"
      if (-not $val -and $old) { $val = $old }
    }
    if (-not $val) { Warn "跳过 $name（未提供地址）"; continue }
    if (-not (Test-AddrFormat $val)) { Die "$name 地址格式应为 IP:端口，收到：$val" }
    $hp = $val -split ':'
    $result += [pscustomobject]@{
      name = $name; remoteHost = $hp[0]; remotePort = [int]$hp[1]
      hostPort = $ServiceDefaults[$name].hostPort; vmPort = $ServiceDefaults[$name].vmPort
    }
  }
  if ($result.Count -eq 0) { Die '未配置任何服务，无事可做' }
  return $result
}

# ---- 探测工具 ----
function Test-RemotePort($h, $p) {
  $client = New-Object System.Net.Sockets.TcpClient
  try {
    $iar = $client.BeginConnect($h, $p, $null, $null)
    if ($iar.AsyncWaitHandle.WaitOne(3000) -and $client.Connected) { $client.EndConnect($iar) | Out-Null; return $true }
    return $false
  } catch { return $false } finally { $client.Close() }
}

function Get-BridgeGateway {
  $r = RunQuiet 'podman network inspect cadence_default --format "{{range .Subnets}}{{.Gateway}}{{end}}"'
  if (-not $r.ok -or -not $r.out) { Die 'cadence_default 网络不存在：先按手册 §2.4 启动 stack（podman-compose up -d mysql redis）' }
  return $r.out
}

function Get-DevboxContainer {
  if (Probe 'podman container inspect devbox') { return 'devbox' }
  if (Probe 'podman container inspect cadence_devbox_1') { return 'cadence_devbox_1' }
  Die 'devbox 容器未运行：先按手册 §2.4 启动（podman start devbox 或 compose up）'
}

function Test-ContainerPort($ctr, $gw, $port) {
  $probe = "timeout 5 bash -c 'echo > /dev/tcp/$gw/$port 2>/dev/null' && echo OK || echo FAIL"
  $out = cmd /c "podman exec $ctr bash -c `"$probe`" 2>nul"
  return (($out -join '').Trim()) -eq 'OK'
}

function Undo-Portproxy($ports) { # 失败路径回滚已建 portproxy，避免无留痕残留
  foreach ($p in $ports) {
    $null = cmd /c "netsh interface portproxy delete v4tov4 listenaddress=127.0.0.1 listenport=$p 2>nul"
    Warn "已回滚 portproxy 127.0.0.1:$p"
  }
}

# ---- 通道搭建 ----
function Invoke-Setup {
  $cfg = Load-Config
  $services = Collect-Services $cfg

  # 前置自检：远端可达（此时尚无任何改动，失败零残留）
  foreach ($s in $services) {
    if (-not (Test-RemotePort $s.remoteHost $s.remotePort)) {
      Die "远端 $($s.name) $($s.remoteHost):$($s.remotePort) 不可达——请先连接 VPN 再重试"
    }
    Info "预检通过：$($s.name) → $($s.remoteHost):$($s.remotePort)"
  }

  $gw = Get-BridgeGateway
  Info "podman 网桥网关：$gw（连接地址将形如 ${gw}:23306，稳定不随 DHCP 变化）"
  $ctr = Get-DevboxContainer

  # Windows 侧 portproxy：幂等（先删同监听端口旧规则再建，仅 127.0.0.1，不暴露局域网）
  $created = @()
  foreach ($s in $services) {
    $null = cmd /c "netsh interface portproxy delete v4tov4 listenaddress=127.0.0.1 listenport=$($s.hostPort) 2>nul"
    $null = cmd /c "netsh interface portproxy add v4tov4 listenaddress=127.0.0.1 listenport=$($s.hostPort) connectaddress=$($s.remoteHost) connectport=$($s.remotePort)"
    if ($LASTEXITCODE -ne 0) { Undo-Portproxy $created; Die "portproxy 写入失败（$($s.name) :$($s.hostPort)），请以管理员运行" }
    $created += $s.hostPort
    Info "portproxy：127.0.0.1:$($s.hostPort) → $($s.remoteHost):$($s.remotePort)"
  }

  # VM 侧：渲染服务表 env → cp 两文件 → ssh 执行 setup
  $envFile = Join-Path $env:TEMP 'cadence-relay.env'
  $envLines = @('# name vmport bindaddr hostport')
  foreach ($s in $services) { $envLines += "$($s.name) $($s.vmPort) $gw $($s.hostPort)" }
  ($envLines -join "`n") + "`n" | Set-Content -Encoding ascii -NoNewline $envFile
  podman machine cp "$PSScriptRoot\relay-vm.sh" "${mach}:/tmp/cadence-relay-vm.sh"
  if ($LASTEXITCODE -ne 0) { Undo-Portproxy $created; Die 'machine cp relay-vm.sh 失败' }
  podman machine cp "$envFile" "${mach}:/tmp/cadence-relay.env"
  if ($LASTEXITCODE -ne 0) { Undo-Portproxy $created; Die 'machine cp relay.env 失败' }
  $null = podman machine ssh "sudo mv /tmp/cadence-relay-vm.sh /usr/local/bin/relay-vm.sh && sudo mv /tmp/cadence-relay.env /etc/cadence-relay.env"

  $null = podman machine ssh 'sudo bash /usr/local/bin/relay-vm.sh setup'
  if ($LASTEXITCODE -eq 3) {
    Warn 'VM 侧端口被存量监听占用（如手搭中继 socat-mysql-relay）。'
    $ans = Read-Host '确认拆除占用者并由本脚本接管？(Y/N)'
    if ($ans -notmatch '^[Yy]') { Undo-Portproxy $created; Die '已取消。可先手工拆除占用者后重跑' }
    $null = podman machine ssh 'sudo bash /usr/local/bin/relay-vm.sh setup --force'
  }
  if ($LASTEXITCODE -ne 0) { Undo-Portproxy $created; Die "VM 侧 setup 失败（exit $LASTEXITCODE），日志：podman machine ssh 'tail /var/log/cadence-relay.log'" }

  # 容器内端到端探活
  $fail = @()
  foreach ($s in $services) {
    if (Test-ContainerPort $ctr $gw $s.vmPort) { Info "$($s.name)：容器 → ${gw}:$($s.vmPort) OK" }
    else { Warn "$($s.name)：容器 → ${gw}:$($s.vmPort) FAIL"; $fail += $s.name }
  }

  # 留痕（成功才落盘，teardown 按此精确回滚）
  @{ version = 1; services = $services } | ConvertTo-Json -Depth 4 | Set-Content -Encoding ascii $RelayJson

  # 存量安装目录的 .gitignore 可能早于 relay 存在：补一行防误提交内网 IP 配置
  $gi = Join-Path $InstallDir '.gitignore'
  if (Test-Path $gi) {
    $cur = @(Get-Content $gi)
    if ($cur -notcontains 'relay.json') { Add-Content -Encoding ascii -Path $gi -Value 'relay.json' }
  }

  Info '完成。容器内应用连接地址（podman 网桥网关，稳定）：'
  foreach ($s in $services) { Write-Host "  $($s.name) → ${gw}:$($s.vmPort)" }
  if ($fail.Count -gt 0) { Warn "部分服务未通：$($fail -join '、')——先看 -Status 定位哪一跳" }
}

# ---- 状态查询：逐跳 ----
function Invoke-Status {
  $cfg = Load-Config
  if (-not $cfg) { Die "无 $RelayJson——通道未建立，先运行 setup" }
  $rules = (cmd /c 'netsh interface portproxy show all') -join "`n"
  foreach ($s in $cfg.services) {
    $h1 = ($rules -match ('(?m)^\s*127\.0\.0\.1\s+' + $s.hostPort + '\s+'))
    if ($h1) { Info "$($s.name) hop1 portproxy 127.0.0.1:$($s.hostPort) OK" }
    else { Warn "$($s.name) hop1 portproxy 缺失——重跑 setup 重建" }
  }
  $null = podman machine ssh 'sudo bash /usr/local/bin/relay-vm.sh status'
  if ($LASTEXITCODE -ne 0) { Warn 'VM 侧存在 FAIL 项（见上方 [cadence-relay] 输出）' }
  $ctr = ''
  if ((Probe 'podman container inspect devbox')) { $ctr = 'devbox' }
  elseif ((Probe 'podman container inspect cadence_devbox_1')) { $ctr = 'cadence_devbox_1' }
  if ($ctr -and (Probe 'podman network inspect cadence_default')) {
    $gw = Get-BridgeGateway
    foreach ($s in $cfg.services) {
      if (Test-ContainerPort $ctr $gw $s.vmPort) { Info "$($s.name) hop3 容器 → ${gw}:$($s.vmPort) OK" }
      else { Warn "$($s.name) hop3 容器 → ${gw}:$($s.vmPort) FAIL——VM 中继未监听则重跑 setup" }
    }
  } else { Warn 'devbox 容器或 stack 网络未起，跳过容器侧探测（hop3）' }
  $vpnDown = @()
  foreach ($s in $cfg.services) {
    if (-not (Test-RemotePort $s.remoteHost $s.remotePort)) { $vpnDown += $s.name }
  }
  if ($vpnDown.Count -gt 0) {
    Warn "远端不可达：$($vpnDown -join '、')——中继正常时报 VPN 未连，请先连 VPN"
  } else { Info '远端可达（hop4）——链路完整' }
}

# ---- 对称拆除 ----
function Invoke-Teardown {
  $cfg = Load-Config
  if (-not $cfg) { Die "无 $RelayJson——没有本脚本建立的通道可拆" }
  Write-Host '将删除：'
  foreach ($s in $cfg.services) { Write-Host "  portproxy 127.0.0.1:$($s.hostPort) → $($s.remoteHost):$($s.remotePort)" }
  Write-Host '  VM 内 /usr/local/bin/cadence-relay-*、/etc/cadence-relay.env、wsl.conf 钩子行、socat 包'
  Write-Host "  $RelayJson"
  $ans = Read-Host '确认拆除？(Y/N)'
  if ($ans -notmatch '^[Yy]') { Die '已取消' }
  foreach ($s in $cfg.services) {
    $null = cmd /c "netsh interface portproxy delete v4tov4 listenaddress=127.0.0.1 listenport=$($s.hostPort) 2>nul"
    Info "portproxy 127.0.0.1:$($s.hostPort) 已删"
  }
  podman machine cp "$PSScriptRoot\relay-vm.sh" "${mach}:/tmp/cadence-relay-vm.sh"
  if ($LASTEXITCODE -ne 0) { Die 'machine cp 失败（machine 停止？）——已保留 relay.json，恢复后重跑 -Teardown' }
  $null = podman machine ssh 'sudo mv /tmp/cadence-relay-vm.sh /usr/local/bin/relay-vm.sh && sudo bash /usr/local/bin/relay-vm.sh teardown'
  if ($LASTEXITCODE -ne 0) { Die 'VM 侧 teardown 失败——已保留 relay.json，恢复后重跑 -Teardown' }
  Remove-Item $RelayJson -ErrorAction SilentlyContinue
  Info 'teardown 完成，两侧已还原到通道建立前'
}

# ---- 入口 ----
try {
  if ($Status -and $Teardown) { Die '-Status 与 -Teardown 互斥' }
  if ($Status)   { Invoke-Status; exit 0 }
  if ($Teardown) { Invoke-Teardown; exit 0 }
  Invoke-Setup
  exit 0
} catch {
  Die "未预期的错误：$($_.Exception.Message)"
}
