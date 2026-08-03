# Docker 多项目基础设施统一配置指南

> [!WARNING]
> **这份文档描述的是作者那台宿主机的跨项目基础设施，不是本项目的配置方式。**
> 本项目自 2026-08 去宿主机化之后，**配置的唯一入口是仓库根目录的 `.env`**（照 `.env.example` 抄），
> 不需要、也不应该按本文去建 `/opt/docker_shared/api_keys.env`。两处具体冲突：
>
> 1. 本文说"**不建项目级 `.env`**"——本项目相反，`.env` 是必需的，`compose` 的 `env_file` 直接读它；
>    `/opt/docker_shared/api_keys.env` 降级成可选的 `${SHARED_ENV_FILE}`（`required: false`），
>    只是作者机器上集中管 Key 的便利，不是前提。
> 2. 本文第二节让把 `ALL_PROXY=socks5://...` 写进共享文件——**本项目的容器里绝不能有它**。
>    镜像没装 `httpx[socks]`，继承到 socks 代理会让 openai 客户端初始化就崩，
>    所以 `docker-compose.yml` 里 `ALL_PROXY` 被强制置空。代理请用 `LIT_HTTP_PROXY`/`LIT_HTTPS_PROXY`。
>
> 详见 `handoff.md` §5「环境配置」。以下内容仅对作者本机的其他项目有效。

适用于作者宿主机上所有需要调用大模型 API 和外网代理的 Docker 项目。**一次配置，全机生效。**

---

## 一、集中管理 API Key（免 `.env` 文件）

### 原理

所有项目的 `docker-compose.yml` 通过 `env_file` 指向服务器上**同一个全局配置文件**。
新增/更换 Key 只需改这一个文件，重启相关容器即可，不需要碰任何项目目录。

### 服务器端：创建全局 Key 文件（只做一次）

```bash
sudo mkdir -p /opt/docker_shared
sudo nano /opt/docker_shared/api_keys.env
sudo chmod 600 /opt/docker_shared/api_keys.env
```

文件内容（按需增减）：

```ini
# ── 大模型 API Keys ──
DEEPSEEK_API_KEY=sk-xxxxx
GEMINI_API_KEY=AIzaSyxxxxx
MINERU_API_KEY=eyJ0eXBlxxxxx
# 以后新增的 Key 也放这里，比如：
# OPENAI_API_KEY=sk-xxxxx
# ANTHROPIC_API_KEY=sk-ant-xxxxx

# ── 网络代理（见下方第二节）──
HTTP_PROXY=http://172.17.0.1:20171
HTTPS_PROXY=http://172.17.0.1:20171
ALL_PROXY=socks5://172.17.0.1:20170
```

### 项目端：每个 `docker-compose.yml` 只需加这一段

```yaml
services:
  your-service:
    build: .
    restart: always
    # ↓↓↓ 这一行就够了，指向全局文件，项目目录下不需要 .env ↓↓↓
    env_file:
      - /opt/docker_shared/api_keys.env
    environment:
      # 项目自己的专属配置直接写在这里
      - TZ=Asia/Shanghai
      - YOUR_PROJECT_SPECIFIC_VAR=value
```

> [!IMPORTANT]
> **不要**在项目目录下放 `.env` 文件写 API Key 了。
> 如果项目代码里有 `load_dotenv()`，在容器内没有 `.env` 文件时它会静默跳过，不影响运行（环境变量已由 Docker 注入）。

---

## 二、Docker 容器访问宿主机 v2rayA 代理

### 原理

v2rayA 跑在宿主机上，Docker 容器通过 Docker bridge 网关 IP（`172.17.0.1`）访问宿主机服务。
需要将 v2rayA 监听地址从 `127.0.0.1` 改为 `0.0.0.0`，并用 UFW 阻止外网访问代理端口。

### 第 1 步：修改 v2rayA 监听地址

打开 v2rayA Web 管理界面 → 设置 → 将入站监听地址改为 `0.0.0.0`。

当前端口表：

| 端口 | 协议 | 改前监听 | 改后监听 |
|------|------|---------|---------|
| 20170 | SOCKS5 | `127.0.0.1` | `0.0.0.0` |
| 20171 | HTTP | `127.0.0.1` | `0.0.0.0` |
| 20172 | HTTP | `127.0.0.1` | `0.0.0.0` |

### 第 2 步：UFW 防火墙锁死代理端口

```bash
# 允许 Docker 容器网段访问（必须在 deny 之前）
sudo ufw allow from 172.17.0.0/16 to any port 20170:20172 proto tcp

# 拒绝所有其他来源（外网）
sudo ufw deny 20170:20172/tcp
```

验证规则顺序：

```bash
sudo ufw status numbered
```

确保 `allow 172.17.0.0/16` 的编号在 `deny` 之前。

### 效果验证

```bash
# 从宿主机测试（应该成功）
curl -x http://127.0.0.1:20171 https://www.google.com

# 从容器内测试（应该成功）
docker run --rm curlimages/curl -x http://172.17.0.1:20171 https://www.google.com

# 从外网测试（应该被拒绝）
# 在另一台机器上：curl -x http://你的服务器IP:20171 https://www.google.com → 连接拒绝 ✅
```

---

## 速查：新项目部署 Checklist

- [ ] `docker-compose.yml` 加 `env_file: - /opt/docker_shared/api_keys.env`
- [ ] 项目专属配置写在 `environment:` 里，**不建项目级 `.env`**
- [ ] 代码中用 `os.getenv("DEEPSEEK_API_KEY")` 读取，无需改动
- [ ] 如需新 Key，只编辑 `/opt/docker_shared/api_keys.env` 一处
