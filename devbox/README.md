# cadence devbox —— 一体化 AI 开发环境容器 使用手册

> 版本：第一期（2026-09-11）｜ 运行时：**podman**（Windows/macOS/Linux 三平台统一）｜ 镜像压缩体积 872MB ｜ 内置：claude 2.1.247 / codex 0.153.4 / pi 0.85.0 / omp 18.1.17（bun 1.4.2）/ kimi（官方安装器）+ JDK21 + Maven 3.9.16 + Node 24.21.0 + uv 0.12.11 + mysql/redis/rabbitmq/minio 编排
> 完整设计见 `cadence/designs/2026-09-11_方案设计_Cadence-skills一体化开发环境容器devbox_v1.0.md`
> 说明：工具链对接的是 docker 兼容协议（sock/命令语义），全程使用 podman 运行；Linux 路线已实测，Windows/macOS 路线待 §8 真机验收。

## 0. 这是什么、为什么（背景与目的）

devbox 是给**业务开发/测试人员**的一体化 AI 开发环境容器：预装 claude/codex/pi/kimi/omp 五端 CLI 与 Java/前端/Python 工具链，编排 mysql/redis/rabbitmq/minio，鉴权只需维护一份 `cadence-box.yaml`。你装一次 Podman Desktop，剩下的一切——构建、运行、中间件、AI agent——都在容器里；代码始终留在宿主你自己的磁盘上（双向共享）。背景与完整动机见[仓库 README](../README.md#背景与目的)。

## 1. 镜像获取（三选一）

**路线 A：GHCR 公开仓库（推荐，已可用）**

```bash
podman pull ghcr.io/michaelche956/cadence-devbox:latest
```

直连慢/超时改用 ghcr 代理前缀拉取，再 retag 成正式名（install.ps1 已内置此回退）：

```bash
podman pull ghfast.top/ghcr.io/michaelche956/cadence-devbox:latest
podman tag ghfast.top/ghcr.io/michaelche956/cadence-devbox:latest ghcr.io/michaelche956/cadence-devbox:latest
```

tag 策略：`latest`（滚动）/ `1.0.0`（当前里程碑）/ `YYYY.WW`（周版，二期 CI 起提供）。已验证匿名可拉；公共加速镜像（daocloud/南大）不覆盖 GHCR 个人包——国内直连慢用上面代理前缀或路线 C。

**路线 B：国内 ACR（待开通，开通后此处回填地址）**

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

参数：`-Workspace`（代码父目录，不传则交互询问）、`-InstallDir`（默认 devbox 旁 `cadence\`）、`-Image`（默认 GHCR latest）。

脚本自动做的 5 步：

| 步 | 内容 | 对应手工段落 |
|---|---|---|
| 1 | 检测 podman；machine 未建自动 `init`、未运行自动 `start`；检测 podman-compose | §2.1 |
| 2 | 建安装目录，拷 compose/no-sock/catalog，生成 cadence-box.yaml 密钥模板 + .env + .gitignore，**中途暂停等你填密钥** | §2.2 下文/§2.3 |
| 3 | 配置国内镜像加速：daocloud mirror 写入 podman machine（中间件镜像走 docker.io） | §2.2.1 |
| 4 | 建 16 个数据卷；拉主镜像（直连失败自动依次试 ghfast.top / gh-proxy.com / mirror.ghproxy.com 三个 ghcr 代理并 retag）；起 mysql/redis；起 devbox 并等待「就绪」日志 | §2.2 第 4 步/§2.4 |
| 5 | 打印后续使用提示（进容器/换 key/开中间件/日常开关机） | §3 |

**重跑安全**：已存在的 cadence-box.yaml、stack\compose.yaml（含 `stack add` 自加的服务）、catalog、.env 一律保留不覆盖；16 个卷的数据零影响；仅重建 devbox 容器。中途失败从头重跑即可（幂等）。

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
CADENCE_WORKSPACE=D:\code
CADENCE_DEVBOX_IMAGE=ghcr.io/michaelche956/cadence-devbox:latest
COMPOSE_PROFILES=
"@ | Set-Content -Encoding utf8 C:\cadence\stack\.env
```

- `CADENCE_WORKSPACE`：你的代码父目录（几十个 git 仓库的上一级），将整体挂载为容器内 `/workspace`
- `CADENCE_DEVBOX_IMAGE`：默认填 GHCR 地址；离线路线改 `localhost/cadence-devbox:dev`

**第 4 步：创建数据卷（external 卷需预建，一次性）**

compose.yaml 里 16 个卷全部声明 `external: true`——podman-compose 只挂载、不创建，卷不存在则启动直接报错；预建后数据与容器生命周期解耦（`down -v`、`system prune` 都删不掉，重建容器/升级镜像数据全在）。三步：

```powershell
podman machine start     # ① 确保 machine 在跑（已启动会提示 already running，继续即可）
# ② 整行复制粘贴——16 个卷逐个创建约 1 秒；重复执行无害（已存在的报错跳过）
"cadence-claude","cadence-codex","cadence-pi","cadence-kimi","cadence-agents","cadence-omp","cadence-m2","cadence-npm","cadence-npm-global","cadence-uv","cadence-pip","cadence-gradle","cadence-mysql-data","cadence-redis-data","cadence-rabbitmq-data","cadence-minio-data" | ForEach-Object { podman volume create $_ }
podman volume ls         # ③ 验证：列出 16 行 cadence- 开头的卷
```

macOS/Linux 等价命令（bash 循环）：

```bash
for v in cadence-claude cadence-codex cadence-pi cadence-kimi cadence-agents cadence-omp cadence-m2 cadence-npm cadence-npm-global cadence-uv cadence-pip cadence-gradle cadence-mysql-data cadence-redis-data cadence-rabbitmq-data cadence-minio-data; do podman volume create "$v"; done
podman volume ls | wc -l   # 应输出 16
```

### 2.2.1 国内镜像加速（手工安装直连慢/超时才配；一键脚本第 3 步已自动完成）

中间件镜像（mysql/redis/rabbitmq/minio）都在 docker.io，直连拉不动时配 daocloud 公共加速。跑 §2.2 一键脚本的用户无需动手——脚本第 3 步自动写入下述同名文件（幂等），仅当脚本加速步失败时按本节补救：

**Windows / macOS**（podman remote 客户端读**本机**配置并随 pull 生效，不进 machine；二选一）：

- GUI（推荐）：Podman Desktop → Settings → **Registries** → Add registry，填 `docker.m.daocloud.io`
- 手写 mirror 文件（对 docker.io 全量生效）。Windows（PowerShell）：

```powershell
$d = "$env:APPDATA\containers\registries.conf.d"; mkdir $d -Force | Out-Null
@('[[registry]]','prefix = "docker.io"','location = "docker.m.daocloud.io"','') | Set-Content "$d\999-mirror.conf"
```

  macOS（bash）：

```bash
mkdir -p ~/.config/containers/registries.conf.d
printf '[[registry]]\nprefix = "docker.io"\nlocation = "docker.m.daocloud.io"\n' > ~/.config/containers/registries.conf.d/999-mirror.conf
```

**Linux**：同 macOS 写法（`~/.config/containers/registries.conf.d/`）。§2.5 三件套里的 `unqualified-search-registries` 是等效的另一写法，配过其一即可。

> ⚠️ 别用 `podman machine ssh` 往 machine 的 `/etc/containers/` 写——remote 场景 pull 读的是客户端本机配置，写 machine 里不生效。

> devbox 主镜像在 ghcr.io：上面的 daocloud mirror 只管 docker.io，对 ghcr.io 无效——主镜像拉不动用 §1 的 ghcr 代理前缀（ghfast.top 等）或路线 C 离线包。

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
  -v /var/run/podman/podman.sock:/var/run/docker.sock `
  -p 127.0.0.1:3000:3000 -p 127.0.0.1:8080:8080 `
  ghcr.io/michaelche956/cadence-devbox:latest

podman logs devbox | Select-Object -Last 3     # 应见「就绪」
podman exec -it devbox bash                    # 进入容器
```

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
| Windows Podman Desktop | ⏳ 待 §8 真机验证 | machine 默认 rootful，理论免 keep-id/cgroup 配置；宿主路径自动映射进 machine |
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
| 宿主工具连库 | Navicat 连 `127.0.0.1:3306`（root/cadence123）；浏览器开 `localhost:3000/8080` |
| 下班 | `podman stop devbox` + `podman-compose -f compose.yaml stop`——凭据/依赖缓存/中间件数据全在卷里，不丢 |

代码工作方式：宿主 IDE 照常编辑 `D:\code`（或 `~/code`）下的仓库，容器内 agent/构建实时看到同一工作树（bind 双向共享）；push 在宿主 git 客户端完成。

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
podman pull ghcr.io/michaelche956/cadence-devbox:2026.38
podman rm -f devbox && # 按启动命令重建（建议存成 start-devbox 脚本）
# 离线路线：podman load -i 新包，改 .env 指向新 tag，重建 devbox
```

**skills 更新（自动）**：每次容器启动 entrypoint 自动执行 `install.sh update`（幂等；失败仅告警不阻断，下次启动重试）。

**数据与回滚**：16 个数据卷独立于镜像，更新镜像不动数据；回滚=把 `.env` 的镜像 tag 改回上一周版重建 devbox。缓存膨胀时 `podman system prune`（不会碰 external 卷）。

## 7. FAQ

1. **企业敏感场景关闭运维通道**：devbox 启动命令去掉 `-v /var/run/podman/podman.sock:/var/run/docker.sock`（或参考 `stack/docker-compose.no-sock.yml`）——agent 仍能连中间件端口开发，但不能启停容器
2. **新仓库避免换行符幻影 diff**：仓库根加 `.gitattributes`：`* text=auto eol=lf`
3. **宿主与容器同时跑 git 偶发 index.lock**：瞬时锁，重试即可；agent 提交时避免宿主同时操作
4. **dev server 收不到宿主侧文件改动**：镜像已默认 `CHOKIDAR_USEPOLLING=1`（vite）；spring-boot-devtools 需在配置中开启轮询：`spring.devtools.restart.poll-interval=1s` + `quiet-period=0.8s`

## 8. Windows 真机验收（待执行——第一期交付后由维护者在真机完成）

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

- [ ] 1.【真机人工】装机到进终端（留证：机型、Podman/镜像版本、网络环境、命令、录屏）——**Linux 侧已预覆盖安装布局逻辑**
- [ ] 2. 改 `cadence-box.yaml` 换 key/模型 → 重启容器 → 五端按新配置工作；容器销毁重建配置仍生效——**已预覆盖**（渲染 6 文件 + exit 2 拒启，46 项测试；真机补真实 key 验证）
- [ ] 3.【真机实测，设门】容器内 mvn/npm create/uv init 各 <3 分钟——**参考计时已取**（mvn 15s/spring-boot 依赖 20s/vite 0.7s/uv 0.1s）
- [ ] 4.【真机人工】宿主改文件 5 秒热更 + 无幻影 diff（留证：改文件时间、热更与 git status/diff 截图）
- [ ] 5. skills 首启投影 + 体积 ≤1GB——**已达标**（872MB；devbox-stack skill 待仓库 merge 后随 install.sh 投影）
- [ ] 6.【真机】中间件全链路（含 minio 上传）——**Linux 容器侧已预覆盖**（Spring+Vue+MySQL+Redis 全链路实测，2 个连接串模板 bug 已修复）

## 9. 镜像构建信息

- 构建上下文：`devbox/`；版本 pin 唯一来源 `devbox/versions.env`（升级只改它 + Dockerfile 默认值）
- 版本清单：容器内 `cat /opt/cadence/versions.txt`
- 源：apt/pypi=aliyun，maven=aliyun（原 USTC 因容器 TLS 指纹被 CF 误杀），node=npmmirror，npm=registry.npmmirror.com，omp/bun=npmmirror（官方安装器走 GitHub 不可达）
- omp 本地语音/视觉依赖（onnxruntime/sherpa-onnx，568MB）已按需裁剪；需要时容器内 `npm i -g` 补装
