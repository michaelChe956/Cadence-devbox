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
# DNAT 映射表（派生物）：安装目录一份（容器可见，便于诊断），VM 侧一份（规则输入）
$DnatMapFile = Join-Path (Join-Path $InstallDir 'stack') 'data\relay-map.env'

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

# 地址问句的取值指引：IP 不是"随便填个内网 IP"，而是该域名当前解析出来的地址。
# VPN（aTrust）环境下域名型服务常解析为 198.18.x.x 假 IP——只有宿主发起的连接走隧道。
function Write-AddressHint {
  Write-Host '  提示：IP 用 nslookup <域名> 查（VPN 内网域名常解析为 198.18.x.x 假 IP，不是 10.x.x.x 真实内网段）' -ForegroundColor DarkGray
}

# 合法 IPv4 点分十进制（每段 0-255）：映射表 real_ip/target_ip 的生成端校验
function Test-IPv4($s) {
  if (-not $s) { return $false }
  if ("$s" -notmatch '^(\d{1,3}\.){3}\d{1,3}$') { return $false }
  foreach ($o in ("$s" -split '\.')) { if ([int]$o -gt 255) { return $false } }
  return $true
}

# IPv4 → 32 位无符号（用 [uint64] 承载，避免 PowerShell 对 uint32 位移的符号问题）
function ConvertTo-IPv4UInt($ip) {
  $b = ([System.Net.IPAddress]::Parse($ip)).GetAddressBytes()
  return ([uint64]$b[0] * 16777216) + ([uint64]$b[1] * 65536) + ([uint64]$b[2] * 256) + [uint64]$b[3]
}

# IP 是否落在 CIDR 内（仅 IPv4；用于 R1：真实地址撞网桥子网时拒绝写入映射）
function Test-InSubnet($ip, $cidr) {
  if (-not $ip -or -not $cidr) { return $false }
  $p = "$cidr".Split('/')
  if ($p.Count -ne 2) { return $false }
  if (-not (Test-IPv4 $ip) -or -not (Test-IPv4 $p[0])) { return $false }
  $len = 0
  if (-not [int]::TryParse($p[1], [ref]$len) -or $len -lt 1 -or $len -gt 32) { return $false }
  # 注意：0xFFFFFFFF 在 PowerShell 里按 int32 解析成 -1，转 [uint64] 会抛错，故用十进制。
  $mask = [uint64]4294967295
  if ($len -lt 32) { $mask = ([uint64]4294967295 -shl (32 - $len)) -band [uint64]4294967295 }
  return (((ConvertTo-IPv4UInt $ip) -band $mask) -eq ((ConvertTo-IPv4UInt $p[0]) -band $mask))
}

# 合法域名（RFC 952/1123）：拒绝空串、含空白、首尾为 . 或 -、连续点、单标签 >63、
# 总长 >253、IP 字面量（域名位写 IP 是配置错误，应填地址段）；`-` 为清空语义、
# 不是域名。大小写归一化由调用方落盘前完成（Split-AddrInput 已 ToLowerInvariant）
function Test-HostName($s) {
  if ($null -eq $s) { return $false }
  $s = "$s".Trim()
  if (-not $s) { return $false }
  if ($s.Length -gt 253) { return $false }
  if ($s -match '^(\d{1,3}\.){3}\d{1,3}$') { return $false }
  if ($s -notmatch '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$') { return $false }
  if ($s -match '\.\.') { return $false }
  foreach ($lab in ($s -split '\.')) { if ($lab.Length -gt 63) { return $false } }
  return $true
}

# 地址问句的唯一输入形态："IP:端口" 或 "IP:端口 域名"（域名可选，同一问句，不二次询问）
# 地址段走 Test-AddrFormat、域名段走 Test-HostName；域名段留空=沿用 $oldHost，
# 输入 "-" / none = 显式清空（否则"回车保持原值"会让已录域名永远删不掉）
function Split-AddrInput($raw, $oldHost) {
  $raw = "$raw".Trim()
  $tok = @($raw -split '\s+', 2 | Where-Object { $_ -ne '' })
  $addr = if ($tok.Count -ge 1) { $tok[0] } else { '' }
  if (-not (Test-AddrFormat $addr)) { Die "地址格式应为 IP:端口[ 域名]，收到：$raw" }
  # 局部变量名不叫 $host：$Host 是 PowerShell 内置只读自动变量（大小写不敏感），
  # 给它赋值会抛 "Cannot overwrite variable host because it is read-only"。
  # 对象的属性名仍是 host（relay.json 的键名），不受影响。
  $domain = $oldHost
  if ($tok.Count -gt 1) {
    $h = $tok[1].Trim()
    if ($h -in @('-', 'none')) { $domain = '' }
    elseif (-not (Test-HostName $h)) { Die "域名格式不合法：$h（留空=保持原值；- 或 none=清空）" }
    else { $domain = $h.ToLowerInvariant() }
  }
  return @{ addr = $addr; host = $domain }
}

# 解析 IP:端口 → 服务对象（含推导端口，占用表感知自动顺延）；格式/越界在此显式报错
# $domain 为可选域名（与地址同一次交互录入）；空=$null 时归一化为空串，落盘时省略该键。
# 形参不叫 $host：$Host 是内置只读自动变量（大小写不敏感），同名形参会直接报错。
function New-Service($name, $val, $taken, $domain = '') {
  if (-not (Test-AddrFormat $val)) { Die "$name 地址格式应为 IP:端口[ 域名]，收到：$val" }
  $hp = $val -split ':'
  $ports = Get-RelayPorts ([int]$hp[1]) $taken
  $taken[$ports.hostPort] = $name
  $taken[$ports.vmPort] = $name
  return [pscustomobject]@{
    name = $name; remoteHost = $hp[0]; remotePort = [int]$hp[1]
    hostPort = $ports.hostPort; vmPort = $ports.vmPort; host = "$domain"
  }
}

# 显式指定本地端口（交互修改用）：校验范围/撞栈/撞已分配；vmPort 恒为 hostPort+10000
function New-ServiceWithHostPort($name, $val, $hostPort, $taken, $domain = '') {
  if (-not (Test-AddrFormat $val)) { Die "$name 地址格式应为 IP:端口[ 域名]，收到：$val" }
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
    hostPort = $hostPort; vmPort = $vmPort; host = "$domain"
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
    # host 可选：旧 relay.json 无该键等价于"未录入"（不假定键存在，但已录域名不得丢）
    $old = @($cfg.services | ForEach-Object {
      $h = ''
      if ($_.PSObject.Properties['host']) { $h = [string]$_.host }
      [pscustomobject]@{
        name = $_.name; remoteHost = $_.remoteHost
        remotePort = [int]$_.remotePort; hostPort = [int]$_.hostPort; vmPort = [int]$_.vmPort
        host = $h
      }
    })
  }
  $keepIdx = @()

  if ($old.Count -gt 0) {
    Info '已有通道（编号）：'
    for ($i = 0; $i -lt $old.Count; $i++) {
      $o = $old[$i]
      $dn = ''
      if ($o.host) { $dn = "，域名 $($o.host)" }
      $line = '  {0}. {1} → {2}:{3}（host {4} / vm {5}{6}）' -f ($i + 1), $o.name, $o.remoteHost, $o.remotePort, $o.hostPort, $o.vmPort, $dn
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
  # 地址问句同一行可续写域名（IP:端口[ 域名]）：回车=地址与域名都保持原值，
  # "-" / none=清空域名——没有第二次询问，也不要求用户手改 relay.json
  foreach ($k in $editIdx) {
    $o = $old[$k]
    $keep = "$($o.remoteHost):$($o.remotePort)"
    if ($o.host) { $keep = "$keep $($o.host)" }
    Write-AddressHint
    $val = (Read-Host "新远端地址（IP:端口[ 域名]，回车保留 $keep）").Trim()
    if (-not $val) {
      $sp = @{ addr = "$($o.remoteHost):$($o.remotePort)"; host = $o.host }
    } else {
      $sp = Split-AddrInput $val $o.host
    }
    $val = $sp.addr
    $dn = ''
    if ($sp.host) { $dn = "，域名 $($sp.host)" }
    $np = (Read-Host '新本地端口（回车=自动推导；直接输入数字覆盖）').Trim()
    if ($np -eq '') {
      $obj = New-Service $o.name $val $taken $sp.host
      Info "修改 $($o.name) → $val（host $($obj.hostPort) / vm $($obj.vmPort)$dn）"
    } else {
      $pnum = 0
      if (-not [int]::TryParse($np, [ref]$pnum)) { Die "$($o.name) 本地端口须为数字：$np" }
      $obj = New-ServiceWithHostPort $o.name $val $pnum $taken $sp.host
      Info "修改 $($o.name) → $val（host $($obj.hostPort) / vm $($obj.vmPort)，显式指定$dn）"
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
    # 参数保持纯 IP:端口形态（不解析域名），但覆盖同名服务时旧域名不能丢
    $oldHost = ''
    foreach ($o in $old) { if ($o.name -eq $pn) { $oldHost = $o.host } }
    $obj = New-Service $pn $val $taken $oldHost
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
    $dn = ''
    if ($obj.host) { $dn = "，域名 $($obj.host)" }
    Info "$verb $pn → $val（host $($obj.hostPort) / vm $($obj.vmPort)$note$dn）"
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
      Write-AddressHint
      $val = (Read-Host "远端 $newName 地址（IP:端口[ 域名]，域名可留空）").Trim()
      if (-not $val) { Warn "跳过 $newName（未提供地址）"; continue }
      $sp = Split-AddrInput $val ''
      $names[$newName] = $true
      $obj = New-Service $newName $sp.addr $taken $sp.host
      $note = ''
      if ($obj.hostPort - $obj.remotePort -ne 10000) { $note = '，自动顺延' }
      $dn = ''
      if ($sp.host) { $dn = "，域名 $($sp.host)" }
      Info "新增 $newName → $($sp.addr)（host $($obj.hostPort) / vm $($obj.vmPort)$note$dn）"
      $result += $obj
    } else {
      if ($catNames -notcontains $t) { Die "未知目录项：$t（目录：$($catNames -join ' ') 或 custom）" }
      if ($names.ContainsKey($t)) { Warn "$t 已在清单中，跳过（改地址：不保留该项后新增）"; continue }
      Write-AddressHint
      $val = (Read-Host "远端 $t 地址（例如 10.129.12.75:$($ServiceCatalog[$t])，IP:端口[ 域名]）").Trim()
      if (-not $val) { Warn "跳过 $t（未提供地址）"; continue }
      $sp = Split-AddrInput $val ''
      $names[$t] = $true
      $obj = New-Service $t $sp.addr $taken $sp.host
      $note = ''
      if ($obj.hostPort - $obj.remotePort -ne 10000) { $note = '，自动顺延' }
      $dn = ''
      if ($sp.host) { $dn = "，域名 $($sp.host)" }
      Info "新增 $t → $($sp.addr)（host $($obj.hostPort) / vm $($obj.vmPort)$note$dn）"
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

# 网桥子网 CIDR（如 10.89.0.0/24）：用于 R1——真实地址撞网桥子网时改写规则无法生效。
# 读不到不算失败（返回空串 = 跳过该项校验），不阻断 setup。
function Get-BridgeSubnet {
  $r = RunQuiet 'podman network inspect cadence_default --format "{{range .Subnets}}{{.Subnet}}{{end}}"'
  if (-not $r.ok -or -not $r.out) { return '' }
  return "$($r.out)".Trim()
}

# ---- devbox 容器定位 ----
# 固定容器名（compose 的 container_name）：安装器与手册统一用它。
# 探测只认「在运行」的容器——`podman container inspect` 对已停止容器同样成功，
# 拿已停止的容器当探测目标会让所有探针打空，报出「所有通道全断」的假故障
# （2026-10-27 真机：宿主残留一个 Exited 的 devbox 容器，16 条探针全红）。
function Get-ContainerNames {
  $r = RunQuiet 'podman ps -a --format "{{.Names}}"'
  if (-not $r.ok) { return @() }
  return @($r.out -split '\s+' | Where-Object { $_ })
}

function Get-ContainerRunning([string]$name) {
  $r = RunQuiet "podman container inspect --format ""{{.State.Running}}"" $name"
  if (-not $r.ok) { return $false }
  return ($r.out.Trim() -eq 'true')
}

function Resolve-DevboxContainer {
  # 返回 @{ name; extras; hint }：name 为空 = 没有可用的探测目标（hint 给出处置动作）。
  $candidates = @(@(Get-ContainerNames) | Where-Object { $_ -like '*devbox*' })
  $running = @($candidates | Where-Object { Get-ContainerRunning $_ })
  if ($running.Count -eq 0) {
    if ($candidates.Count -gt 0) {
      return @{ name = ''; extras = $candidates
                hint = "devbox 容器未运行（存在但已停止：$($candidates -join '、')）——先启动：podman start cadence_devbox" }
    }
    return @{ name = ''; extras = @()
              hint = 'devbox 容器不存在：先创建（podman-compose -f stack/compose.yaml up -d devbox）' }
  }
  $name = if ($running -contains 'cadence_devbox') { 'cadence_devbox' } else { $running[0] }
  $extras = @($candidates | Where-Object { $_ -ne $name })
  $hint = ''
  if ($running.Count -gt 1) {
    $hint = "检测到多个在运行的 devbox 容器：$($running -join '、')——请只保留一个（它们挂载同一个 /workspace，并发 git 会撞 index.lock）"
  } elseif ($extras.Count -gt 0) {
    $hint = "检测到多余的 devbox 容器：$($extras -join '、')——建议清理（podman rm -f <名>），避免与 $name 争用同一 /workspace"
  }
  return @{ name = $name; extras = $extras; hint = $hint }
}

function Get-DevboxContainer {
  $r = Resolve-DevboxContainer
  if (-not $r.name) { Die $r.hint }
  if ($r.hint) { Warn $r.hint }
  if ($r.name -ne 'cadence_devbox') {
    Warn "devbox 容器名为 $($r.name)（约定名 cadence_devbox）——建议重跑安装器统一命名"
  }
  return $r.name
}

# ---- 域名解析一致性核对 ----
# 已录域名的服务：核对「该域名当前解析结果」与「已录地址」是否一致。
# 只告警、不改写用户已录地址——用户可能故意填非解析地址（直连 IP / CDN 出口）。
# 解析失败与「远端不可达」分开报：前者多半是 VPN 没连或域名不对。
function Resolve-DomainIp([string]$domain) {
  if (-not $domain) { return @{ ok = $false; msg = '未录入域名' } }
  try {
    $recs = @(Resolve-DnsName -Name $domain -Type A -ErrorAction Stop |
              Where-Object { $_.IPAddress })
    if ($recs.Count -eq 0) { return @{ ok = $false; msg = '无 A 记录' } }
    return @{ ok = $true; ip = [string]$recs[0].IPAddress }
  } catch {
    return @{ ok = $false; msg = "$($_.Exception.Message)" }
  }
}

function Check-DomainResolution($services) {
  foreach ($s in @($services)) {
    $domain = ''
    if ($s.PSObject.Properties['host']) { $domain = "$($s.host)".Trim() }
    if (-not $domain -or $domain -eq '-') { continue }
    $r = Resolve-DomainIp $domain
    if (-not $r.ok) {
      Warn "$($s.name)：域名 $domain 解析失败（$($r.msg)）——请确认 VPN 已连接；不影响本次通道建立"
      continue
    }
    if ($r.ip -ne $s.remoteHost) {
      Warn "$($s.name)：域名 $domain 当前解析 $($r.ip)，与已录地址 $($s.remoteHost) 不一致——可能是地址抄错或假 IP 变了，请确认后重跑 setup"
    }
  }
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

# ---- DNAT 映射表（设计 §5.3/§5.4）----
# 格式：5 段必填 + 可选第 6 段域名（缺省写 '-'）；'#' 注释行、CRLF 容忍。
#   <name> <real_ip> <real_port> <target_ip> <target_port> [domain]
# target_ip 写本次生成时的网桥网关（审计/漂移比对）；DNAT 规则的实际目标由 VM 侧
# 取 relay 服务表的 bindaddr 与 vmPort，本表只作比对与诊断，不参与规则生成。
# 生成端校验：name 合法、IP 合法、端口 1..65535、域名段合法或 '-'。
# (real_ip, real_port) 重复不 Die 而是告警并跳过该条：同一远端端点被两项服务
# 共用时，nft 只能有一条规则命中（先命中者胜），两个目标不可能同时生效；
# 但 relay 通道本身对"同远端端口多实例"是支持的（端口自动顺延），因此这里
# MUST NOT 让 DNAT 的局限阻断 relay 建链（DNAT 是附加能力，失败只告警）。
function New-DnatMapText($services, $gw, $subnet = '') {
  if (-not (Test-IPv4 $gw)) { Die "网桥网关非法：$gw（无法生成映射表的 target_ip）" }
  $lines = @(
    '# generated by relay.ps1 - do not edit'
    '# <name> <real_ip> <real_port> <target_ip> <target_port> [domain]'
  )
  $seen = @{}
  foreach ($s in $services) {
    if (-not (Test-SvcName $s.name)) { Die "映射表服务名非法：$($s.name)（限字母数字与 _-）" }
    if (-not (Test-IPv4 $s.remoteHost)) { Die "映射表 $($s.name) 的真实 IP 非法：$($s.remoteHost)" }
    $rp = [int]$s.remotePort
    $tp = [int]$s.vmPort
    if ($rp -lt 1 -or $rp -gt 65535) { Die "映射表 $($s.name) 的 real_port 超出 1-65535：$rp" }
    if ($tp -lt 1 -or $tp -gt 65535) { Die "映射表 $($s.name) 的 target_port 超出 1-65535：$tp" }
    # R1：真实地址落在 podman 网桥子网内时，容器会直接走本地网桥而不会把包发到 VM
    # 的 prerouting——改写规则永远命中不了，写进去只会误导。告警并跳过（不阻断 relay）。
    if ($subnet -and (Test-InSubnet $s.remoteHost $subnet)) {
      Warn "映射表跳过 $($s.name)：真实地址 $($s.remoteHost) 落在 podman 网桥子网 $subnet 内——容器会直接经网桥访问，改写规则无法生效；relay 通道不受影响"
      continue
    }
    $key = "$($s.remoteHost):$rp"
    if ($seen.ContainsKey($key)) {
      Warn "映射表跳过 $($s.name)：远端端点 $key 与 $($seen[$key]) 相同，nft 只能有一条规则命中（先命中者胜）——该服务用真实地址直连不生效，relay 通道不受影响"
      continue
    }
    $seen[$key] = $s.name
    $dom = '-'
    $h = ''
    if ($s.PSObject.Properties['host']) { $h = "$($s.host)".Trim() }
    if ($h -and $h -ne '-') {
      if (-not (Test-HostName $h)) { Die "映射表 $($s.name) 的域名非法：$h" }
      $dom = $h.ToLowerInvariant()
    }
    $lines += '{0} {1} {2} {3} {4} {5}' -f $s.name, $s.remoteHost, $rp, $gw, $tp, $dom
  }
  return (($lines -join "`n") + "`n")
}

# 原子落盘：先写 .tmp 再 Move-Item -Force（避免半截文件被消费）；-Encoding ascii
# 保证无 BOM（WinPS 5.1 的 utf8 会写 BOM，首行 name 变成 \ufeffmysql 会让 bash 侧失败），
# 手工拼 "`n" + -NoNewline 保证纯 LF（CRLF 会被 nft/bash 拒绝）
function Write-DnatMapFile($text, $path) {
  New-Item -ItemType Directory -Force (Split-Path $path) | Out-Null
  $tmp = "$path.tmp"
  try {
    Set-Content -Encoding ascii -NoNewline -Path $tmp -Value $text
    Move-Item -Force $tmp $path
  } catch {
    Remove-Item -Force $tmp -ErrorAction SilentlyContinue
    throw
  }
}

# 留痕形态（relay.json）：host 仅在非空时写入——旧 relay.json 无该键等价于
# "未录入"，读取侧按属性是否存在判空；抽出为独立函数以便行为测试直接抽取。
function New-RelayConfig($services, $gw) {
  $svcOut = foreach ($s in $services) {
    $o = [ordered]@{
      name = $s.name; remoteHost = $s.remoteHost; remotePort = $s.remotePort
      hostPort = $s.hostPort; vmPort = $s.vmPort
    }
    if ($s.host) { $o['host'] = $s.host }
    [pscustomobject]$o
  }
  return @{ version = 1; gateway = $gw; services = @($svcOut) }
}

# ---- 通道搭建 ----
function Invoke-Setup {
  $cfg = Load-Config
  $services = @(Collect-Services $cfg)
  # 动手改宿主之前先核对域名解析：地址抄错 / 假 IP 变了当场可见（只告警，不改写）
  Check-DomainResolution $services
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
  # DNAT 映射表内容：由内存中的 $services + $gw 渲染，并在此完成生成端校验（§5.3）。
  # 此时 relay.json 尚未落盘（它到成功后才写），所以数据源只能是本次问答结果。
  # 校验失败即终止：Windows portproxy 与 VM 侧都还没有本轮的改动产物。
  $mapText = New-DnatMapText $services $gw (Get-BridgeSubnet)
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
  # DNAT 映射表：同一内容两份——安装目录 stack/data/（容器内只读诊断，已被 .gitignore）
  # 与 VM /etc/cadence-relay-dnat.env（dnat_apply 的输入）。无 BOM、纯 LF、原子替换。
  $mapFile = $DnatMapFile
  try { Write-DnatMapFile $mapText $mapFile }
  catch { Undo-Portproxy $created; Die "映射表落盘失败（$mapFile）：$($_.Exception.Message)" }
  Info "映射表：$mapFile（$($services.Count) 条）"
  podman machine cp "$PSScriptRoot\relay-vm.sh" "${mach}:/tmp/cadence-relay-vm.sh"
  if ($LASTEXITCODE -ne 0) { Undo-Portproxy $created; Die 'machine cp relay-vm.sh 失败' }
  podman machine cp "$envFile" "${mach}:/tmp/cadence-relay.env"
  if ($LASTEXITCODE -ne 0) { Undo-Portproxy $created; Die 'machine cp relay.env 失败' }
  podman machine cp "$retireFile" "${mach}:/tmp/cadence-relay.retired.env"
  if ($LASTEXITCODE -ne 0) { Undo-Portproxy $created; Die 'machine cp retire.env 失败' }
  podman machine cp "$mapFile" "${mach}:/tmp/cadence-relay-dnat.env"
  if ($LASTEXITCODE -ne 0) { Undo-Portproxy $created; Die 'machine cp relay-map.env 失败' }
  $mvCmd = 'sudo mv /tmp/cadence-relay-vm.sh /usr/local/bin/relay-vm.sh'
  $mvCmd = "$mvCmd && sudo mv /tmp/cadence-relay.env /etc/cadence-relay.env"
  $mvCmd = "$mvCmd && sudo mv /tmp/cadence-relay.retired.env /etc/cadence-relay.retired.env"
  $mvCmd = "$mvCmd && sudo mv /tmp/cadence-relay-dnat.env /etc/cadence-relay-dnat.env"
  $null = podman machine ssh $mvCmd
  # 搬迁链的退出码 MUST 检查：链尾是映射表搬进 /etc，失败时 VM 侧仍是旧表，
  # dnat_apply 会按旧表写规则（透明映射静默落后一版），而 relay.json 已按新意图落盘。
  if ($LASTEXITCODE -ne 0) { Undo-Portproxy $created; Die '映射表/服务表搬迁到 /etc 失败（machine 异常？）——已回滚 portproxy' }

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

  # 透明地址探活（DNAT 用例）：容器内按**真实地址**能否连通。
  # 这条是 DNAT 是否生效的唯一端到端证据（规则未建立/映射表陈旧时上面的桥接探活
  # 仍会 OK）。失败只 Warn——DNAT 是附加能力，relay 通道已就绪时不该因此回滚。
  if ($ctr) {
    foreach ($s in $services) {
      if (Test-ContainerPort $ctr $s.remoteHost $s.remotePort) {
        Info "$($s.name)：容器 → 真实地址 $($s.remoteHost):$($s.remotePort) OK（DNAT 生效）"
      } else {
        Warn "$($s.name)：容器 → 真实地址 $($s.remoteHost):$($s.remotePort) FAIL——DNAT 未生效（relay 通道本身不受影响；重跑 relay.ps1 或 relay-vm.sh dnat-apply）"
      }
    }
  }

  # 留痕（成功才落盘，teardown 按此精确回滚）
  $cfgOut = New-RelayConfig $services $gw
  $cfgOut | ConvertTo-Json -Depth 4 | Set-Content -Encoding ascii $RelayJson

  # 存量安装目录的 .gitignore 可能早于 relay 存在：补一行防误提交内网 IP 配置
  $gi = Join-Path $InstallDir '.gitignore'
  if (Test-Path $gi) {
    $cur = @(Get-Content $gi)
    if ($cur -notcontains 'relay.json') { Add-Content -Encoding ascii -Path $gi -Value 'relay.json' }
  }

  # 完成输出同时给两种地址：真实地址是推荐用法（应用配置照旧、零改动），
  # 桥接地址保留兼容（旧配置/排查用）。只打桥接地址会诱导用户去改应用配置，
  # 正是本能力要消除的动作。
  Info '完成。应用连接地址：'
  foreach ($s in $services) {
    Write-Host "  $($s.name) → $($s.remoteHost):$($s.remotePort)（真实地址，推荐：应用配置照旧即可）"
    Write-Host "           ${gw}:$($s.vmPort)（桥接地址，兼容旧配置 / 排查用）"
  }
  if ($fail.Count -gt 0) { Warn "部分服务未通：$($fail -join '、')——先看 -Status 定位哪一跳" }
}

# ---- 配置清单（-List）：只读 relay.json 打印表格，不探活（探活是 -Status 的职责） ----
function Invoke-List {
  $cfg = Load-Config
  if (-not $cfg) { Die "无 $RelayJson——通道未建立，先运行 setup（探活用 -Status）" }
  $gw = $cfg.gateway
  if (-not $gw) { $gw = Get-BridgeGateway }
  Info '已配置通道（relay.json 留痕；探活请用 -Status）：'
  # 不新增域名列：域名不参与容器连接（DNAT 只按 IP 匹配），且本表列宽受
  # "每行 ≤100 字符"约束（tests/devbox/test_relay.py 钉住），域名列最容易踩线
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
  Check-DomainResolution @($cfg.services)
  $rules = (cmd /c 'netsh interface portproxy show all') -join "`n"
  foreach ($s in $cfg.services) {
    $h1 = ($rules -match ('(?m)^\s*127\.0\.0\.1\s+' + $s.hostPort + '\s+'))
    if ($h1) { Info "$($s.name) hop1 portproxy 127.0.0.1:$($s.hostPort) OK" }
    else { Warn "$($s.name) hop1 portproxy 缺失——重跑 setup 重建" }
  }
  # VM 侧输出 MUST 透传给用户：relay-vm.sh 的 log() 全部写 stdout，DNAT 诊断
  # （逐条 OK / 规则缺失 / 映射表缺失 / 漂移告警）都在其中；$null = 会把它整段吞掉，
  # 用户只看到一句无细节的提示。逐行回显。
  podman machine ssh 'sudo bash /usr/local/bin/relay-vm.sh status' | ForEach-Object { Write-Host $_ }
  if ($LASTEXITCODE -ne 0) { Warn 'VM 侧存在 FAIL 项（见上方 [cadence-relay] 输出）' }
  $ctr = ''
  $res = Resolve-DevboxContainer
  if ($res.hint) { Warn $res.hint }
  if ($res.name) { $ctr = $res.name }
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
  Write-Host '  VM 内 /usr/local/bin/cadence-relay-*、/etc/cadence-relay.env、/etc/cadence-relay-dnat.env、DNAT 单元与 nft 表、wsl.conf 钩子行、socat 包'
  Write-Host "  $RelayJson"
  Write-Host "  $DnatMapFile"
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
  # 映射表是派生物：含内网地址，拆除 MUST 一并清掉（否则宿主上残留，用户以为已还原）
  Remove-Item $DnatMapFile -ErrorAction SilentlyContinue
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
