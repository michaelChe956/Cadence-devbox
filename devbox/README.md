# cadence devbox —— 一体化 AI 开发环境容器 使用手册

> 版本：第一期（2026-09-11）｜ 运行时：**podman**（Windows/macOS/Linux 三平台统一）｜ 镜像压缩体积 872MB ｜ 内置：claude 2.1.247 / codex 0.153.4 / pi 0.85.0 / omp 18.1.17（bun 1.4.2）/ kimi（官方安装器）+ JDK21 + Maven 3.9.16 + Node 24.21.0 + uv 0.12.11 + mysql/redis/rabbitmq/minio 编排
> 完整设计见 `cadence/designs/2026-09-11_方案设计_Cadence-skills一体化开发环境容器devbox_v1.0.md`
> 说明：工具链对接的是 docker 兼容协议（sock/命令语义），全程使用 podman 运行；Linux 路线已实测（一期验收+E2E），**Windows 真机验收已通过**（见 §8），macOS 路线待真机验证。
> JDK 选择（`jdk list` / `jdk use 8|21`，含完整 OpenJDK 8 按需安装与跨重建持久）见 §3.1；其验证状态也记在该节（Linux 实测，Windows/Gradle 未实测）。

## 0. 这是什么、为什么（背景与目的）

devbox 是给**业务开发/测试人员**的一体化 AI 开发环境容器：预装 claude/codex/pi/kimi/omp 五端 CLI 与 Java/前端/Python 工具链，编排 mysql/redis/rabbitmq/minio，鉴权只需维护一份 `cadence-box.yaml`。你装一次 Podman Desktop，剩下的一切——构建、运行、中间件、AI agent——都在容器里；代码始终留在宿主你自己的磁盘上（双向共享）。背景与完整动机见[仓库 README](../README.md#背景与目的)。

## 1. 镜像获取（三选一）

**路线 A：国内 ACR 阿里云（推荐——免代理免登录直拉，公开仓库）**

```bash
podman pull crpi-qzp491l6hpbyhd49.cn-hangzhou.personal.cr.aliyuncs.com/cadence/devbox:latest
```

tag 策略：`latest`（滚动）/ `1.0.0`（当前里程碑）/ `YYYY.WW`（周版，二期 CI 起提供）。

**路线 B：GHCR 公开仓库（海外/有代理时备选）**

```bash
podman pull ghcr.io/michaelche956/cadence-devbox:latest
```

直连慢/超时改用南大 ghcr 镜像拉取，再 retag 成正式名（install.ps1 对 ghcr 源已内置此回退）：

```bash
podman pull ghcr.nju.edu.cn/michaelche956/cadence-devbox:latest
podman tag ghcr.nju.edu.cn/michaelche956/cadence-devbox:latest ghcr.io/michaelche956/cadence-devbox:latest
```

> 各通道实测（2026-09-11，以 podman 实拉为准）：阿里云 ACR / ghcr.io 直连 / 南大 ghcr（ghcr.nju.edu.cn）均可拉；daocloud 只接 docker.io 上游（中间件 mirror 用，见 §2.2.1）；ghfast.top / gh-proxy.com / mirror.ghproxy.com 是 **git 加速代理**（§2.2 路 A 专用），不能拉镜像。

**路线 C：离线 tar**

从维护者处拿到 `devbox-image.tar.gz` 后：

```bash
podman load -i devbox-image.tar.gz     # 导入为 localhost/cadence-devbox:dev
```

## 2. 初次使用

> 首次安装路线：**Windows 优先一键脚本（§2.2，自动完成 §2.2–§2.4 全程）**；macOS/Linux 或想逐条理解时走手工步骤。

### 2.1 安装 podman（一次性，按平台）

**Windows**（Podman Desktop，约 30–60 分钟含一次重启）：

```powershell
winget install RedHat.Podman-Desktop     # 或官网下载安装包
podman machine init                      # 首次初始化（GUI 里点 Initialize 等价）
podman machine start
podman version                           # 验证
winget install Python.Python.3.12        # compose 依赖 Python（已装可跳过）；装完新开一个 PowerShell 再继续
pip install podman-compose -i https://pypi.tuna.tsinghua.edu.cn/simple   # §2.4/一键脚本都依赖它
podman-compose --version                 # 验证，输出版本号即通
```

- 安装器后台自动启用所需 Windows 虚拟化组件，按提示重启一次；提示 BIOS 虚拟化未开时进 BIOS 打开
- `pip` 装包走清华源；`winget install` Python 后 PATH 需新开 PowerShell 才生效——`podman-compose` 报「无法识别」九成是这两条没做
- 国内加速：一键脚本已内置（§2.2 第 3 步）；手工安装直连慢再配（§2.2.1）

**macOS**：

```bash
brew install --cask podman-desktop      # 或仅 CLI：brew install podman
podman machine init --now
podman version
```

**Linux**（rootless，一次性三件套见 §2.5）：

```bash
sudo pacman -S podman                   # Arch；Debian/Ubuntu 用 apt install podman
uv tool install podman-compose          # 三平台统一装 compose（uv 支持 Windows/macOS/Linux）
```

### 2.2 首次安装：一键脚本 install.ps1（推荐）

Windows 装完 §2.1 的 Podman Desktop 后，**优先直接跑一键安装器**——自动完成本节与 §2.3/§2.4 的全部内容。在**仓库根目录**（`devbox` 文件夹的上一层，即 clone 或解压 ZIP 得到的目录，如 `C:\Users\你\Downloads\Cadence-devbox-main`）打开 PowerShell：

```powershell
cd C:\Users\你\Downloads\Cadence-devbox-main       # 进入仓库根（devbox 的上一层；.\devbox\ 相对路径依赖它）
powershell -ExecutionPolicy Bypass -File .\devbox\install.ps1 -Workspace D:\code
```

懒 cd 的话任意目录用绝对路径也行：`-File C:\Users\你\Downloads\Cadence-devbox-main\devbox\install.ps1`

参数：`-Workspace`（代码父目录，不传则交互询问）、`-InstallDir`（默认 devbox 旁 `cadence\`）、`-Image`（默认 ACR 地址，见 §1 路线 A）。

> ⚠️ **已有安装目录在别处时必须显式传 `-InstallDir`**（如 `-InstallDir D:\testpodman\cadence`）：默认值只认「`devbox\` 的上一层 + `cadence`」，不传就会在仓库里装出**第二套**（旧 compose.yaml 无固定容器名、`.env` 不重新生成，症状是容器名回落 + 启动失败）。

脚本自动做的 5 步：

| 步 | 内容 | 对应手工段落 |
|---|---|---|
| 1 | 检测 podman；machine 未建自动 `init`、未运行自动 `start`；检测 podman-compose | §2.1 |
| 2 | 建安装目录，拷 compose/no-sock/catalog，生成 cadence-box.yaml 密钥模板 + .env + .gitignore，**中途暂停等你填密钥** | §2.2 下文/§2.3 |
| 3 | 配置国内镜像加速：daocloud mirror 写入**宿主 `%APPDATA%` 与 podman machine `/etc/containers` 两侧**（pull 实际在 machine 内执行，只写宿主侧不生效） | §2.2.1 |
| 4 | 建 17 个数据卷（含 JDK 状态卷 `cadence-jdks`）；拉主镜像（默认**阿里云 ACR**；若 `-Image` 是 ghcr 源则南大优先、失败退直连并 retag）；起 mysql/redis；起 devbox 并等待「就绪」日志 | §2.2 第 4 步/§2.4 |
| 5 | 打印后续使用提示（进容器/换 key/开中间件/日常开关机） | §3 |

**重跑安全**：已存在的 cadence-box.yaml、stack\compose.yaml（含 `stack add` 自加的服务）、catalog、.env 一律保留不覆盖；17 个卷的数据零影响；仅重建 devbox 容器。中途失败从头重跑即可（幂等）。

以下手工步骤供 macOS/Linux 用户、或想逐条理解/脚本不可用时使用：


最终布局（以 Windows `C:\cadence` 为例；macOS/Linux 用 `~/cadence`，命令同理）：

```text
C:\cadence\
├── cadence-box.yaml                ← devbox\cadence-box.yaml.example 拷入后改名（§2.3 填密钥）
└── stack\
    ├── compose.yaml                ← devbox\compose.yaml
    ├── docker-compose.no-sock.yml  ← devbox\stack\docker-compose.no-sock.yml
    └── catalog\                    ← devbox\stack\catalog\（整个目录）
```

**第 1 步：拿到仓库文件（二选一）**

**路 A（本机有 git）**——直连或代理任选其一，克隆后记下其中 `devbox` 目录的完整路径：

```bash
git clone https://github.com/michaelChe956/Cadence-devbox.git                            # 直连
git clone https://ghfast.top/https://github.com/michaelChe956/Cadence-devbox.git         # 代理①
git clone https://gh-proxy.com/https://github.com/michaelChe956/Cadence-devbox.git       # 代理②
git clone https://mirror.ghproxy.com/https://github.com/michaelChe956/Cadence-devbox.git # 代理③
```

**路 B（纯浏览器）**：仓库页 → 绿色 **Code** 按钮 → **Download ZIP** → 解压到任意位置（示例 `C:\Users\你\Downloads\`，得到 `Cadence-devbox-main\`）。下载慢时直接浏览器开代理地址：`https://ghfast.top/https://github.com/michaelChe956/Cadence-devbox/archive/refs/heads/main.zip`

**第 2 步：打开 PowerShell，复制 4 项文件**

任务栏搜索框输入 `powershell` → 回车打开（必须是 PowerShell，不是 CMD，语法不通用）。逐条粘贴执行，`$repo` 那行先改成第 1 步的实际路径：

```powershell
mkdir C:\cadence\stack -Force
$repo = "C:\Users\你\Downloads\Cadence-devbox-main\devbox"
Copy-Item "$repo\compose.yaml"                     C:\cadence\stack\compose.yaml
Copy-Item "$repo\stack\docker-compose.no-sock.yml" C:\cadence\stack\docker-compose.no-sock.yml
Copy-Item "$repo\stack\catalog"                    C:\cadence\stack\catalog -Recurse
Copy-Item "$repo\cadence-box.yaml.example"         C:\cadence\cadence-box.yaml
```

不想敲命令？资源管理器等价操作：

1. `Win+E` 打开资源管理器 → C 盘新建文件夹 `cadence`，进去再新建 `stack`
2. 进入解压出的 `devbox` 文件夹，按上方布局把 4 项分别复制到对应位置（`catalog` 整个文件夹拖进 `stack`）
3. `cadence-box.yaml.example` 粘贴到 `C:\cadence` 后重命名为 `cadence-box.yaml`——先勾上「查看 → 文件扩展名」，否则 `.example` 后缀去不干净

**第 3 步：写 `stack\.env`（三行）**

下面命令**整行复制粘贴**到 PowerShell 执行（不要用 `@"…"@` here-string 多行粘贴——控制台下易碎，也容易把命令原文误写进文件）：

```powershell
"CADENCE_WORKSPACE=D:/code","CADENCE_DEVBOX_IMAGE=crpi-qzp491l6hpbyhd49.cn-hangzhou.personal.cr.aliyuncs.com/cadence/devbox:latest","COMPOSE_PROFILES=" | Set-Content -Encoding utf8 C:\cadence\stack\.env
```

或者：记事本新建 `C:\cadence\stack\.env`，手工录入下面三行（KEY=VALUE，一行一条）：

```ini
CADENCE_WORKSPACE=D:/code
CADENCE_DEVBOX_IMAGE=crpi-qzp491l6hpbyhd49.cn-hangzhou.personal.cr.aliyuncs.com/cadence/devbox:latest
COMPOSE_PROFILES=
```

- `CADENCE_WORKSPACE`：你的代码父目录（几十个 git 仓库的上一级），将整体挂载为容器内 `/workspace`
- `CADENCE_DEVBOX_IMAGE`：默认填阿里云 ACR 地址（§1 路线 A，国内推荐）；备选 GHCR；离线路线改 `localhost/cadence-devbox:dev`

**第 4 步：创建数据卷（external 卷需预建，一次性）**

compose.yaml 里 17 个卷全部声明 `external: true`——podman-compose 只挂载、不创建，卷不存在则启动直接报错；预建后数据与容器生命周期解耦（`down -v`、`system prune` 都删不掉，重建容器/升级镜像数据全在）。三步：

```powershell
podman machine start     # ① 确保 machine 在跑（已启动会提示 already running，继续即可）
# ② 整行复制粘贴——17 个卷逐个创建约 1 秒；重复执行无害（已存在的报错跳过）
"cadence-claude","cadence-codex","cadence-pi","cadence-kimi","cadence-agents","cadence-omp","cadence-jdks","cadence-m2","cadence-npm","cadence-npm-global","cadence-uv","cadence-pip","cadence-gradle","cadence-mysql-data","cadence-redis-data","cadence-rabbitmq-data","cadence-minio-data" | ForEach-Object { podman volume create $_ }
podman volume ls --format "{{.Name}}" | Select-String "^cadence-"    # ③ 验证：应列出 17 行 cadence- 开头的卷
```

macOS/Linux 等价命令（bash 循环）：

```bash
for v in cadence-claude cadence-codex cadence-pi cadence-kimi cadence-agents cadence-omp cadence-jdks cadence-m2 cadence-npm cadence-npm-global cadence-uv cadence-pip cadence-gradle cadence-mysql-data cadence-redis-data cadence-rabbitmq-data cadence-minio-data; do podman volume create "$v"; done
podman volume ls --format '{{.Name}}' | grep -c '^cadence-'   # 应输出 17
```

> 已有安装（一期镜像）**升级到含 JDK 选择特性的版本时必须补建 `cadence-jdks`**：external 卷 compose 不会自建，缺它会直接启动失败。重跑 install.ps1（幂等）或手工 `podman volume create cadence-jdks` 即可，不会动其它卷的数据。详见 §6。

### 2.2.1 国内镜像加速（手工安装直连慢/超时才配；一键脚本第 3 步已自动完成）

> 实测（2026-09-11，podman 实拉）：mysql/redis/rabbitmq/minio 四镜像经 daocloud **全部秒级拉通**；南大 docker 镜像（docker.nju.edu.cn）403 不可用——中间件加速认准 daocloud，主镜像才用南大 ghcr（见 §1）。

中间件镜像（mysql/redis/rabbitmq/minio）都在 docker.io，直连拉不动时配 daocloud 公共加速。跑 §2.2 一键脚本的用户无需动手——脚本第 3 步自动写入下述同名文件（幂等），仅当脚本加速步失败时按本节补救：

**Windows / macOS**（2026-09-20 Windows 实测修正：**pull 在 podman machine（WSL/HyperV VM）内执行、读 VM 的 `/etc/containers` 配置**——日志证据 `Resolving … using unqualified-search registries /usr/share/…/999-podman-machine.conf`。只写宿主本机配置对 docker.io 拉取不生效，两侧都要写；install.ps1 第 3 步已自动完成）：

PowerShell（宿主侧 + machine 侧；`$mach` 为机器名，默认 `podman-machine-default`）：

```powershell
$d = "$env:APPDATA\containers\registries.conf.d"; mkdir $d -Force | Out-Null
@('[[registry]]','prefix = "docker.io"','location = "docker.m.daocloud.io"','') | Set-Content "$d\999-mirror.conf"
$mach = (podman machine inspect --format '{{.Name}}' | Select-Object -First 1).Trim()
podman machine cp "$d\999-mirror.conf" "${mach}:999-mirror.conf"
podman machine ssh 'sudo mkdir -p /etc/containers/registries.conf.d && sudo mv -f ~/999-mirror.conf /etc/containers/registries.conf.d/999-mirror.conf'
podman machine stop; podman machine start   # 关键：machine 内常驻 API service 缓存 registries 配置，不重启则新 mirror 不生效（restart 子命令 6.1 才有，stop+start 全版本等效）
```

  macOS（bash，宿主侧 + machine 侧同理）：

```bash
mkdir -p ~/.config/containers/registries.conf.d
printf '[[registry]]\nprefix = "docker.io"\nlocation = "docker.m.daocloud.io"\n' > ~/.config/containers/registries.conf.d/999-mirror.conf
mach=$(podman machine inspect --format '{{.Name}}' | head -1)
podman machine cp ~/.config/containers/registries.conf.d/999-mirror.conf "${mach}:999-mirror.conf"
podman machine ssh 'sudo mkdir -p /etc/containers/registries.conf.d && sudo mv -f ~/999-mirror.conf /etc/containers/registries.conf.d/999-mirror.conf'
podman machine stop && podman machine start   # 关键：machine 内常驻 API service 缓存 registries 配置，不重启则新 mirror 不生效（restart 子命令 6.1 才有，stop+start 全版本等效）
```

**Linux**（原生 podman，**无 machine**——上面 macOS 块里的 `machine cp/ssh/restart` 三步不适用，只写宿主侧即生效）：`mkdir -p ~/.config/containers/registries.conf.d` 后同 macOS 的 printf 写法。§2.5 三件套里的 `unqualified-search-registries` 是等效的另一写法，配过其一即可。

> devbox 主镜像：首选 §1 路线 A（阿里云 ACR，免代理直拉）；走 GHCR 时注意上面的 daocloud mirror 只管 docker.io——拉不动用南大镜像（ghcr.nju.edu.cn）或路线 C 离线包。


### 2.3 填鉴权文件（唯一要你编辑的文件）

用记事本打开 `C:\cadence\cadence-box.yaml`，填三处：`base_url`（中转/官方端点）、`api_key`、各端用哪个 `model`。git 姓名/邮箱也在这里。

```yaml
git:
  name: 张三
  email: zhangsan@corp.com
providers:
  relay1:
    base_url: http://你的端点/v1
    api_key: sk-你的key
    models:
      - { id: glm-5.3, ctx: 200k }
agents:
  claude: { provider: relay1, model: glm-5.3 }
  codex:  { provider: relay1, model: glm-5.3 }
  pi:     { provider: relay1 }
  kimi:   { provider: relay1 }
  omp:    { provider: relay1, model: glm-5.3 }
```

> ⚠️ 此文件=密钥，勿提交勿分享。换 key/换模型改这里，**`podman stop cadence_devbox` + `podman start cadence_devbox` 生效**（实测 `podman restart` 不会重跑 entrypoint 渲染，配置不生效）。

### 2.4 启动与首验（podman 拓扑）

**中间件与 devbox 全部走 podman-compose**（同一个项目 `cadence`；Windows/macOS 的 machine 默认 rootful，路径由 podman 自动映射进 machine）：

```powershell
cd C:\cadence\stack
podman-compose -f compose.yaml up -d          # 中间件 + devbox（按 .env 的 PROFILES 增减）
# 或分开起：
podman-compose -f compose.yaml up -d mysql redis   # 中间件
podman-compose -f compose.yaml up -d devbox        # devbox 容器

podman logs cadence_devbox | Select-Object -Last 3     # 应见「就绪」
podman exec -it cadence_devbox bash                   # 进入容器
```

- **devbox 容器只有这一条创建路径**，容器名固定为 `cadence_devbox`（compose 的 `container_name`）。不要再手工 `podman run` 造一个 devbox 容器：两个容器会挂载同一个 `/workspace`，容器内并发 git 会撞 `index.lock`。安装器（`install.ps1`）会先清理历史遗留的 devbox 容器（早期手工路线与 compose 默认名），再创建固定名容器，并断言 devbox 类容器至多一个。
- **升级到固定名版本的老安装目录**：安装器**不覆盖**已存在的 `stack\compose.yaml`（保留你 `stack add` 自加的服务），所以固定容器名不会自动生效——手动替换一次即可：
  ```powershell
  Copy-Item D:\testpodman\Cadence-devbox\devbox\compose.yaml D:\testpodman\cadence\stack\compose.yaml -Force
  ```
  替换后再跑 `podman-compose -f compose.yaml up -d devbox`，容器名才会变成 `cadence_devbox`。
- 挂载由 compose 统一定义：鉴权文件、stack、`/workspace`、JDK 选择状态卷（`cadence-jdks`，§3.1），以及 12 个 agent 登录态与语言缓存卷（`.claude`/`.codex`/`.pi`/`.kimi`/`.agents`/`.omp`/`.m2`/`.gradle`/`.npm`/`.npm-global`/`.cache/uv`/`.cache/pip`）——登录态与依赖缓存在容器重建后仍在。五端配置每次启动都会按 `cadence-box.yaml` 重新渲染。
- 容器重建（改名、删除后重建、镜像升级）不影响 `/workspace` 里的仓库：它是宿主 bind mount。

macOS/Linux 同命令（路径换 `~/cadence/...`；Linux 需加 `--userns=keep-id`，见 §2.5）。

容器内首验：

```bash
claude --version && codex --version && pi --version && omp --version   # 五端 CLI
stack status                # mysql/redis 运行状态
stack conn mysql            # 标准 JDBC/连接串（可直接抄进 application.yml）
ls /workspace               # 你的全部仓库已可见
```

### 2.5 Linux rootless 特有配置（维护者/高级用户）

> 本节命令在本机 Arch Linux + podman 6.1.1 **实测通过**（一期验收环境）。

```bash
# 1. cgroup 走 cgroupfs（rootless 必配，否则 start/restart 报 systemd dbus 错）
mkdir -p ~/.config/containers
printf '[engine]\ncgroup_manager = "cgroupfs"\n' > ~/.config/containers/containers.conf
# 2. user socket（运维通道依赖）
systemctl --user enable --now podman.socket
# 3. 镜像源（已配过可跳过）
printf 'unqualified-search-registries = ["docker.m.daocloud.io", "docker.io"]\n' >> ~/.config/containers/registries.conf
```

Linux rootless 下 compose 创建的 devbox 同样需要 keep-id 用户命名空间——写进 §2.5 的 `~/.config/containers/containers.conf` 即可（比逐条命令加参数省事）：

```toml
[containers]
userns = "keep-id"
```

sock 挂载源用 `$XDG_RUNTIME_DIR/podman/podman.sock`。

**平台差异与验证状态**：

| 平台 | 状态 | 差异 |
|---|---|---|
| Linux rootless podman | ✅ 已实测（一期验收+E2E） | `--userns=keep-id` 必需；cgroupfs 配置；rabbitmq 命名卷权限问题（mysql/redis/minio 正常） |
| Windows Podman Desktop | ✅ 已验收（2026-09-21，见 §8） | machine 默认 rootful，免 keep-id/cgroup 配置；宿主路径自动映射进 machine |
| macOS podman machine | ⏳ 待真机验证 | 同上（Apple 虚拟化） |

stack 命令（status/restart/logs）已按 label 直连 docker 兼容 API 适配 podman 双形态。

### 2.6 连接你自己的远端中间件（VPN，Windows 专用）

#### 适用场景与前提

> 适用：中间件在**公司内网**、只能从 Windows 宿主经 VPN（如 aTrust）访问——容器直连 VPN 网段不通（VPN 丢弃一切 NAT 转发流量，实测定论）。基于真机验证链路产品化，完整设计见 OpenSpec change `improve-relay-service-config`（含归档前身 `add-remote-middleware-relay`）。

**状态（2026-10-19）**：链路与一键入口均已真机验收通过；`-List`、多实例同远端端口顺延、改址/改端口交互亦经维护者 Windows 真机确认（含 `-Status`/`-Teardown` 回归）。**主入口 = `relay.ps1`（仓库根目录一条命令，见下）**；固化形态为**每服务一个 systemd 单元**（`cadence-relay@mysql`/`cadence-relay@redis`，单服务故障互不影响、开机自恢复）。验收证据：多通道 setup 一次拉通、`-Status` 逐跳全部 OK、`-Teardown` 对称拆除后重建成功；容器内协议级实测 MySQL `SELECT 1=1`、Redis `PING` 返回 `PONG`。

前提清单：

- VPN 在线（setup 前置预检远端可达，不通则报错退出、零改动）
- Windows + Podman Desktop，podman machine 为 **WSL 后端**（其他后端明确拒绝）
- 管理员权限（脚本自动请求 UAC 提权）
- stack 已起（§2.4）——网桥网关来自 `cadence_default` 网络
- devbox 容器在运行（固定容器名 `cadence_devbox`；脚本按**运行态**探测——容器未运行会明确提示"先启动"，不会再被当成探测目标打出满屏 FAIL）

**链路**（4 跳）：容器 → VM socat（绑网桥网关，每服务一个 systemd 单元）→ mirrored 回环 127.0.0.1 → Windows portproxy → VPN → 远端；VM 侧 systemd 优先（`Restart=always` 自恢复），wsl.conf [boot] 兜底为 5 秒巡检的监督循环（等价自恢复；存在外来 [boot] 段时显式失败）。

#### 一键入口 relay.ps1（推荐）

**状态（2026-10-19，WinPS 5.1 真机验收通过）**：多通道 setup（新增/删除/改址）、`-List` 配置查询、`-Status` 逐跳体检、`-Teardown` 拆除与拆除后重建均已完整跑通。在仓库根目录打开 PowerShell 执行即可（脚本自动请求管理员提权）：

```powershell
powershell -ExecutionPolicy Bypass -File .\devbox\relay.ps1            # setup（问答）
powershell -ExecutionPolicy Bypass -File .\devbox\relay.ps1 -List      # 查看已配置服务表
powershell -ExecutionPolicy Bypass -File .\devbox\relay.ps1 -Status    # 逐跳体检
powershell -ExecutionPolicy Bypass -File .\devbox\relay.ps1 -Teardown  # 对称拆除
```

`-Status`/`-List`/`-Teardown` 三开关互斥（同时只能给一个）；`-MySQL`/`-Redis` 参数配合 setup 直给（见「服务的增删改」）。

**`-InstallDir <目录>`**：安装目录（`install.ps1` 装出的 `cadence` 目录，`relay.json` 落在这里），默认 = 仓库父目录下的 `cadence`。仓库克隆在子目录、安装目录在别处时**必须显式指定**（维护者机即此情况）：

```powershell
powershell -ExecutionPolicy Bypass -File .\devbox\relay.ps1 -InstallDir D:\testpodman\cadence
```

**setup 运行顺序**：远端可达预检（VPN 未连即报错退出、零改动）→ 读 podman 网桥网关与子网 → 写 Windows portproxy（仅 127.0.0.1）→ 生成 DNAT 映射表（Windows 安装目录与 VM 侧各一份，见下节）→ `podman machine cp` 四文件 → VM 侧 `relay-vm.sh setup --retire`（末尾自动应用 DNAT 规则）→ 容器内探活（桥接地址 + 真实地址两条证据）→ 全部成功才落盘 relay.json。

#### 透明地址映射：容器内直接用真实 IP:端口

**解决什么问题**：应用里写的是公司内网**真实地址**（如 `10.129.12.75:3306`）——从配置中心拉配置的 Spring 工程尤其改不得。而容器直连这类地址不通（VPN 丢弃 NAT 转发流量，只有 Windows 宿主发起的连接被放行）。启用本能力后，**容器内照旧写真实地址即可连通**：不改工程代码、不改配置中心数据、不改容器镜像/compose，`application.yml` 里的 `10.129.12.75:3306` 原样能连。

真机验证（2026-10-20）：容器内 `mysql -h 10.129.12.75 -P 3306 -u <user> -p -e "SELECT 1"` 返回 `1`；`podman machine stop/start` 后规则自动重建、连接照旧。

**怎么用（三步）**：

1. **跑一次 setup**（本节开头那条 `relay.ps1`），问答里录入真实地址。
2. **应用配置一个字不动**——`application.yml` 的 `10.129.12.75:3306`、配置中心里的值、启动参数，全部保持原样。
3. **照常启动应用**。容器内任何进程（应用、`mvn`、JDBC 客户端、诊断工具）访问该地址都会被自动接管。

旧写法仍然有效：relay 通道打印的**桥接地址**（如 `10.89.0.1:23306`）继续可用，两种地址并存，不需要为了本能力迁移任何配置。

**怎么工作**：relay 通道就绪后，VM 里多一张 nft DNAT 表——容器发往真实地址的包进入 VM 协议栈时被无状态改写到 relay 的桥接监听地址，其后完全复用既有链路：

```text
容器（照旧连 10.129.12.75:3306）
  → VM nft DNAT：目标改写为 10.89.0.1:23306（该服务的 relay 桥接监听地址）
  → VM socat（网关:23306）→ mirrored 回环 127.0.0.1:13306
  → Windows portproxy → VPN → 远端 10.129.12.75:3306
```

规则挂在自建 `table ip cadence_dnat`（priority `dstnat - 1`，早于 podman netavark 的标准 dstnat 链，顺序确定），不碰系统其它规则。容器零特权、零改动；容器重启/重建不影响（规则在 VM 内核态，与容器生命周期无关）。

**开启方式**：跑一次本节开头的 `relay.ps1` setup——同一入口、同一份配置，**无新命令、无新配置文件**。setup 会由当前配置生成**映射表**（`<安装目录>\stack\data\relay-map.env` 一份供排查，VM `/etc/cadence-relay-dnat.env` 一份供应用规则），VM 侧 setup 末尾按表应用规则。映射表是派生物，勿手改。

**地址问句带域名**：地址问句从 `IP:端口` 扩为 `IP:端口[ 域名]`——域名是同一问句的可选第二段，不二次询问、可留空。域名仅留痕（relay.json 的 `host` 字段）供诊断与后续域名场景，**不参与 DNAT 规则生成**（规则只按 IP 匹配）。

```text
远端 kafka 地址（例如 10.129.12.75:9092，IP:端口[ 域名]）: 10.129.30.8:9092
远端 mysql 地址（例如 10.129.12.75:3306，IP:端口[ 域名]）: 10.129.12.75:3306 mysql.corp.local
新远端地址（IP:端口[ 域名]，回车保留 10.129.12.75:3306 mysql.corp.local）: 10.129.20.88:3306 -
```

第三行是修改已有服务：只给地址 = 域名保持原值；清空已录域名须显式给 `-` 或 `none`（直接回车 = 地址与域名都保留）。域名写错当场报错，不写半份配置。

**重启后自动恢复**：nft 规则在内核态、VM 重启即丢，由 VM 侧固化——systemd 可用时单元 `cadence-relay-dnat.service` 开机重放（`ExecStop` 清表）；systemd 不可用时复用既有 wsl.conf `[boot]` 监督循环重放。宿主/VM 重启后无需重跑 setup，容器重启更无影响：

```powershell
podman machine ssh 'systemctl is-enabled cadence-relay-dnat; systemctl is-active cadence-relay-dnat'
```

**怎么确认生效**：三条独立证据，都要看：

1. setup 结束前自动探活，逐服务打印 `容器 → 真实地址 … OK（DNAT 生效）`（桥接探针只证明 relay 通，这条才证明 DNAT 生效；失败只告警，不回滚 relay）。
2. `relay.ps1 -Status` 的 VM 段末尾带 DNAT 诊断（规则表存在性、逐条目标、映射表与服务表一致性、网关漂移）：

```text
[cadence-relay] DNAT: mysql 10.129.12.75:3306 → 10.89.0.1:23306 OK
[cadence-relay] DNAT: redis 10.129.117.17:6379 → 10.89.0.1:26379 OK
```

3. 容器内手动探针（`podman exec -it cadence_devbox bash` 进入后）：`CONNECT-OK` 即已接管，之后应用/JDBC 照旧连真实地址：

```bash
if timeout 5 bash -c 'echo > /dev/tcp/10.129.12.75/3306' 2>/dev/null; then echo CONNECT-OK; else echo CONNECT-FAIL; fi
```

**排障对照表**：

| 看到的输出 | 含义 | 处置 |
|---|---|---|
| setup / `-Status`：`devbox 容器未运行（存在但已停止：…）` | 容器停了——脚本不会拿停止的容器当探测目标 | `podman start cadence_devbox` |
| setup / `-Status`：`devbox 容器不存在` | 容器没建 | `podman-compose -f stack/compose.yaml up -d devbox` |
| setup / `-Status`：`检测到多个…devbox 容器` | 历史上并存过手工容器与 compose 容器，两者挂同一 `/workspace` | 删到只剩一个：`podman rm -f <多余容器名>` |
| setup / `-Status`：`域名 X 当前解析 A，与已录地址 B 不一致` | 地址抄错，或 VPN 重连后假 IP 变了 | 确认后重跑 setup（问句里的 IP 重新 `nslookup` 一次） |
| setup / `-Status`：`域名 X 解析失败` | 多半是 VPN 没连或域名写错 | 连上 VPN 再跑一次 |
| setup：`容器 → 真实地址 … FAIL` | 改写规则未生效（relay 通道本身正常） | 重跑 `relay.ps1`，或只重建规则：`podman machine ssh 'sudo bash /usr/local/bin/relay-vm.sh dnat-apply'` |
| `-Status`：`table ip cadence_dnat 不存在——规则缺失` | 规则缺失（VM 重启后未重放 / 曾手工清表） | 同上 |
| `-Status`：`缺映射表 …` | 映射表缺失（派生物被清） | 重跑 `relay.ps1` |
| `-Status`：`bind … 不在本机网卡上——网桥已重建/网关漂移` | podman 网络重建 | 重跑 `relay.ps1` 重绑 |
| setup：`映射表跳过 X：远端端点 … 相同` | 两项服务写了同一个真实 IP:端口（DNAT 只能一条规则命中） | 正常行为，relay 通道仍可用；该服务用真实地址直连不生效，可改其中一项的端口或接受此限制 |
| setup：`映射表跳过 X：… 落在 podman 网桥子网内` | 真实地址就在容器网段内，容器本来就直连、不经过改写点 | 正常行为，无需处理 |

**什么时候要重跑 setup**：

| 场景 | 要重跑吗 | 说明 |
|---|---|---|
| 改远端地址 / 新增服务 / 删除服务 | **要** | 配置变了，映射表与规则跟着重算 |
| 重启 Windows / WSL / `podman machine` | 不要 | 规则由 VM 侧单元自动重建 |
| 容器重建、换镜像、`podman restart` | 不要 | 规则在 VM 内核态，与容器生命周期无关 |
| 换应用、改应用配置 | 不要 | 应用照旧写真实地址即可 |
| VPN 重连 / 掉线恢复 | 不要 | 通道与规则都还在 |
| podman 网络重建（网关漂移） | **要** | `-Status` 会报 `bind … 不在本机网卡上` |

**怎么撤销**：`relay.ps1 -Teardown` 对称拆除（DNAT 规则、VM 侧单元与映射表、宿主侧映射表一起清）。只想手工清规则本身时（只删自有表、幂等）：

```powershell
podman machine ssh 'sudo nft delete table ip cadence_dnat'
```

**两条真机验证过的坑**：

- VM（Fedora podman machine）内**只有 nft、没有 iptables**——本能力用 nft 实现，不需要也不建议装 iptables（会随 podman machine 升级丢失）。手工加规则时**不要**在 `podman machine ssh` 命令行里敲 `nft add chain ... '{ type nat hook ... }'`：花括号里的空格与分号经 Windows→bash 引号层会被吃掉而报语法错。正确形态是写 `.nft` 文件（**LF 行尾**，CRLF 会被 nft 拒绝）送进 VM 后 `sudo nft -f 文件`——脚本内部就是这么做的。
- PowerShell 里粘贴多行命令会进 `>>` 续行模式，引号容易被弄乱；需要多行时用 here-string 或先写文件再执行。

#### VPN 侧 HTTP 服务（GitLab / Artifactory）

容器里 `git clone` / `git fetch` 走的是同一条 relay 通道——容器直连 VPN 网段不通（aTrust 丢弃一切 NAT 转发流量，只有宿主发起的连接走隧道）。域名型服务还多一层：它们在宿主 DNS 上被 aTrust 解析成**假 IP**（`198.18.x.x`），容器拿到同一个假 IP，但只有宿主能连。

三步：

```powershell
# 1) 查这个域名当前解析成什么（VPN 下通常是假 IP）
nslookup gitlab.tianti.tg.unicom.local
#    → Address: 198.18.0.2

# 2) 加一条通道：relay.ps1 问答填 custom → 服务名 → "198.18.0.2:80 gitlab.tianti.tg.unicom.local"
powershell -ExecutionPolicy Bypass -File .\devbox\relay.ps1 -InstallDir D:\testpodman\cadence

# 3) 容器内直接用（不改任何 git 配置）
podman exec -it cadence_devbox git -C /workspace/authority fetch origin
```

要点：

- **域名和 IP 一起填**：问句接受 `IP:端口 域名`；域名留痕后，setup 与 `-Status` 会核对「当前解析结果 vs 已录地址」，不一致当场告警（只告警，**不改写**你已填的地址）。
- **HTTP Host 头保持不变**：改写只换目标 IP，请求里的 `Host: gitlab…` 原样保留，所以 GitLab 的虚拟主机匹配正常。
- **假 IP 变了怎么办**：VPN 重连后假 IP 可能变 → 容器里 `git fetch` 超时 → 重新 `nslookup` 一次、重跑 `relay.ps1`（幂等，约 30 秒）。
- **凭据**：不要把口令写进 remote URL；容器内用 `git config --global credential.helper store`（明文存 `~/.git-credentials`，记得 `chmod 600`）。

#### 查看与日常（-List / -Status）

**`-List`：只读查看配置**——从 relay.json 打印服务表，**不探活**（探活是 `-Status` 的职责）：

```text
[cadence-relay] 已配置通道（relay.json 留痕；探活请用 -Status）：
  序号  服务名         远端地址             Windows portproxy    容器连接地址
  1    mysql           10.129.12.75:3306       127.0.0.1:13306      10.89.0.1:23306
  2    mysql_log       10.129.12.76:3306       127.0.0.1:13307      10.89.0.1:23307
```

容器连接地址的网关取 setup 时留痕在 relay.json 的 `gateway` 字段（免探测）；旧版 relay.json 无此字段时现场解析网桥网关（需 stack 网络在）。无 relay.json 时报「通道未建立」。

**`-Status`：逐跳体检**（每服务四段）：

| 跳 | 检查什么 | FAIL 指向 |
|---|---|---|
| hop1 | Windows portproxy 规则 127.0.0.1:hostPort 是否存在 | 规则缺失——重跑 setup 重建 |
| hop2 | VM 侧每服务实例监听（pid=MainPID、bind 在网卡、回环一跳） | VM 侧 FAIL 项见其输出；网关漂移时提示重跑 setup 重绑 |
| hop3 | 容器 → 网关:vmPort 可达 | VM 中继未监听——重跑 setup |
| hop4 | 宿主 → 远端可达 | VPN 未连（中继正常时）——先连 VPN |

#### 服务的增删改（重跑 setup 问答即更新）

**问答流程**：先回显已有通道（编号，首跑无此步）并问「保留已有序号（回车=全部保留，a=全部删除，逗号分隔=仅保留所列）」；已有通道时再问「要修改的已有序号（回车跳过，逗号分隔）」逐项改地址/改本地端口；最后问「可新增（目录：mysql redis kafka zookeeper rabbitmq；custom=自定义；回车跳过）」与「新增（多项逗号分隔，如 kafka,zookeeper 或 custom）」，目录项逐项追问远端地址（如「远端 kafka 地址（例如 10.129.12.75:9092，IP:端口[ 域名]）」，域名可留空、同一次录入，见「透明地址映射」），`custom` 先问「自定义服务名（限字母数字与 `_-`，如 es7、db2-prod）」再问地址。

| 操作 | 怎么做 | 行为 |
|---|---|---|
| 新增目录服务 | 新增问答选目录项，给远端 IP:端口 | 端口按远端端口推导/顺延（见下节） |
| 新增自定义 | `custom` → 服务名（限字母数字与 `_-`）→ IP:端口 | 同上；VM 侧实例名 `cadence-relay@<服务名>` |
| 删除服务 | 保留问答只列要保留的序号（`a`=全删） | 当场清该服务 Windows portproxy；VM 实例随 setup 回收（所有权核验后 disable，未知 unit 不动；boot 兜底按旧表对账 `--retire`，三参核验一致才回收） |
| 改远端地址 | 修改问答选序号 → 新地址（回车=地址与域名都保留原值，`-`/`none` 清空域名）→ 本地端口回车 | relay.json 换地址、端口不变（无冲突时推导值=旧值）；旧端口的 portproxy 与 VM 监听一并清除 |
| 改本地端口 | 修改问答选序号 → 地址回车 → 输入数字 | 校验 1-55535、不撞 stack、不与其他服务重复，vmPort 恒=hostPort+10000 同步；违反当场报错说明 |
| 多实例同远端端口 | 直接再配一项（如 `mysql` 与 `mysql_log` 同为远端 3306） | 自动顺延：13306/23306、13307/23307 并存 |
| 参数直给 | `-MySQL`/`-Redis IP:端口` | 清单同名则改地址（端口按现状重分配），没有则新增 |

**顺序确定性**：端口分配按服务在清单中的顺序进行（保留项在前、参数项次之、新增项最后），结果确定、重跑稳定，relay.json 服务顺序保持；未修改的保留项一律沿用旧端口，不重新推导。

**kafka 示例**（已有 mysql/redis，保留全部并新增 kafka 10.129.30.8:9092）：

```text
[cadence-relay] 已有通道（编号）：
  1. mysql → 10.129.12.75:3306（host 13306 / vm 23306）
  2. redis → 10.129.20.31:6379（host 16379 / vm 26379）
保留已有序号（回车=全部保留，a=全部删除，逗号分隔=仅保留所列）: ⏎
要修改的已有序号（回车跳过，逗号分隔）: ⏎
[cadence-relay] 可新增（目录：mysql redis kafka zookeeper rabbitmq；custom=自定义；回车跳过）
新增（多项逗号分隔，如 kafka,zookeeper 或 custom）: kafka
远端 kafka 地址（例如 10.129.12.75:9092，IP:端口[ 域名]）: 10.129.30.8:9092
```

**改 mysql 地址示例**（换 IP、端口回车不动）：

```text
保留已有序号（回车=全部保留，a=全部删除，逗号分隔=仅保留所列）: ⏎
要修改的已有序号（回车跳过，逗号分隔）: 1
新远端地址（IP:端口[ 域名]，回车保留 10.129.12.75:3306）: 10.129.20.88:3306
新本地端口（回车=自动推导；直接输入数字覆盖）: ⏎
[cadence-relay] 修改 mysql → 10.129.20.88:3306（host 13306 / vm 23306）
```

**落盘与结果输出**：各服务先打印容器内探活（桥接地址与真实地址各一条），全部成功后落盘 `<安装目录>\relay.json`（services 动态长度 + `gateway` 留痕），最后打印**两种可用的连接地址**——真实地址是推荐用法（应用配置照旧），桥接地址保留兼容：

```text
[cadence-relay] 完成。应用连接地址：
  mysql → 10.129.12.75:3306（真实地址，推荐：应用配置照旧即可）
          10.89.0.1:23306（桥接地址，兼容旧配置 / 排查用）
  redis → 10.129.117.17:6379（真实地址，推荐：应用配置照旧即可）
          10.89.0.1:26379（桥接地址，兼容旧配置 / 排查用）
```

桥接地址的网关取自 setup 时留痕的 `gateway`，稳定不随宿主 DHCP 变化。

#### 端口规则（推导 + 自动顺延）

通道端口按**远端端口**推导：hostPort=远端+10000、vmPort=hostPort+10000（存量 mysql/redis 值不变，旧 relay.json 无需迁移）：

| 远端端口 | Windows portproxy | VM 中继监听 | 目录示例 |
|---|---|---|---|
| 3306 | 127.0.0.1:13306 | 网关:23306 | mysql |
| 6379 | 127.0.0.1:16379 | 网关:26379 | redis |
| 9092 | 127.0.0.1:19092 | 网关:29092 | kafka |
| 2181 | 127.0.0.1:12181 | 网关:22181 | zookeeper |
| 5672 | 127.0.0.1:15673（顺延） | 网关:25673 | rabbitmq（越过 15672） |
| 任意 P | 127.0.0.1:P+10000 | 网关:P+20000（未顺延时） | 推导规则 |

**多实例同远端端口自动顺延**：候选端口（host 或 vm 任一）命中已分配服务端口或本地 stack 发布端口（3306/6379/5672/15672/9000/9001/3000/8080）时，hostPort/vmPort 同步 +1 重试（vmPort 恒为 hostPort+10000）。多套同类型环境可并存：`mysql`（远端 3306）→ 13306/23306，再配自定义 `mysql_log`（同远端 3306）→ 自动顺延到 13307/23307；远端 rabbitmq 5672 → 越过本地管理台 15672 得 15673/25673（不再要求远端换端口）。推导值与 stack 自带中间件天然错开（如 mysql 本地 13306/容器侧 23306 vs stack 的 3306/6379），两套并存互不影响。远端端口合法范围 1-45535；顺延后 hostPort 须 ≤55535（vmPort 恒 ≤65535），越界显式报错。实际分配端口在输出与 relay.json 中明示（顺延时带「自动顺延」标记）。

> **多实例同远端端点与透明地址映射**：DNAT 规则按「真实 IP:端口」匹配，一个远端端点只能有一条规则命中（先命中者胜）。因此当两项服务指向**同一个**远端 IP:端口（如 `mysql` 与 `mysql_log` 都写 `10.129.12.75:3306`）时，映射表会**告警并跳过**后一项——该服务用真实地址直连不生效，但 **relay 通道本身照常建立**（端口自动顺延不受影响），setup 不会因此失败。
>
> 同理，若某个真实地址**落在 podman 网桥子网内**（如 `10.89.0.x`），容器会直接经网桥访问它、包到不了 VM 的改写点——映射表同样告警并跳过该条，不阻断 relay。

#### 备用入口：交互 WSL 窗口（排查或无 PowerShell 场景）

> 一键入口不可用 / 需手工排查时使用；正常场景优先用上面的 `relay.ps1`。

前提：Windows 侧 portproxy 已按服务表建好（127.0.0.1:13306/16379 → VPN 侧远端）、stack 已起（§2.4）、podman 网桥网关存在——`podman network inspect cadence_default` 查 `Gateway`（下例 10.89.0.1，按实际替换）。

第 1 步：Windows 终端进入 VM 交互 root 窗口（非交互 `systemctl` 会报 Transport 错，必须交互进）：

```powershell
wsl -d podman-machine-default -u root
```

第 2 步：VM 窗口内（提示 getcwd 错误先执行 `cd /root`）。先拿到更新后的脚本——仓库在 Windows 磁盘时经 `/mnt/c` 直读，否则在 Windows 侧 `podman machine cp .\devbox\relay-vm.sh podman-machine-default:/root/relay-vm.sh` 后用 `/root/relay-vm.sh`。设变量并写服务表两行（服务名与端口不得重复）：

```bash
S=/mnt/c/path/to/Cadence-devbox/devbox/relay-vm.sh
printf '%s\n' 'mysql 23306 10.89.0.1 13306' 'redis 26379 10.89.0.1 16379' > /etc/cadence-relay.env
```

第 3 步：先试运行（预期 exit 3 并列出「可接管」的前台监听），确认后再接管：

```bash
bash "$S" setup          # 端口被前台 socat 占用时 exit 3，列占用者
bash "$S" setup --force  # 精确接管：仅 exe+argv 与服务表完全一致的监听
```

`--force` 接管规则（双重身份核验）：`readlink /proc/<pid>/exe` 必须是 socat 本体，且按 NUL 边界读取的完整命令行恰为三参（socat + `TCP-LISTEN:<vmport>,bind=<bindaddr>,fork,reuseaddr` + `TCP:127.0.0.1:<hostport>`），kill 前重核；argv 伪装（exe 不符）、任何归属其它 systemd unit 的占用者一律拒绝（不杀不停不删，exit 3）。接管会断开现有连接（约 1-5 秒空档），请在空闲时明确执行。setup 为事务式：写入前快照被覆盖文件与各实例 enabled/active 状态，任一步失败按快照回滚（还原文件、恢复原实例状态、按原命令行恢复被接管监听），不卸载 socat 包。旧版 `cadence-relay.service` 单元会在所有权核验后安全迁移，非本方案同名文件则拒绝。

第 4 步：验证。VM 侧 `status` 报告每服务 systemd 单元 active、监听 pid=MainPID、bind 在网卡、回环 portproxy 一跳；再从容器内实测：

```bash
bash "$S" status
```

```powershell
# 桥接地址（relay 通道本身）
podman exec cadence_devbox mysql -h 10.89.0.1 -P 23306 -u root -p -e "SELECT 1"
podman exec cadence_devbox redis-cli -h 10.89.0.1 -P 26379 --askpass PING
# 真实地址（走 DNAT；需先按上节跑过 relay.ps1 setup 生成规则）
podman exec cadence_devbox mysql -h 10.129.12.75 -P 3306 -u root -p -e "SELECT 1"
```

固化结果：每服务 `cadence-relay@<name>.service`（`Restart=always`+`RestartSec=5`+`StartLimitIntervalSec=0`：开机网桥未就绪时每 5 秒自动重试直到绑定成功）。宿主/WSL 重启后无需人工干预；podman 网络重建导致网关漂移时 `status` 会明确提示重跑 setup 重绑。

拆除（VM 侧，只清自有痕迹：单元/模板/脚本/服务表/日志/boot 钩子；**不动** socat 包、未知监听与 Windows 侧 portproxy）：

```bash
bash "$S" teardown
```

#### 排障

**零输出退出**——现象：执行 `relay.ps1` 后**没有任何输出**，进程直接结束，退出码 0（看起来像「没反应」）。

成因：终端管控/EDR 按脚本内容特征静默终止进程——2026-10-19 定案为脚本内 `TcpClient` 字面量触发；该写法已移除（改用 `System.Net.Sockets.Socket`，并有测试钉住字面量不再出现），此后真机未再复现。

自查：先跑 `-Status`，看脚本能否正常出声——能打印说明脚本本身正常，问题在提权或某一跳，按输出逐跳定位；若仍零输出，记录终端管控/EDR 的拦截日志（时间点、进程名、规则名/策略名）并反馈。**反馈前不要**自行关闭防护或加白名单。

**VPN 断线**：应用连不上远端是预期行为（中继仍在）；`-Status` 会区分「VPN 未连」与「中继故障」——先连 VPN，通道无需重建。

**端口冲突 / 占用报错**：显式指定本地端口撞 stack 发布端口或与其他服务重复时，当场报错并说明冲突对象（撞 stack / 与哪项服务冲突）——换一个端口或回车改走自动顺延；顺延越界（hostPort 须 ≤55535）同样显式报错；VM 侧端口被存量手搭中继占用时 setup 先 exit 3 报占用者，确认后仅精确接管（规则见备用入口 `--force`）。

**拆除范围差异**：`relay-vm.sh teardown` 只清 VM 侧、不删 Windows portproxy（如需手工删除：`netsh interface portproxy delete v4tov4 listenport=13306` 等按端口精确删除）；`relay.ps1 -Teardown` 会连同两侧（portproxy 与 VM 实例）一起删除。

## 3. 日常使用

| 场景 | 操作 |
|---|---|
| 每天开工 | `podman-compose -f compose.yaml up -d`（在 stack 目录；二次启动秒级）+ `podman start cadence_devbox` |
| 进入容器 | `podman exec -it cadence_devbox bash`，然后直接 `claude` / `codex` / `pi` / `kimi` / `omp` |
| 换 key/换模型 | 改 `..\cadence-box.yaml` → `podman stop cadence_devbox` + `podman start cadence_devbox`（勿用 `restart`，实测不重渲染） |
| 中间件增删启停 | 容器内 `stack` 命令（见 §4），或直接对 agent 说「加个 kafka」「重启 mysql」 |
| 跑项目 | 容器内 `app run myapp -- mvn spring-boot:run`（后台+日志+端口探活），或让 agent 全权 |
| 切 JDK 版本 | 容器内 `jdk list` / `jdk use 8` / `jdk use 21`（见 §3.1；默认内置 Temurin 21，选择跨重建保留） |
| 宿主工具连库 | Navicat 连 `127.0.0.1:3306`（root/cadence123）；浏览器开 `localhost:3000/8080` |
| 下班 | `podman stop cadence_devbox` + `podman-compose -f compose.yaml stop`——凭据/依赖缓存/中间件数据全在卷里，不丢 |

代码工作方式：宿主 IDE 照常编辑 `D:\code`（或 `~/code`）下的仓库，容器内 agent/构建实时看到同一工作树（bind 双向共享）；push 在宿主 git 客户端完成。

### 3.1 JDK 版本选择（容器内 `jdk list` / `jdk use`）

镜像内置 **Temurin 21**（`/usr/lib/jvm/temurin-21-jdk-amd64`）；需要跑老项目时可在容器内切到**完整的 OpenJDK 8 JDK**（`java` 与 `javac` 都是 8，不是只有运行时的 JRE）。选择保存在独立的 `cadence-jdks` 卷里，**重建容器、升级镜像都不丢**；不弹菜单、不自动识别项目、不影响宿主。

```bash
jdk list          # 列出内置/已安装版本，* 标记当前选择
jdk use 21        # 切回镜像内置 Temurin 21
jdk use 8         # 切到 OpenJDK 8；没装过时先按需下载安装（见 §3.1.1）
```

| 场景 | 行为 |
|---|---|
| 全新安装（状态卷为空） | 容器首启自动选**内置 21**；启动日志：`未发现已有 JDK 选择——初始化为镜像内置 21` |
| 已选过版本 → 重建容器/升级镜像 | 沿用上次选择；启动日志：`已有 JDK 选择有效：主版本 …——保留` |
| 命令一致性 | `java`/`javac`/`mvn` 等一律读固定的 `JAVA_HOME=/home/dev/.cadence/jdks/current`，`current/bin` 在 `PATH` 前部：**非交互** `podman exec cadence_devbox java -version`（不读 `.bashrc`）、交互式 shell、容器内 agent 拿到的都是同一个版本 |
| 已运行的 JVM | **不受切换影响**（切换只原子替换 `current` 链接，不改已启动进程的 `java.home`） |
| 切回 / 卸载 | `jdk use 21` 切回；想删掉 8：先 `jdk use 21`，再 `sudo rm -rf /home/dev/.cadence/jdks/8`（不能删当前选中的目录） |

**生效边界（重要）**

- 只影响**切换之后新启动**的 Java 进程。已解析并缓存过 `java` 真实路径的 shell/IDE/守护进程不会热切换；**不要在一次构建过程中切换版本**——正在跑的构建若再起 Java 子进程，可能混用两个版本。
- `PATH` 顺序：`current/bin` → `/usr/local/bin`（兜底入口）→ `/usr/bin`。只有**绝对路径** `/usr/bin/java` 会拿到系统自带的 Temurin 21；普通 `java` 命令永远走当前选择。
- JDK 状态损坏（`current` 悬空）或状态卷未挂载时，兜底入口**明确报错并返回 127**，不会静默回退到系统 Java（否则 `java` 报 21、`mvn` 报 JAVA_HOME 无效，同一次损坏出现两种矛盾表现）。修复：`jdk list` 看状态 → `jdk use 21`。

#### 3.1.1 `jdk use 8` 首次安装：来源与校验

默认按需安装的是**固定版本**（不猜 URL、不静默换源、不降级校验）：

| 项 | 值 |
|---|---|
| 发行物 | Eclipse Temurin **完整 JDK 8**（`temurin-8-jdk`） |
| 版本 / 架构 | `8.0.504.0.0+1-0` / `amd64`（未提供 arm64；非 amd64 上 `jdk use 8` 会明确拒绝） |
| 默认来源 | `https://mirrors.ustc.edu.cn/adoptium/deb/pool/main/t/temurin-8/temurin-8-jdk_8.0.504.0.0+1-0_amd64.deb`（中科大 USTC 的 Adoptium 镜像） |
| 大小 / SHA-256 | 85228180 字节 / `8747c07903772fb7fcfff803e06bd4761124def9d2aef781c98c745c10e6841a` |
| 信任链 | 镜像内 apt 源用 Adoptium 签名密钥（UID `Adoptium GPG Key (DEB/RPM Signing Key) <temurin-dev@eclipse.org>`，指纹 `3B04 D753 C905 0D9A 5D34 3F39 843C 48A5 65F8 F04B`，与发行方 <https://adoptium.net/installation/linux/> 公布一致）校验签名源；版本与 SHA-256 取自该源的签名 `Release → Packages` 元数据，并独立下载复核 |
| 安装步骤 | 下载 → 固定 SHA-256 校验 → 安全解包（校验 Debian 包类型、控制字段身份、成员路径不逃逸）→ 真实 `java`/`javac` 版本检查（都必须是 8）→ 同卷原子发布 → 最后原子替换 `current` |
| 重复执行 | 已安装且校验通过的版本**不重复下载**，直接切换 |
| 失败行为 | 下载失败 / 哈希不符 / 包身份不符 / 缺 `javac` / 版本不是 8 / 中途被中断：**一律非零退出，`current` 与已安装版本一点不动**，并清理安装临时目录（不留半个安装）；被 `SIGKILL`/断电直接杀掉时收不到信号、来不及清理，残留的 `.install.*` 由**下次 `jdk use 8` 安装前**的过期清理回收（只删超过 60 分钟未更新的目录，正在进行的安装不会误删） |

实测（2026-09-23，Linux + rootless podman）：从 USTC 下载 85MB 约 9 秒；`jdk use 8` 后 `java -version` → `1.8.0_504`、`javac -version` → `1.8.0_504`、`mvn -v` → `Java version: 1.8.0_504`。

#### 3.1.2 换源 / 自建镜像 / 离线导入（必须成对配置）

来源与哈希**必须成对**给出；只给其一、或哈希不符都会被拒绝——不存在"只放宽校验"的开关：

```bash
# 一次性（只对本次命令生效）
podman exec -e CADENCE_JDK8_URL=https://你的镜像/temurin-8-jdk_8.0.504.0.0+1-0_amd64.deb \
            -e CADENCE_JDK8_SHA256=<该发行物的独立核实 SHA-256> \
            devbox jdk use 8

# 常驻：devbox 启动命令加 -e（或在 stack/compose.yaml 的 devbox.environment 下加同名两项），之后容器内直接 jdk use 8

# 离线导入：deb 先放到宿主，经挂载点可达后用 file:// 指向（sha 取自你自己的导入记录）
podman exec -e CADENCE_JDK8_URL=file:///cadence/stack/temurin-8-jdk_8.0.504.0.0+1-0_amd64.deb \
            -e CADENCE_JDK8_SHA256=8747c07903772fb7fcfff803e06bd4761124def9d2aef781c98c745c10e6841a \
            devbox jdk use 8
```

- 校验值必须来自**对该发行物的独立核实**（发行方签名元数据、你自建镜像的构建记录等）。不要从下载同一份文件的同一个地方同时取包和哈希——那样证明不了真实性。
- 执行时 stderr 会同时打印**实际使用的来源**与**期望哈希**，便于核对；想让自建源成为**默认**需改 `devbox/jdk.sh` 里的清单（属实现改动，不是配置项，改前请走评审）。
- 校验失败时按提示核对：源被换过 / 镜像站同步滞后 / 你手里的 SHA 不是这个包的——**不要为了"装上去"而改用不校验的路径**。

#### 3.1.3 运行时依赖与已知风险

`jdk use 8` 是**解包安装**（不跑 `apt`），因此不会自动安装 deb 声明的系统依赖；下表是内置 Temurin 21 层构建时 `apt-get install -y --no-install-recommends temurin-21-jdk` 的**实测解析结果**（不是照抄 deb 的声明）：

| 依赖 | 镜像内状态 | 影响与处理 |
|---|---|---|
| `libasound2` | **已预装**（由 `liboss4-salsa-asound2` 满足，提供 `libasound.so.2`） | Temurin 21 的硬依赖，构建时 apt 已自动装入一个有 `Provides: libasound2` 的包：`/usr/lib/x86_64-linux-gnu/libasound.so.2 → liboss4-salsa.so.2.0.0`——是 OSS4 兼容层而非真 ALSA（容器内无音频设备，两者等价）。`java -version`/`javac`/Maven 构建均正常。想换成真 ALSA 库：`sudo apt-get install -y libasound2t64`（noble 里 `libasound2` 只是**虚拟包名**，照旧名安装会 `E: Package 'libasound2' has no installation candidate`）；**该改动不跨重建保留**，要持久请改 `devbox/Dockerfile` |
| 字体 `fonts-dejavu-core`、`fonts-dejavu-mono` | 已预装 | 来源不是 `Recommends`（Dockerfile 用了 `--no-install-recommends`），而是 `libfontconfig1 → fontconfig-config` 的 Depends 备选组选中 `fonts-dejavu-core` 并连带 `fonts-dejavu-mono`。headless 出图/出 PDF（JasperReports、POI 等）的拉丁/希腊/西里尔字形够用；**不含中日韩字形**（中文报表会出方框），中文请另装 `sudo apt-get install -y fonts-noto-cjk`，只需更多拉丁字形则装 `fonts-dejavu-extra` |
| `fontconfig`（`fc-list`/`fc-cache` 命令） | **未预装** | 镜像里只有运行库 `libfontconfig1` 与配置包 `fontconfig-config`，命令包 `fontconfig` 没装（`fc-list` 不可用）。需要时 `sudo apt-get install -y fontconfig`；**该改动不跨重建保留**，要持久请改 `devbox/Dockerfile` |
| `adoptium-ca-certificates`、`java-common` | 已预装 | 无需处理 |

- TLS 信任根由 JDK 自带 `cacerts` 提供，不依赖系统 CA 路径。
- 许可：Temurin 为 GPLv2 + Classpath Exception（Adoptium 发行条款）；对外分发前请走贵司合规确认。

#### 3.1.4 报错速查

| 报错 | 原因 | 处理 |
|---|---|---|
| `不支持的主版本 11（当前可用：21）` | 清单外的主版本（只支持内置 21 与按需的 8） | 用 `jdk use 8` / `jdk use 21`（退出码 3） |
| `CADENCE_JDK8_URL 与 CADENCE_JDK8_SHA256 必须成对配置` | 只配了其中一个 | 两个一起给（§3.1.2） |
| `下载失败：…` | 源不可达/被代理拦/离线 | 换可达镜像或离线 `file://`（§3.1.2）；当前选择不受影响 |
| `SHA-256 校验失败：期望 … 实际 …` | 源上的包与你手里的哈希不一致 | 核对来源与哈希，别绕过校验 |
| `JDK 状态损坏：… 未自动重置` | 状态卷里的 `current` 指向不存在/不可用的目录 | `jdk list` 确认 → `jdk use 21` 修复（不会自动重置你的选择） |
| `JDK 状态目录 … 不存在（状态卷未挂载？）` | 启动命令漏了 `-v cadence-jdks:/home/dev/.cadence/jdks` | 按 §2.4 补挂载后重建容器 |
| `JDK 状态目录 … 不可写（卷属主不是当前用户？）` | 卷属主不是 `dev`（如手工 `mkdir` 成 root） | 重建该卷（`podman volume rm cadence-jdks` 后按 §2.2 第 4 步重来；会丢已装 JDK） |
| `java: JDK 状态不可用：… 已阻止回退到系统 Java` | 兜底入口生效（状态损坏/卷未挂载） | 同上：`jdk use 21`；这是**刻意失败**，不是 bug |

**验证状态**：以下场景已在 **Linux + rootless Podman** 本地真实容器中完成（2026-09-23，基于本地构建镜像）：

- 空 `cadence-jdks` 卷首次启动 → 默认 Temurin 21；`jdk list` 正确标记当前版本。
- `jdk use 8` → 从 USTC 真实下载并校验 85MB 包；`java -version` 与 `javac -version` 均为 `1.8.0_504`，`mvn -v` 使用 `Java version: 1.8.0_504`。
- 删除并重建容器 → 仍使用 8，且已安装包不重复下载；`jdk use 21` 后再次重建 → 仍使用 21。
- 错误 URL、错误 SHA-256、来源不可达、不支持版本、安装中断 → 命令失败且原 `current` 选择不变；损坏状态下 `java`/`javac` 不回退到系统 Java。
- 交互 shell、`podman exec` 非交互命令、Maven，以及 no-sock 等价逐卷挂载拓扑均已验证；本机 `podman-compose` 无法解析 `!override`，因此未直接执行 no-sock Compose 文件。

**未验证**：Windows Podman Desktop 上的同一 JDK 流程（安装器与卷挂载只做静态核对，见 §8）、Gradle CLI（镜像不含 `gradle` CLI，项目自带 `gradlew` 的 JVM 由其读 `JAVA_HOME` 决定）、arm64、macOS。

## 4. 中间件管理（容器内 `stack` 命令）

### 4.1 命令速查

```text
stack ls                    官方目录 + 当前启用 profile + CHANGES.md 摘要
stack status                全部服务运行状态与端口
stack enable/disable <名>   启用/停用（如 enable mq 加 rabbitmq；enable storage 加 minio）
stack restart/logs <名>     重启 / 日志
stack conn <名>             标准连接串 + application.yaml 片段
stack add <名> [--tpl T]    目录外自加服务（自动留痕 CHANGES.md，建议反馈维护者固化）
stack rm <名> [--purge]     删除（--purge 连数据卷，需二次确认）
stack report                环境报告（服务+版本+变更摘要）
app run/stop/logs/port      本地应用生命周期
```

官方目录（已验证、tag pin、随镜像周更扩充）：mysql:8.4、redis:7.4（默认启用）；rabbitmq:3.13-management（profile `mq`）；minio（profile `storage`）。

### 4.2 用户想增加中间件怎么办（两条路，推荐 A）

**路 A：官方目录里有的——一行启用**

目录内组件已做版本 pin、健康检查、连接串模板和国内源验证，直接启用：

```bash
stack enable mq          # 启用 rabbitmq（写 COMPOSE_PROFILES 并拉起）
stack status             # 等到 (healthy)
stack conn rabbitmq      # 拿标准连接串：amqp://...:5672 + 管理台 http://127.0.0.1:15672
```

或者更简单——**对容器内 agent 说一句「把 rabbitmq 打开」**，它会按 devbox-stack skill 协议执行上面全套并回报结果。

**路 B：目录里没有的（kafka / elasticsearch / nacos…）——agent 自加 + 留痕**

对 agent 说「**加个 kafka**」即可。skill 协议会驱动 agent 完成：

1. `stack ls` 查官方目录——确认没有，走自加
2. `stack add kafka --tpl rabbitmq`——用最相近的官方模板生成 service 片段（要求 tag pin、数据卷、healthcheck），追加进 `/cadence/stack/compose.yaml`
3. 自动在 `/cadence/stack/CHANGES.md` 追加留痕行（`- 2026-09-11 add kafka`）——这是模板同步识别用户改动的依据
4. `stack enable`/`up -d` 拉起，`stack conn kafka` 给连接串
5. 提醒你：「这是临时配置，建议反馈维护者固化」

手动执行也是同样两条命令（`stack add` + 拉起），但**别绕过 stack 直接编辑 compose**——不经 skill 的改动不进 CHANGES.md，周更模板同步时会被当作冲突处理。

**想让组件转正进官方目录？三条路（按你意愿选）**：

1. **只自己用**：什么都不用做——路 B 的 `stack add` 结果在你的 `/cadence/stack` 里永久有效（宿主文件+数据卷，重启/重建容器都在），只是不随镜像分发
2. **提 Issue 点菜**：到 [Cadence-devbox Issues](https://github.com/michaelChe956/Cadence-devbox/issues) 提「希望官方目录支持 <组件名>」，附上你的 `stack report` / CHANGES.md 内容更佳——作者据此排期加入，下个周版镜像生效
3. **自己提 PR（或直接找作者加）**：按下方「贡献新组件」清单提交 [Pull Request](https://github.com/michaelChe956/Cadence-devbox/compare)，合并即转正

**贡献新组件的清单**（`devbox/stack/catalog/<组件名>/` 四件套，参照现有 rabbitmq 条目）：

| 文件 | 要求 |
|---|---|
| `profile` | 一行：所属 profile 名（非默认组件用独立 profile，如 `mq`/`storage`/`kafka`） |
| `service.fragment.yaml` | compose 片段：**image tag 精确 pin**（禁 latest）、`127.0.0.1` 端口绑定、数据卷、healthcheck |
| `conn.txt` | 标准连接串 + `application.yaml` 片段（JDBC/URI 格式实测可用，勿踩 `utf8mb4`/`allowPublicKeyRetrieval` 类坑） |
| `conf/`（可选） | 默认配置骨架（如 my.cnf/redis.conf） |

PR 要求：本地 `uv run --with pytest --with pyyaml python -m pytest tests/devbox/ -v` 全绿；若组件进默认启用集，需同步更新 `tests/devbox/test_compose_structure.py` 的服务断言。CI 会自动跑。

### 4.3 换版本 / 删组件

- 换版本：目录内组件改 `/cadence/stack/compose.yaml` 中该 service 的 image tag 后 `stack restart <名>`（agent 可代劳）
- 删组件：`stack rm <名>`；连数据一起删用 `--purge`（会二次确认）

## 5. 实测案例：Java Spring Web + Vue + MySQL + Redis 全链路（2026-09-11 本机实测）

> 环境：Arch Linux + podman 6.1.1。以下数字全部来自真实运行，非设计目标。

### 5.1 拓扑

```text
宿主浏览器 ──:3000──▶ vite dev server ──proxy /api──▶ Spring Boot :8080 ──▶ mysql:3306 / redis:6379
                    （devbox 容器）                  （devbox 容器）      （compose 兄弟容器，服务名直连）
```

代码放在宿主 `~/cadence/ws`（bind 共享），构建/运行全在容器内。

### 5.2 操作流程（6 步，约 8 分钟首跑）

1. **按 §2 起环境**：`stack` 目录下 `podman-compose up -d mysql redis` + `podman run` 起 devbox
2. **写代码**（两种任选）：宿主 IDE 手写，或进容器对 agent 说——
   > 「创建一个最小 Spring Boot 3 应用：`/api/items` 增查走 MySQL（表自动建），`/api/hits` 走 Redis 计数，跑在 8080；再创建 vue3+vite 前端，代理 `/api` 到 8080，dev server 跑 3000 并对宿主可访问」
3. **起后端**：容器内 `app run backend -- mvn spring-boot:run`——首次从 aliyun 拉依赖约 2–4 分钟，**二次启动 1.05 秒**
4. **起前端**：容器内 `npm create vite@latest web -- --template vue`（实测 0.7s）→ 配 proxy → `npm install`（npmmirror，实测 60s 含启动 dev server）
5. **访问**：宿主浏览器开 `http://localhost:3000`
6. **核对数据**：Navicat 连 `127.0.0.1:3306`（root/cadence123）；容器内 `stack logs mysql` / `docker exec … redis-cli GET hits`

### 5.3 实测效果

| 验证点 | 实测结果 |
|---|---|
| Web API 写 MySQL | `POST /api/items` → `{"ok":true,"name":"第一条-E2E写入"}`；库内 `select` 与 API 返回一致，**中文无损** |
| Web API 读 MySQL | `GET /api/items` 返回 JSON 列表（id/name/created_at），经 vite 代理与直连 8080 结果一致 |
| Redis 计数 | curl 路径 2→3；**浏览器页面路径累计到 12**（redis-cli 直查同步） |
| 页面 | 浏览器渲染「devbox E2E — SpringBoot + MySQL + Redis + Vue」：输入框写库、列表实时刷新、计数按钮即时累加（截图留证） |
| 服务名连通 | 容器内 `mysql`/`redis` DNS 解析 + TCP 直连 OK |
| 双向工作树 | 宿主写代码→容器构建运行；容器产物宿主可见 |

顺带战果：该 E2E 抓出官方连接串模板 2 个必挂 bug（`characterEncoding=utf8mb4`、缺 `allowPublicKeyRetrieval`），已修复并烧进镜像。

### 5.4 参考计时（验收 3 探针，依赖预热后）

`mvn archetype:generate` 15s ｜ spring-boot-starter-web 依赖拉取 20s ｜ `npm create vite` 0.7s ｜ `uv init` 0.1s

## 6. 如何更新

**镜像更新（周更）**

```bash
cd <安装目录>/stack
# 改 .env 中 CADENCE_DEVBOX_IMAGE 为新周版 tag（如 :2026.38），然后：
podman pull crpi-qzp491l6hpbyhd49.cn-hangzhou.personal.cr.aliyuncs.com/cadence/devbox:2026.38
podman rm -f cadence_devbox && # 按启动命令重建（建议存成 start-devbox 脚本）
# 离线路线：podman load -i 新包，改 .env 指向新 tag，重建 devbox
```

**skills 更新（自动）**：每次容器启动 entrypoint 自动执行 `install.sh update`（幂等；失败仅告警不阻断，下次启动重试）。

**升级到含 JDK 选择特性的版本（一期镜像 → 本期）**

1. 拉取新镜像（见上）
2. **补建 `cadence-jdks` 卷**：external 卷 compose 不会自建，缺它启动直接报错——重跑 `install.ps1`（幂等）或手工 `podman volume create cadence-jdks`
3. 按启动命令重建 devbox；进容器 `jdk list` 应显示 `21 内置`，需要 8 就 `jdk use 8`

> 补建卷不动任何旧数据（一期安装无此卷）；新卷首启前 `jdk list` 显示「当前选择：未设置（下次启动初始化为内置 21）」属正常。

**数据与回滚**：17 个数据卷独立于镜像，更新镜像不动数据；回滚=把 `.env` 的镜像 tag 改回上一周版重建 devbox。缓存膨胀时 `podman system prune`（不会碰 external 卷）。

- **回滚不删卷**：JDK 状态卷（已安装的 8 + `current` 选择）留在原处；旧镜像不认识 `jdk` 命令、只继续用内置 21，不会破坏卷内容——再升回新镜像时选择仍在。
- 内置 21 的目录若随镜像升级改名，entrypoint 会识别并**迁移指向内置 21 的选择**；额外安装的 8 不受影响。
- 卸载该特性：`podman volume rm cadence-jdks`（会一并删掉已安装的 8；容器正在用时先 `podman rm -f cadence_devbox`）。

## 7. FAQ

1. **企业敏感场景关闭运维通道**：devbox 启动命令去掉 `-v /var/run/podman/podman.sock:/var/run/docker.sock`（或参考 `stack/docker-compose.no-sock.yml`）——agent 仍能连中间件端口开发，但不能启停容器
2. **新仓库避免换行符幻影 diff**：仓库根加 `.gitattributes`：`* text=auto eol=lf`
3. **宿主与容器同时跑 git 偶发 index.lock**：瞬时锁，重试即可；agent 提交时避免宿主同时操作
4. **dev server 收不到宿主侧文件改动**：镜像已默认 `CHOKIDAR_USEPOLLING=1`（vite）；spring-boot-devtools 需在配置中开启轮询：`spring.devtools.restart.poll-interval=1s` + `quiet-period=0.8s`
5. **Windows 上 `podman pull` 报 `…registries.conf.d\999-…conf: The file cannot be accessed by the system`**：Podman Desktop 生成的该文件系统层不可读，会卡死一切镜像拉取——install.ps1 第 3 步已自动改名 `.unreadable.bak` 绕过；手工安装则手动删除该文件
6. **podman-compose 报 `unknown mount option /workspace`**：短语法挂载按 `:` 切分，Windows 盘符路径（`D:/code:/workspace`）被切成三段——compose.yaml 的 workspace 挂载已用长语法 `type: bind` 规避，旧安装目录请删除 `stack\compose.yaml` 重跑 install.ps1 重新生成
7. **容器内没有 `jdk` 命令**：镜像是旧版（不含 JDK 选择特性），换新版镜像并按 §6 补建 `cadence-jdks` 卷
8. **`jdk use 8` 后重建容器又变回 21**：启动命令漏了 `-v cadence-jdks:/home/dev/.cadence/jdks`（§2.4）——状态只存在该卷里，没挂卷就只是容器内临时状态
9. **关掉运维通道（no-sock）后 JDK 还能用吗**：能。sock 与 JDK 无关；`stack/docker-compose.no-sock.yml` 已保留 `cadence-jdks` 挂载，`jdk list`/`jdk use` 照常工作（该文件用 compose 的 `!override` 语法，需 compose 实现支持该标签；本机 `podman-compose` 版本解析该标签会报 `RepresenterError`，故未在本机实跑该文件，其挂载集合已按等价 `podman run` 逐个挂载实测通过）
10. **`jdk` 相关报错怎么查**：见 §3.1.4 报错速查表（不支持版本 / 来源与哈希未成对 / 下载失败 / 哈希不符 / 状态损坏 / 卷未挂载 / 卷不可写 / 兜底入口拒绝回退）
11. **容器日志报 `Permission denied`、启动即退出（Windows/podman）**：老环境的命名卷首挂时挂载点未被镜像预建，卷根成了 root 属主，容器内 dev(1000) 写不进。新镜像已在构建时预建全部挂载点（属 dev）；**已存在的坏卷不会被新镜像修复**，一次性修复命令（PowerShell，重复执行无害）：
    ```powershell
    "cadence-claude","cadence-codex","cadence-pi","cadence-agents","cadence-omp","cadence-npm","cadence-uv","cadence-pip" | ForEach-Object { podman run --rm -v "${_}:/d" docker.io/library/alpine:latest chown -R 1000:1000 /d }
    ```
    执行前先 `podman compose stop devbox`；修完 `podman compose up -d` 即可
12. **改了 `stack\compose.yaml`（如固定容器名），重启容器后改动消失**：entrypoint 每次启动会用镜像内置模板同步 `stack/`——**分叉的文件会被备份后覆盖**。要长期保留本地版，在 `stack\` 下建 `CHANGES.md`（内容写清改了什么），entrypoint 检测到它即整段跳过同步：
    ```powershell
    @'
    # 本地改动记录
    - compose.yaml：devbox 服务固定容器名 cadence_devbox
    '@ | Set-Content -Encoding utf8 D:\testpodman\cadence\stack\CHANGES.md
    ```
13. **`jdk list` 报「状态目录不可写」/ `jdk use 8` 失败**：老环境里 `cadence-jdks` 卷的属主可能是 `root:root`（早期镜像初始化所致），容器内 dev 写不进。一次性修复（执行前先停容器，修完再起）：
    ```powershell
    podman stop cadence_devbox
    podman run --rm -v cadence-jdks:/d docker.io/library/alpine:latest chown -R 1000:1000 /d
    podman start cadence_devbox
    ```
    也可以用运行中的容器直接修：`podman exec -u root cadence_devbox chown -R dev:dev /home/dev/.cadence` 然后 `podman stop cadence_devbox; podman start cadence_devbox`
14. **容器启动日志报 `skills 安装/更新失败`（`set: pipefail: invalid option name`）**：Cadence-skills 的 `install.sh` 第 2 行是 `set -euo pipefail`，报错说明那次执行的 shell 不认 `pipefail`（根因未定位；`POSIXLY_CORRECT`、`bash --posix`、`dash` 三种假设都已试过、均不复现）。容器与 agent 照常可用，只是 skills 没自动更新。手动补一次：
    ```powershell
    podman exec -it cadence_devbox bash -lc 'cd ~/.agents/Cadence-skills && git pull --ff-only && bash install.sh'
    ```
    新版 entrypoint 已加「首跑失败 → 换 `/bin/bash` 重试 → 仍失败则打印 shell 路径/版本与脚本头 3 行」的诊断，随下一个镜像生效

## 8. Windows 真机验收（已执行：2026-09-21，维护者 michaelChe）

### 8.1 执行指引

**阶段 A（Arch 维护机）**：

```bash
cd /home/michaelche/workspace/github/Cadence-devbox
podman save cadence-devbox:dev | gzip > /tmp/devbox-image.tar.gz
tar czf /tmp/devbox-files.tar.gz devbox/
```

两个文件传到 Windows 目标机。

**阶段 B（Windows 装机）**：按 §2.1（Podman Desktop）–2.4 执行并全程录屏（= 验收 1 留证）。重点确认：machine 内宿主路径映射、sock 挂载、bind 写权限三个 Windows 特有点。

**阶段 C（容器内验收操作）**：

1. 验收 3 计时：`time mvn -B archetype:generate -DgroupId=t -DartifactId=d -DarchetypeArtifactId=maven-archetype-quickstart`；`time npm create vite@latest dv -- --template vue`；`time uv init dp`（依赖预热后各 <3 分钟）
2. 验收 4：宿主 `git init` 仓库提交数文件 → 容器内 `git status` 应无「全文件被修改」；起 vite 后宿主改 `App.vue`，容器内 dev server 5 秒热更
3. 验收 6：容器内对 agent 说「创建最小 Spring Boot 3 应用：/api/items 增查走 MySQL、/api/hits 走 Redis 计数，表自动建并跑起来；再建 vue3+vite 前端代理 /api 到 8080，3000 端口起 dev server」→ Windows 浏览器开 `localhost:3000` 完成增删查 + 文件上传走 minio；Navicat 连 `127.0.0.1:3306` 核对数据

**阶段 D**：截图/录屏按 7.2 清单归档；macOS 真机同法（§2.1 macOS 路线）。

### 8.2 验收清单（设计文档「七、验证口径」）

- [x] 1. 装机到进终端——**通过**（维护者真机确认）
- [x] 2. 改 `cadence-box.yaml` 换 key/模型 → 重启容器 → 五端按新配置工作；容器销毁重建配置仍生效——**通过**（维护者真机确认）
- [x] 3. 容器内 mvn/npm create/uv init 各 <3 分钟——**通过**（维护者真机确认）
- [x] 4. 宿主改文件 5 秒热更 + 无幻影 diff——**通过**（维护者真机确认）
- [x] 5. skills 首启投影 + 体积 ≤1GB——**通过**（镜像压缩体积 872MB）
- [x] 6. 中间件全链路（含 minio 上传）——**通过**（维护者真机确认）

> **留证情况**：本次真机的机型、Podman/镜像版本、网络环境、各项实测数值与录屏/截图**未留存**（维护者确认结果通过，细节数据未归档）。
> 因此本清单只记录结论，不记录数值；后续需要数值证据时按 §8.1 重新执行并留证。
> 第 3 项的时间门与第 4 项的 5 秒热更为设计「七、验证口径」的验收门槛，本次按维护者确认为通过。
>
> **范围说明**：上表是**一期**验收（2026-09-21），当时镜像尚无 §3.1 的 JDK 选择特性；该特性在 Windows 上**尚未真机验证**——`install.ps1` 的 `cadence-jdks` 预建与 `podman run -v` 挂载只做过静态核对，Windows 端口只到「静态断言通过」，不构成真机 E2E 结论。

### 8.3 变更验收：devbox 容器唯一化（2026-10-28，Windows 真机）

变更加归档在 OpenSpec `2026-10-28-unify-devbox-container-and-vpn-services`：容器只经 compose 创建、名字固定 `cadence_devbox`、relay 探测按运行态、地址问句给出取值指引、已录域名做解析核对。

真机环境：Windows + Podman Desktop 6.0.2（WSL 后端）+ ACR `latest` 镜像。

**已验证（真机）**

- [x] `podman-compose 6.0.2` **支持** `container_name`：`podman ps` 显示容器名 `cadence_devbox`，安装器不再输出「未按固定名创建容器」警告
- [x] `install.ps1 -InstallDir <既有安装目录>` 生效：`生成安装目录` 指向正确路径，`.env` 被重新生成（三行齐全）
- [x] `stack\CHANGES.md` 存在时 entrypoint 跳过模板同步（日志：`检测到 CHANGES.md：保留当前 /cadence/stack`），本地 `container_name` 不被镜像内置模板覆盖
- [x] 17 个命名卷全部保留；agent 登录态（`.claude` / `.codex` / `.omp`）与 `cadence-jdks` 卷未丢
- [x] relay 通道恢复后，容器内 `git fetch origin --prune` 至 GitLab（`198.18.0.2:80` 经 VM 侧 DNAT）成功

**本次暴露并已修的问题**

| 问题 | 处置 |
|---|---|
| 老 `.env` 缺 `CADENCE_DEVBOX_IMAGE` → compose 回落最早版镜像 → `mkdir /home/dev/.cadence/logs: Permission denied`，容器 Exit 1 | install.ps1 新增 `Resolve-EnvImage`：缺键自动补写（并加测试） |
| `install.ps1` 无 `-InstallDir`，已有安装目录在别处时静默装出第二套 | 新增 `-InstallDir` 参数（与 relay.ps1 对齐） |
| `cadence-jdks` 卷属主 `root:root` → `jdk` 报「状态目录不可写」 | `chown -R dev:dev` 一次性修复（FAQ 13） |
| 改了 `stack\compose.yaml` 会被镜像内置模板覆盖 | 需在 `stack\` 建 `CHANGES.md`（FAQ 12）；下个镜像构建会把新模板打进去，届时可不再需要 |

**未纳入本次**：镜像内 skills 安装脚本报 `set: pipefail: invalid option name`（既有问题，软失败不阻断容器启动）。

## 9. 镜像构建信息

- 构建上下文：`devbox/`；版本 pin 唯一来源 `devbox/versions.env`（升级只改它 + Dockerfile 默认值）
- 版本清单：容器内 `cat /opt/cadence/versions.txt`
- 源：apt/pypi=aliyun，maven=aliyun（原 USTC 因容器 TLS 指纹被 CF 误杀），node=npmmirror，npm=registry.npmmirror.com，omp/bun=npmmirror（官方安装器走 GitHub 不可达）
- omp 本地语音/视觉依赖（onnxruntime/sherpa-onnx，568MB）已按需裁剪；需要时容器内 `npm i -g` 补装

### 9.1 构建与发布（维护者手动，无自动周构建）

镜像**不在 CI 里自动构建**：`.github/workflows/devbox-ci.yml` 只跑 pytest + shellcheck。发布有两条路：

**A. 一键发布（推荐）：GitHub Actions 手动触发**

`Actions → devbox image publish → Run workflow`，填 tag（如 `2026.44`）。首次使用需先在仓库
`Settings → Secrets and variables → Actions` 配三个 secret：

| secret | 内容 |
|---|---|
| `ACR_IMAGE` | 完整镜像名（不含 tag），如 `<registry>/cadence/devbox` |
| `ACR_USERNAME` | ACR 用户名 |
| `ACR_PASSWORD` | ACR 密码 / 访问令牌 |

工作流会构建、验证两个关键路径（`/home/dev/.cadence` 存在、`/opt/cadence/stack/compose.yaml` 存在）、推送，并在 Summary 里给出升级命令。

**B. 本地构建并推送**（需要 ACR 写权限；构建在 podman machine 的 Linux VM 内执行，不是 Windows 原生）

仓库自带一键脚本 `devbox/publish-image.ps1`——构建 + 三项校验（`.cadence` 预建 / 模板含 `container_name` / entrypoint 语法）+ 可选推送：

```powershell
# 只构建并校验（不推）
powershell -ExecutionPolicy Bypass -File .\devbox\publish-image.ps1 -Tag 2026.44

# 构建 + 校验 + 推送，并把 latest 一起更新
powershell -ExecutionPolicy Bypass -File .\devbox\publish-image.ps1 -Tag 2026.44 -Push -AlsoLatest
```

手工等价命令（脚本内部就是这么做的，注意先去掉首行 `# syntax=`）：

```bash
podman build -f devbox/Dockerfile -t <registry>/cadence/devbox:2026.44 devbox
podman push <registry>/cadence/devbox:2026.44
```

> ⚠️ **zsh 用户注意**：`podman push "$IMAGE:latest"` 在 zsh 里会被解析成 `$IMAGE:l`（转小写修饰符）+ 字面量 `atest`，实际推到 `.../devboxatest`——会在 ACR 里凭空多建一个仓库。必须写成 `${IMAGE}:latest`。`$IMAGE:2026.44` 侥幸没事（`2` 不是合法修饰符），所以这坑只在 `:latest` 这类字母开头的 tag 上出现。bash 无此问题。

**为什么必须重新构建镜像才能撤掉 `CHANGES.md`**：镜像里内置了构建时的 `stack/` 模板
（`compose.yaml` / `docker-compose.no-sock.yml` / `catalog`），entrypoint 每次启动会把它们同步
到安装目录。改了模板文件（例如给 `compose.yaml` 加 `container_name`）后，只有新镜像才带新模板；
在那之前，安装目录用 `stack\CHANGES.md` 挡住同步（见 FAQ 12）。
