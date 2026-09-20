# cadence devbox Windows 首次安装脚本（podman 路线，对齐 devbox/README.md §2.1–2.4；设计 4.6：≤5 步）
# 自动完成：1.检测 podman/machine → 2.建安装目录+拷文件+生成密钥模板 → 3.配置国内镜像加速
#           → 4.建 16 数据卷+拉镜像(默认阿里云 ACR 直拉)+起中间件与 devbox → 5.打印后续提示
# 用法（在仓库根目录——devbox 文件夹的上一层——打开 PowerShell；任意目录则 -File 用绝对路径）：
#   powershell -ExecutionPolicy Bypass -File .\devbox\install.ps1 -Workspace D:\code
param(
  [string]$Image = "crpi-qzp491l6hpbyhd49.cn-hangzhou.personal.cr.aliyuncs.com/cadence/devbox:latest",
  [string]$Workspace = ""
)
$ErrorActionPreference = 'Stop'
if (-not $InstallDir) { $InstallDir = Join-Path (Split-Path $PSScriptRoot -Parent) 'cadence' }

function Info($m) { Write-Host "[cadence] $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[cadence][警告] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "[cadence][错误] $m" -ForegroundColor Red; exit 1 }

# 允许失败的原生探测调用（$ErrorActionPreference=Stop 时 2>$null 会抛 NativeCommandError，先放宽）
function Probe($cmd) {
  $ErrorActionPreference = 'Continue'
  Invoke-Expression "$cmd 2>`$null" | Out-Null
  $code = $LASTEXITCODE
  $ErrorActionPreference = 'Stop'
  return ($code -eq 0)
}

# ---- 第 1 步：检测 podman / Podman Desktop，确保 machine 在跑（README §2.1） ----
Info '第 1/5 步：检测 podman（Podman Desktop）'
if (-not (Get-Command podman -ErrorAction SilentlyContinue)) {
  Die "未检测到 podman。请先安装 Podman Desktop（README §2.1）：winget install RedHat.Podman-Desktop ；装完重开 PowerShell 再运行本脚本。"
}
Info "podman 已就绪：$(podman --version)"
if (Probe 'podman machine inspect --format "{{.State}}"') {
  $ErrorActionPreference = 'Continue'
  $state = (podman machine inspect --format '{{.State}}' 2>$null | Out-String).Trim()
  $ErrorActionPreference = 'Stop'
} else { $state = '' }
if (-not $state) {
  Info '未发现 podman machine，初始化（首次约 3–5 分钟，可能提示重启 Windows 开虚拟化）…'
  podman machine init
  if ($LASTEXITCODE -ne 0) { Die 'podman machine init 失败：确认安装后已按提示重启过 Windows 再重试' }
} elseif ($state -notmatch 'Running') {
  Info '启动 podman machine…'
  podman machine start
  if ($LASTEXITCODE -ne 0) { Die 'podman machine start 失败：请把上方错误反馈维护者' }
}
if (-not (Get-Command podman-compose -ErrorAction SilentlyContinue)) {
  Die "未检测到 podman-compose。装法（README §2.1）：winget install Python.Python.3.12 后新开 PowerShell，再 pip install podman-compose -i https://pypi.tuna.tsinghua.edu.cn/simple，然后重跑本脚本"
}

# ---- 第 2 步：生成安装目录 cadence-box.yaml 与 stack（README §2.2/2.3） ----
Info "第 2/5 步：生成安装目录 $InstallDir"
if (-not $Workspace) {
  $Workspace = Read-Host '请输入宿主代码父目录绝对路径（例如 D:\code，将挂载为容器内 /workspace）'
}
if (-not (Test-Path $Workspace)) { Die "目录不存在：$Workspace" }
New-Item -ItemType Directory -Force -Path "$InstallDir\stack" | Out-Null
if (Test-Path "$InstallDir\stack\compose.yaml") {
  Warn 'stack\compose.yaml 已存在，保留不覆盖（含 stack add 自加服务；要重置官方模板请先手动删除再重跑）'
} else {
  Copy-Item "$PSScriptRoot\compose.yaml" "$InstallDir\stack\compose.yaml"
}
if (Test-Path "$InstallDir\stack\docker-compose.no-sock.yml") {
  Warn 'stack\docker-compose.no-sock.yml 已存在，保留不覆盖'
} else {
  Copy-Item "$PSScriptRoot\stack\docker-compose.no-sock.yml" "$InstallDir\stack\docker-compose.no-sock.yml"
}
if (Test-Path "$InstallDir\stack\catalog") {
  Warn 'stack\catalog 已存在，保留不覆盖（含自定义 conf；要重置请先手动删除再重跑）'
} else {
  Copy-Item "$PSScriptRoot\stack\catalog" "$InstallDir\stack\catalog" -Recurse
}
if (Test-Path "$InstallDir\cadence-box.yaml") {
  Warn "cadence-box.yaml 已存在，保留不覆盖（如需重新生成请先改名备份）"
} else {
  Copy-Item "$PSScriptRoot\cadence-box.yaml.example" "$InstallDir\cadence-box.yaml"
  Warn "已生成 cadence-box.yaml 模板——该文件=密钥，勿提交勿分享"
}
# .env：工作区/镜像/启用 profile（默认空=仅 mysql+redis）——已存在则保留，防抹掉用户启用的 profile
if (Test-Path "$InstallDir\stack\.env") {
  Warn "stack\.env 已存在，保留不覆盖（改工作区/镜像请手动编辑它）"
  $envImage = (Select-String -Path "$InstallDir\stack\.env" -Pattern '^CADENCE_DEVBOX_IMAGE=(.+)$').Matches[0].Groups[1].Value.Trim()
  if ($envImage) { $Image = $envImage }   # 容器跟随 .env 里的镜像，保证与配置一致
} else {
  $ws = ($Workspace -replace '\\', '/').TrimEnd('/')
  "CADENCE_WORKSPACE=$ws`nCADENCE_DEVBOX_IMAGE=$Image`nCOMPOSE_PROFILES=" | Set-Content "$InstallDir\stack\.env" -Encoding ascii
}
# 本地 .gitignore（安装目录不是 git 仓库时亦预留）
"cadence-box.yaml`nstack/.env`nstack/CHANGES.md`nstack/data/`n" | Set-Content "$InstallDir\.gitignore" -Encoding ascii
Info "请编辑 $InstallDir\cadence-box.yaml 填写 provider 端点/key/模型（git.name/email 也要填）"
$done = Read-Host '填完后按回车继续（Ctrl+C 退出先去填）'

# ---- 第 3 步：宿主侧写 registry mirror（README §2.2.1） ----
# podman remote 机制：Windows 客户端读本机 %APPDATA%\containers 配置并随 pull 生效——不用 machine ssh。
# 无条件重写（内容固定幂等，兼自愈旧坏文件）；同时清理旧版脚本误写进 machine 的尝试（若有）。
Info '第 3/5 步：配置镜像加速（daocloud mirror → 宿主 registries.conf.d）'
$confDir = Join-Path $env:APPDATA 'containers\registries.conf.d'
New-Item -ItemType Directory -Force -Path $confDir | Out-Null
$confFile = Join-Path $confDir '999-mirror.conf'
@('[[registry]]', 'prefix = "docker.io"', 'location = "docker.m.daocloud.io"', '') | Set-Content -Path $confFile -Encoding ascii
Info "已写入 $confFile"
# 自愈：registries.conf.d 里系统层不可读的 .conf 会卡死一切 podman pull（实测 Windows 上
# Podman Desktop 生成的 999-podman-desktop-registries-from-host.conf 可报 "cannot be accessed by the system"）。
# containers/image 只读 *.conf——改名 .bak 即绕过，Podman Desktop 之后会自行重建。
Get-ChildItem "$confDir\*.conf" | ForEach-Object {
  $f = $_   # 注意：catch 块内 $_ 是异常对象——文件对象必须先存变量
  try { Get-Content -Raw -LiteralPath $f.FullName -ErrorAction Stop | Out-Null }
  catch {
    Warn "registries.conf.d\$($f.Name) 系统无法读取（会卡死一切 podman pull）——尝试绕过"
    try { Rename-Item -LiteralPath $f.FullName -NewName "$($f.Name).unreadable.bak" -ErrorAction Stop }
    catch { try { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop } catch {} }
    if (Test-Path -LiteralPath $f.FullName) {
      Die "无法绕过不可读的 $($f.FullName)——请手动删除后重跑：Remove-Item -LiteralPath '$($f.FullName)' -Force"
    }
    Warn "已绕过（Podman Desktop 之后会自行重建该文件）"
  }
}
Probe "podman machine ssh 'sudo rm -f /etc/containers/registries.conf.d/999-mirror.conf'" | Out-Null

# ---- 第 4 步：预创建 external 卷 + 拉镜像 + 起中间件与 devbox（README §2.2/2.4） ----
Info '第 4/5 步：创建数据卷并启动'
$vols = @('cadence-claude','cadence-codex','cadence-pi','cadence-kimi','cadence-agents','cadence-omp',
          'cadence-m2','cadence-npm','cadence-npm-global','cadence-uv','cadence-pip','cadence-gradle',
          'cadence-mysql-data','cadence-redis-data','cadence-rabbitmq-data','cadence-minio-data')
foreach ($v in $vols) { if (-not (Probe "podman volume exists $v")) { podman volume create $v | Out-Null } }
if (-not (Probe "podman image inspect $Image")) {
  # 拉取顺序（2026-09-11 podman 实测）：默认走阿里云 ACR（免代理免登录直拉）。
  # 若 -Image 是 ghcr.io 源：南大 ghcr 镜像优先（国内快、覆盖本包），失败退直连；拉到后 retag 成 -Image 正式名。
  # 注：daocloud 只接 docker.io 上游（中间件 mirror 专用，见第 3 步）；ghfast/gh-proxy 等 gh 代理仅适用于 git clone。
  $px = $Image -replace '^ghcr\.io/', 'ghcr.nju.edu.cn/'
  $tries = @($px)
  if ($px -ne $Image) { $tries += $Image }
  $src = $null
  foreach ($t in $tries) {
    Info "拉取镜像：$t（压缩 872MB，视网速 2–15 分钟）…"
    podman pull $t
    if ($LASTEXITCODE -eq 0) { $src = $t; break }
  }
  if ($src) {
    if ($src -ne $Image) { podman tag $src $Image; podman rmi $src | Out-Null }
  } else {
    Warn "拉取失败：可走 README §1 路线 C 离线 tar（podman load -i devbox-image.tar.gz）后以 -Image localhost/cadence-devbox:dev 重跑；或 -Image ghcr.io/michaelche956/cadence-devbox:latest 切 GHCR（自动南大回退）"
  }
}
podman-compose -f "$InstallDir\stack\compose.yaml" up -d mysql redis
if ($LASTEXITCODE -ne 0) { Die '中间件启动失败：请把上方错误反馈维护者' }
if (Probe 'podman container inspect devbox') { podman rm -f devbox | Out-Null }
podman run -d --name devbox --network cadence_default `
  -v "$InstallDir\cadence-box.yaml:/cadence/auth.yaml:ro" `
  -v "$InstallDir\stack:/cadence/stack" `
  -v "${Workspace}:/workspace" `
  -v /var/run/podman/podman.sock:/var/run/docker.sock `
  -p 127.0.0.1:3000:3000 -p 127.0.0.1:8080:8080 `
  $Image
if ($LASTEXITCODE -ne 0) { Die 'devbox 启动失败：请把上方错误反馈维护者' }
Info '等待 devbox 就绪（entrypoint 首启渲染五端配置）…'
$ready = $false
foreach ($i in 1..30) {
  Start-Sleep -Seconds 1
  $ErrorActionPreference = 'Continue'
  $log = (podman logs devbox 2>&1 | Out-String)
  $ErrorActionPreference = 'Stop'
  if ($log -match '就绪') { $ready = $true; break }
}
if ($ready) { Info 'devbox 已就绪（日志含「就绪」）' }
else { Warn '30 秒内未见就绪日志，稍后手动查看：podman logs devbox' }

# ---- 第 5 步：后续提示 ----
Info '第 5/5 步：完成。后续操作：'
Write-Host @"
  1. 进入容器：podman exec -it devbox bash
     首验：claude --version && stack status && ls /workspace
  2. 你的全部仓库在容器内 /workspace 下，与 Windows 双向同步
  3. 换 key/换模型：改 $InstallDir\cadence-box.yaml → podman restart devbox
  4. 加开中间件（容器内）：stack enable mq   # rabbitmq；stack enable storage   # minio
  5. 日常开关：podman stop devbox / podman start devbox（machine 随 Podman Desktop 起）
"@
Info '安装完成。'
