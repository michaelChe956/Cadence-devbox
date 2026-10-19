# cadence relay 宿主侧一键脚本：容器 → VM socat → mirrored 回环 → Windows portproxy → VPN → 远端中间件
# 设计与排障背景见 openspec/changes/add-remote-middleware-relay/ 与手册 devbox/README.md §2.6。
# 用法（仓库根目录打开 PowerShell，管理员自动提升）：
#   powershell -ExecutionPolicy Bypass -File .\devbox\relay.ps1                       # 拉通/更新（问答）
#   powershell -ExecutionPolicy Bypass -File .\devbox\relay.ps1 -MySQL 10.x.x.x:3306  # 指定 mysql（新增/改址）
#   powershell -ExecutionPolicy Bypass -File .\devbox\relay.ps1 -Status               # 逐跳状态
#   powershell -ExecutionPolicy Bypass -File .\devbox\relay.ps1 -List                # 只读查看已配置服务表
#   powershell -ExecutionPolicy Bypass -File .\devbox\relay.ps1 -Teardown             # 对称拆除
#   存量安装目录（非仓库旁 cadence）须带 -InstallDir <目录>，如 -InstallDir D:\testpodman\cadence
param(
  [switch]$Status,
  [switch]$Teardown,
  [switch]$List,
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

# 本地 stack 发布端口（与 compose.yaml 一致，测试钉住）：端口顺延与显式指定均须避开
function Get-StackPortList {
  $stack = @(3000, 3306, 5672, 6379, 8080, 9000, 9001, 15672)
  return $stack
}

# 推导+自动顺延（2026-10-19 裁决）：hostPort=远端+10000、vmPort=hostPort+10000（存量
# mysql 3306→13306/23306、redis 6379→16379/26379 不变，旧 relay.json 无需迁移）；
# 候选端口（host/vm 任一）命中 $taken（已分配服务端口）或本地 stack 发布端口时整体
# +1 重试。多实例同远端端口依次顺延（mysql→13306/23306、mysql_log→13307/23307）；
# 远端 rabbitmq 5672 自动越过 15672 得 15673/25673。远端端口范围 1-45535；顺延后
# hostPort 须 ≤55535（vmPort 恒 ≤65535），越界显式报错。
function Get-RelayPorts($remotePort, $taken) {
  if ($remotePort -lt 1 -or $remotePort -gt 45535) {
    Die "远端端口超出可推导范围（1-45535）：$remotePort"
  }
  $hostPort = $remotePort + 10000
  while ($true) {
    if ($hostPort -gt 55535) {
      Die "顺延越界：远端端口 $remotePort 顺延后 hostPort $hostPort 超上限 55535（vmPort 须 ≤65535）"
    }
    $vmPort = $hostPort + 10000
    $stack = Get-StackPortList
    $busy = ($taken -and $taken.ContainsKey($hostPort)) -or ($stack -contains $hostPort)
    if ($busy) { $hostPort++; continue }
    $busy = ($taken -and $taken.ContainsKey($vmPort)) -or ($stack -contains $vmPort)
    if ($busy) { $hostPort++; continue }
    return @{ hostPort = $hostPort; vmPort = $vmPort }
  }
}

function Load-Config {
  if (Test-Path $RelayJson) {
    try { return Get-Content -Raw $RelayJson | ConvertFrom-Json } catch { Warn 'relay.json 损坏，按重新问答处理'; return $null }
  }
  return $null
}

function Test-AddrFormat($s) { return ($s -match '^(\d{1,3}\.){3}\d{1,3}:\d{2,5}$') }

function Test-SvcName($s) { return ($s -match '^[A-Za-z0-9_-]+$') }

# 解析 IP:端口 → 服务对象（含推导端口，占用表感知自动顺延）；格式/越界在此显式报错
function New-Service($name, $val, $taken) {
  if (-not (Test-AddrFormat $val)) { Die "$name 地址格式应为 IP:端口，收到：$val" }
  $hp = $val -split ':'
  $ports = Get-RelayPorts ([int]$hp[1]) $taken
  $taken[$ports.hostPort] = $name
  $taken[$ports.vmPort] = $name
  return [pscustomobject]@{
    name = $name; remoteHost = $hp[0]; remotePort = [int]$hp[1]
    hostPort = $ports.hostPort; vmPort = $ports.vmPort
  }
}

# 显式指定本地端口（交互修改用）：校验范围/撞栈/撞已分配；vmPort 恒为 hostPort+10000
function New-ServiceWithHostPort($name, $val, $hostPort, $taken) {
  if (-not (Test-AddrFormat $val)) { Die "$name 地址格式应为 IP:端口，收到：$val" }
  $hp = $val -split ':'
  $rp = [int]$hp[1]
  if ($rp -lt 1 -or $rp -gt 45535) { Die "$name 远端端口超出可推导范围（1-45535）：$rp" }
  if ($hostPort -lt 1 -or $hostPort -gt 55535) { Die "$name 本地端口须在 1-55535：$hostPort" }
  $vmPort = $hostPort + 10000
  $stack = Get-StackPortList
  foreach ($p in @($hostPort, $vmPort)) {
    if ($stack -contains $p) { Die "$name 指定端口 $p 撞本地 stack 发布端口，请换一个" }
    if ($taken.ContainsKey($p)) { Die "$name 指定端口 $p 与服务 $($taken[$p]) 的端口冲突，请换一个" }
  }
  $taken[$hostPort] = $name
  $taken[$vmPort] = $name
  return [pscustomobject]@{
    name = $name; remoteHost = $hp[0]; remotePort = $rp
    hostPort = $hostPort; vmPort = $vmPort
  }
}

# 问答/参数合并出服务清单（动态长度）：回显已有（保留/删除）→ 交互修改保留项 → 目录/自定义新增；
# -MySQL/-Redis 参数映射目录项 mysql/redis：清单中同名则更新地址，没有则直接新增。
# 端口分配（2026-10-19 裁决）：未修改保留项沿用旧端口（先入占用表 $taken，不重推导）；
# 修改/参数/新增项按清单顺序走 Get-RelayPorts 占用感知顺延（含越过 stack 端口），
# vmPort 恒为 hostPort+10000；result 按原相对顺序拼装，relay.json 顺序保持、重跑稳定。
function Collect-Services($cfg) {
  $result = @()
  $names = @{}
  $old = @()
  if ($cfg) {
    # JSON 数值 pwsh7 下解析为 Int64、WinPS 为 Int32：统一转 [int]，保证占用表键与
    # 推导/校验的 Int32 探针同型（否则 ContainsKey/-contains 会误判为不冲突）
    $old = @($cfg.services | ForEach-Object {
      [pscustomobject]@{
        name = $_.name; remoteHost = $_.remoteHost
        remotePort = [int]$_.remotePort; hostPort = [int]$_.hostPort; vmPort = [int]$_.vmPort
      }
    })
  }
  $keepIdx = @()

  if ($old.Count -gt 0) {
    Info '已有通道（编号）：'
    for ($i = 0; $i -lt $old.Count; $i++) {
      $o = $old[$i]
      $line = '  {0}. {1} → {2}:{3}（host {4} / vm {5}）' -f ($i + 1), $o.name, $o.remoteHost, $o.remotePort, $o.hostPort, $o.vmPort
      Write-Host $line
    }
    $ans = (Read-Host '保留已有序号（回车=全部保留，a=全部删除，逗号分隔=仅保留所列）').Trim()
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
    if ($keepIdx.Count -lt $old.Count) {
      $removed = @()
      for ($i = 0; $i -lt $old.Count; $i++) { if ($keepIdx -notcontains $i) { $removed += $old[$i].name } }
      Info ("将删除：" + ($removed -join '、') + "（清 portproxy，VM 实例随 setup 回收）")
    }
  }
  $keepIdx = @($keepIdx | Sort-Object)

  # 参数将覆盖的保留项（-MySQL/-Redis 同名）：旧端口让位重分配，交互修改跳过之
  $paramReplaceIdx = @()
  foreach ($j in $keepIdx) {
    foreach ($pn in @('mysql', 'redis')) {
      $pv = @{ mysql = $MySQL; redis = $Redis }[$pn]
      if (-not $pv -or $old[$j].name -ne $pn -or $paramReplaceIdx -contains $j) { continue }
      $paramReplaceIdx += $j
    }
  }

  # 交互修改保留项（2026-10-19）：改远端地址/显式改本地端口；未修改项沿用旧端口不重推导
  $editIdx = @()
  if ($keepIdx.Count -gt 0) {
    $ans = (Read-Host '要修改的已有序号（回车跳过，逗号分隔）').Trim()
    if ($ans -ne '') {
      foreach ($tok in ($ans -split '[,，]')) {
        $n = -1
        if (-not [int]::TryParse($tok.Trim(), [ref]$n) -or $n -lt 1 -or $n -gt $old.Count) {
          Die "修改序号无效：$tok（应为 1-$($old.Count)，或回车跳过）"
        }
        if ($keepIdx -notcontains ($n - 1)) { Die "序号 $n 未在保留列表中，不能修改（保留问答中未保留它）" }
        if ($paramReplaceIdx -contains ($n - 1)) { Warn "序号 $n 由 -MySQL/-Redis 参数更新，跳过"; continue }
        if ($editIdx -notcontains ($n - 1)) { $editIdx += ($n - 1) }
      }
    }
  }
  $editIdx = @($editIdx | Sort-Object)

  # 阶段一：占用表——未修改且未被参数覆盖的保留项端口先入表（顺延避让依据）
  $taken = @{}
  $kept = @{}
  $keptNameList = @()
  foreach ($k in $keepIdx) {
    $o = $old[$k]
    $keptNameList += $o.name
    if ($editIdx -contains $k -or $paramReplaceIdx -contains $k) { continue }
    foreach ($p in @($o.hostPort, $o.vmPort)) {
      if ($taken.ContainsKey($p)) { Die "relay.json 端口 $p 同时属于 $($taken[$p]) 与 $($o.name)，疑似手改损坏" }
      $taken[$p] = $o.name
    }
    $kept[$k] = $o
    $names[$o.name] = $true
  }

  # 阶段二：修改项按原序重分配（地址回车=保留原值；端口回车=自动顺延推导，数字=显式覆盖）
  foreach ($k in $editIdx) {
    $o = $old[$k]
    $val = (Read-Host "新远端地址（回车保留 $($o.remoteHost):$($o.remotePort)）").Trim()
    if (-not $val) { $val = "$($o.remoteHost):$($o.remotePort)" }
    $np = (Read-Host '新本地端口（回车=自动推导；直接输入数字覆盖）').Trim()
    if ($np -eq '') {
      $obj = New-Service $o.name $val $taken
      Info "修改 $($o.name) → $val（host $($obj.hostPort) / vm $($obj.vmPort)）"
    } else {
      $pnum = 0
      if (-not [int]::TryParse($np, [ref]$pnum)) { Die "$($o.name) 本地端口须为数字：$np" }
      $obj = New-ServiceWithHostPort $o.name $val $pnum $taken
      Info "修改 $($o.name) → $val（host $($obj.hostPort) / vm $($obj.vmPort)，显式指定）"
    }
    $kept[$k] = $obj
    $names[$o.name] = $true
  }

  # 阶段三：按原序拼装保留/修改项（参数覆盖项移到参数位），再处理参数项与新增项
  foreach ($k in $keepIdx) {
    if ($paramReplaceIdx -contains $k) { continue }
    $result += $kept[$k]
  }

  foreach ($pn in @('mysql', 'redis')) {
    $val = @{ mysql = $MySQL; redis = $Redis }[$pn]
    if (-not $val) { continue }
    $obj = New-Service $pn $val $taken
    $verb = '参数新增'
    if ($keptNameList -contains $pn) { $verb = '参数更新' }
    if ($names.ContainsKey($pn)) {
      for ($j = 0; $j -lt $result.Count; $j++) {
        if ($result[$j].name -eq $pn) { $result[$j] = $obj; break }
      }
    } else {
      $names[$pn] = $true
      $result += $obj
    }
    $note = ''
    if ($obj.hostPort - $obj.remotePort -ne 10000) { $note = '，自动顺延' }
    Info "$verb $pn → $val（host $($obj.hostPort) / vm $($obj.vmPort)$note）"
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
      $obj = New-Service $newName $val $taken
      $note = ''
      if ($obj.hostPort - $obj.remotePort -ne 10000) { $note = '，自动顺延' }
      Info "新增 $newName → $val（host $($obj.hostPort) / vm $($obj.vmPort)$note）"
      $result += $obj
    } else {
      if ($catNames -notcontains $t) { Die "未知目录项：$t（目录：$($catNames -join ' ') 或 custom）" }
      if ($names.ContainsKey($t)) { Warn "$t 已在清单中，跳过（改地址：不保留该项后新增）"; continue }
      $val = (Read-Host "远端 $t 地址（例如 10.129.12.75:$($ServiceCatalog[$t])，IP:端口）").Trim()
      if (-not $val) { Warn "跳过 $t（未提供地址）"; continue }
      $names[$t] = $true
      $obj = New-Service $t $val $taken
      $note = ''
      if ($obj.hostPort - $obj.remotePort -ne 10000) { $note = '，自动顺延' }
      Info "新增 $t → $val（host $($obj.hostPort) / vm $($obj.vmPort)$note）"
      $result += $obj
    }
  }

  if ($result.Count -eq 0) { Die '未配置任何服务（重跑 setup 须至少保留/新增一项；全部拆除请用 -Teardown）' }
  return $result
}

# ---- 探测工具 ----
function Test-RemotePort($h, $p) {
  # Socket 版（2026-10-19 真机定案：旧客户端对象构造字面量会触发终端管控静默终止）
  try {
    $sock = New-Object System.Net.Sockets.Socket([System.Net.Sockets.AddressFamily]::InterNetwork, [System.Net.Sockets.SocketType]::Stream, [System.Net.Sockets.ProtocolType]::Tcp)
    $ar = $sock.BeginConnect($h, $p, $null, $null)
    $ok = $ar.AsyncWaitHandle.WaitOne(3000)
    if ($ok -and $sock.Connected) { $sock.EndConnect($ar); $r = $true } else { $r = $false }
    $sock.Close()
    return $r
  } catch { return $false }
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
  $services = @(Collect-Services $cfg)
  # 被删/改端口服务（旧 hostPort 不在新清单，覆盖同名换端口）：预检后清 portproxy；
  # VM 侧实例随 setup 回收、boot 监听按旧表对账回收（--retire）
  $stale = @()
  if ($cfg) {
    $newPorts = @($services | ForEach-Object { $_.hostPort })
    foreach ($o in @($cfg.services)) {
      if ($newPorts -notcontains $o.hostPort) { $stale += $o }
    }
  }


  # 前置自检：远端可达（此时尚无任何改动，失败零残留）
  foreach ($s in $services) {
    if (-not (Test-RemotePort $s.remoteHost $s.remotePort)) {
      Die "远端 $($s.name) $($s.remoteHost):$($s.remotePort) 不可达——请先连接 VPN 再重试"
    }
    Info "预检通过：$($s.name) → $($s.remoteHost):$($s.remotePort)"
  }

  # 被删服务清理（预检通过后才开始改动）：Windows 侧 portproxy 即删；
  # VM 侧自有实例由本次 relay-vm.sh setup 按新服务表渲染 env 时一并回收
  foreach ($o in $stale) {
    $null = cmd /c "netsh interface portproxy delete v4tov4 listenaddress=127.0.0.1 listenport=$($o.hostPort) 2>nul"
    Info "已删 $($o.name) 的 portproxy 127.0.0.1:$($o.hostPort)（VM 实例随 setup 回收）"
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

  # VM 侧：渲染服务表 env 与旧表对账文件 → cp → ssh 执行 setup（--retire）
  $envFile = Join-Path $env:TEMP 'cadence-relay.env'
  $envLines = @('# name vmport bindaddr hostport')
  foreach ($s in $services) { $envLines += "$($s.name) $($s.vmPort) $gw $($s.hostPort)" }
  ($envLines -join "`n") + "`n" | Set-Content -Encoding ascii -NoNewline $envFile
  # 旧服务表（无变更则仅表头）：boot 兜底模式下 setup 按三参核验回收被删/改端口
  # 监听（systemd 模式由实例回收覆盖，此对账为幂等 no-op）。bind 用当前网关——
  # 网关漂移时旧监听 argv 对不上会被 VM 侧显式跳过（不动未知）
  $retireFile = Join-Path $env:TEMP 'cadence-relay.retired.env'
  $retLines = @('# name vmport bindaddr hostport')
  foreach ($o in $stale) { $retLines += "$($o.name) $($o.vmPort) $gw $($o.hostPort)" }
  ($retLines -join "`n") + "`n" | Set-Content -Encoding ascii -NoNewline $retireFile
  podman machine cp "$PSScriptRoot\relay-vm.sh" "${mach}:/tmp/cadence-relay-vm.sh"
  if ($LASTEXITCODE -ne 0) { Undo-Portproxy $created; Die 'machine cp relay-vm.sh 失败' }
  podman machine cp "$envFile" "${mach}:/tmp/cadence-relay.env"
  if ($LASTEXITCODE -ne 0) { Undo-Portproxy $created; Die 'machine cp relay.env 失败' }
  podman machine cp "$retireFile" "${mach}:/tmp/cadence-relay.retired.env"
  if ($LASTEXITCODE -ne 0) { Undo-Portproxy $created; Die 'machine cp retire.env 失败' }
  $mvCmd = 'sudo mv /tmp/cadence-relay-vm.sh /usr/local/bin/relay-vm.sh'
  $mvCmd = "$mvCmd && sudo mv /tmp/cadence-relay.env /etc/cadence-relay.env"
  $mvCmd = "$mvCmd && sudo mv /tmp/cadence-relay.retired.env /etc/cadence-relay.retired.env"
  $null = podman machine ssh $mvCmd

  $setupCmd = 'sudo bash /usr/local/bin/relay-vm.sh setup --retire /etc/cadence-relay.retired.env'
  $null = podman machine ssh $setupCmd
  if ($LASTEXITCODE -eq 3) {
    Warn 'VM 侧端口被存量监听占用（如手搭中继 socat-mysql-relay）。'
    $ans = Read-Host '确认拆除占用者并由本脚本接管？(Y/N)'
    if ($ans -notmatch '^[Yy]') { Undo-Portproxy $created; Die '已取消。可先手工拆除占用者后重跑' }
    $null = podman machine ssh "$setupCmd --force"
  }
  if ($LASTEXITCODE -ne 0) { Undo-Portproxy $created; Die "VM 侧 setup 失败（exit $LASTEXITCODE），日志：podman machine ssh 'tail /var/log/cadence-relay.log'" }

  # 容器内端到端探活
  $fail = @()
  foreach ($s in $services) {
    if (Test-ContainerPort $ctr $gw $s.vmPort) { Info "$($s.name)：容器 → ${gw}:$($s.vmPort) OK" }
    else { Warn "$($s.name)：容器 → ${gw}:$($s.vmPort) FAIL"; $fail += $s.name }
  }

  # 留痕（成功才落盘，teardown 按此精确回滚）
  $cfgOut = @{ version = 1; gateway = $gw; services = $services }
  $cfgOut | ConvertTo-Json -Depth 4 | Set-Content -Encoding ascii $RelayJson

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

# ---- 配置清单（-List）：只读 relay.json 打印表格，不探活（探活是 -Status 的职责） ----
function Invoke-List {
  $cfg = Load-Config
  if (-not $cfg) { Die "无 $RelayJson——通道未建立，先运行 setup（探活用 -Status）" }
  $gw = $cfg.gateway
  if (-not $gw) { $gw = Get-BridgeGateway }
  Info '已配置通道（relay.json 留痕；探活请用 -Status）：'
  $fmt = '  {0,-4} {1,-14} {2,-22} {3,-21} {4}'
  Write-Host ($fmt -f '序号', '服务名', '远端地址', 'Windows portproxy', '容器连接地址')
  $i = 0
  foreach ($s in @($cfg.services)) {
    $i++
    $remote = "$($s.remoteHost):$($s.remotePort)"
    $line = $fmt -f $i, $s.name, $remote, "127.0.0.1:$($s.hostPort)", "${gw}:$($s.vmPort)"
    Write-Host $line
  }
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
  $picked = @()
  if ($Status) { $picked += 'Status' }
  if ($Teardown) { $picked += 'Teardown' }
  if ($List) { $picked += 'List' }
  if ($picked.Count -gt 1) { Die ('开关互斥，只能选一：-' + ($picked -join ' -')) }
  if ($Status)   { Invoke-Status; exit 0 }
  if ($Teardown) { Invoke-Teardown; exit 0 }
  if ($List)     { Invoke-List; exit 0 }
  Invoke-Setup
  exit 0
} catch {
  Die "未预期的错误：$($_.Exception.Message)"
}
