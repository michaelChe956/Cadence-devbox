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

```powershell
@"
CADENCE_WORKSPACE=D:/code
CADENCE_DEVBOX_IMAGE=crpi-qzp491l6hpbyhd49.cn-hangzhou.personal.cr.aliyuncs.com/cadence/devbox:latest
COMPOSE_PROFILES=
"@ | Set-Content -Encoding utf8 C:\cadence\stack\.env
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

> ⚠️ 此文件=密钥，勿提交勿分享。换 key/换模型改这里，`podman restart devbox` 生效。

### 2.4 启动与首验（podman 拓扑）

**中间件走 podman-compose，devbox 单独 `podman run`**（实测拓扑；Windows/macOS 的 machine 默认 rootful，路径由 podman 自动映射进 machine）：

```powershell
cd C:\cadence\stack
podman-compose -f compose.yaml up -d mysql redis     # 中间件（按 .env 的 PROFILES 增减）

podman run -d --name devbox --network cadence_default `
  -v C:\cadence\cadence-box.yaml:/cadence/auth.yaml:ro `
  -v C:\cadence\stack:/cadence/stack `
  -v D:\code:/workspace `
  -v cadence-jdks:/home/dev/.cadence/jdks `
  -v /var/run/podman/podman.sock:/var/run/docker.sock `
  -p 127.0.0.1:3000:3000 -p 127.0.0.1:8080:8080 `
  ghcr.io/michaelche956/cadence-devbox:latest

podman logs devbox | Select-Object -Last 3     # 应见「就绪」
podman exec -it devbox bash                    # 进入容器
```

- `-v cadence-jdks:/home/dev/.cadence/jdks` 是 **JDK 选择的状态卷**（§3.1）：不挂它就只是容器内临时状态，`podman rm` 重建后回到内置 21。卷需先按 §2.2 第 4 步预建。
- 挂载取舍：`podman run` 路线只挂**必需项**（鉴权文件、stack、workspace、JDK 状态卷）；`compose.yaml` 的 devbox 服务还会把另外 16 个缓存/登录态卷（`.claude`/`.codex`/`.m2`/…）一起挂上，让登录态与依赖缓存在容器重建后仍在。两条路线都能跑——五端配置每次启动都会按 `cadence-box.yaml` 重新渲染。

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

Linux 下 devbox 启动命令在 §2.4 基础上**必须加 `--userns=keep-id`**，sock 挂载源用 `$XDG_RUNTIME_DIR/podman/podman.sock`。

**平台差异与验证状态**：

| 平台 | 状态 | 差异 |
|---|---|---|
| Linux rootless podman | ✅ 已实测（一期验收+E2E） | `--userns=keep-id` 必需；cgroupfs 配置；rabbitmq 命名卷权限问题（mysql/redis/minio 正常） |
| Windows Podman Desktop | ✅ 已验收（2026-09-21，见 §8） | machine 默认 rootful，免 keep-id/cgroup 配置；宿主路径自动映射进 machine |
| macOS podman machine | ⏳ 待真机验证 | 同上（Apple 虚拟化） |

stack 命令（status/restart/logs）已按 label 直连 docker 兼容 API 适配 podman 双形态。

## 3. 日常使用

| 场景 | 操作 |
|---|---|
| 每天开工 | `podman-compose -f compose.yaml up -d`（在 stack 目录；二次启动秒级）+ `podman start devbox` |
| 进入容器 | `podman exec -it devbox bash`，然后直接 `claude` / `codex` / `pi` / `kimi` / `omp` |
| 换 key/换模型 | 改 `..\cadence-box.yaml` → `podman restart devbox` |
| 中间件增删启停 | 容器内 `stack` 命令（见 §4），或直接对 agent 说「加个 kafka」「重启 mysql」 |
| 跑项目 | 容器内 `app run myapp -- mvn spring-boot:run`（后台+日志+端口探活），或让 agent 全权 |
| 切 JDK 版本 | 容器内 `jdk list` / `jdk use 8` / `jdk use 21`（见 §3.1；默认内置 Temurin 21，选择跨重建保留） |
| 宿主工具连库 | Navicat 连 `127.0.0.1:3306`（root/cadence123）；浏览器开 `localhost:3000/8080` |
| 下班 | `podman stop devbox` + `podman-compose -f compose.yaml stop`——凭据/依赖缓存/中间件数据全在卷里，不丢 |

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
| 命令一致性 | `java`/`javac`/`mvn` 等一律读固定的 `JAVA_HOME=/home/dev/.cadence/jdks/current`，`current/bin` 在 `PATH` 前部：**非交互** `podman exec devbox java -version`（不读 `.bashrc`）、交互式 shell、容器内 agent 拿到的都是同一个版本 |
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

**验证状态**：`jdk list`、`jdk use 8`（真实下载+校验）、`jdk use 21`、跨重建沿用、失败安全（下载/哈希/不支持版本）、非交互与交互命令一致性、no-sock 挂载拓扑，均已在 **Linux + rootless podman** 实机跑通（2026-09-23，本地构建镜像）。**未验证**：Windows Podman Desktop 上的同一流程（安装器与卷挂载只做了静态核对，见 §8）、Gradle（镜像不含 `gradle` CLI，项目自带 `gradlew` 的 JVM 由其读 `JAVA_HOME` 决定）、arm64。

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
podman rm -f devbox && # 按启动命令重建（建议存成 start-devbox 脚本）
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
- 卸载该特性：`podman volume rm cadence-jdks`（会一并删掉已安装的 8；容器正在用时先 `podman rm -f devbox`）。

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

## 9. 镜像构建信息

- 构建上下文：`devbox/`；版本 pin 唯一来源 `devbox/versions.env`（升级只改它 + Dockerfile 默认值）
- 版本清单：容器内 `cat /opt/cadence/versions.txt`
- 源：apt/pypi=aliyun，maven=aliyun（原 USTC 因容器 TLS 指纹被 CF 误杀），node=npmmirror，npm=registry.npmmirror.com，omp/bun=npmmirror（官方安装器走 GitHub 不可达）
- omp 本地语音/视觉依赖（onnxruntime/sherpa-onnx，568MB）已按需裁剪；需要时容器内 `npm i -g` 补装
