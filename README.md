# sshport

一次性反向 SSH 隧道管理网站。服务器拥有者在网页上申请隧道，服务从宿主机端口范围中分配一个端口，生成 `ssh -R` 命令和一段给本地 Agent 的提示词；远程电脑执行命令后，宿主机 sshd 按监听策略监听该端口。隧道到期或断开后，服务清理残留的 sshd 隧道进程并回收端口。

网站只负责分配、生成、监控、回收和删除隧道，不建立、不维持 SSH 隧道，也不探测远程 SSH 服务是否可用。状态只反映宿主机端口是否正在监听。

## 功能概览

- 单用户访问密钥登录，登录后同时使用申请页和管理页。
- 申请时分配宿主机端口，双重检查内存记录和宿主机监听表。
- 生成包含 `-N -T`、`ExitOnForwardFailure=yes`、客户端保活参数的 `ssh -R` 命令。
- 服务器 `tunnel` 账号和远程电脑账号都使用密码认证；服务器密码由终端输入，远程密码由网站随机生成并展示。
- 申请结果生成“在远程电脑创建专用非 root 用户”的命令和随机密码。
- 生成可直接粘贴给 Agent 的提示词，包含连接信息、远程账号密码、操作边界和断线处理规则。
- 三种监听地址策略：仅回环、全部地址、指定网卡 IP。
- 申请页轮询隧道状态，页面不可见时暂停，失败时保留上次状态。
- 管理页查看全部记录、手动删除、配置监听策略。
- 后台自动回收过期或已断开的隧道，并清理宿主机残留 sshd 进程。
- 无数据库、无缓存、无消息队列；隧道记录保存在进程内存中，策略快照保存为单个 JSON 文件。
- 原生 HTML/CSS/JavaScript，无 CDN、无 emoji、无前端框架。

## 项目结构

```text
.
├── app/
│   ├── __init__.py          版本号
│   ├── __main__.py          python -m app 入口
│   ├── main.py              uvicorn factory 入口
│   ├── web.py               FastAPI 路由、模板、会话、限流、健康检查
│   ├── config.py            TOML 配置加载与严格校验
│   ├── models.py            领域模型、状态、策略、异常
│   ├── security.py          访问密钥校验、会话、CSRF、来源地址
│   ├── rate_limit.py        登录限流、滑动窗口接口限流
│   ├── host.py              宿主机端口表与网卡地址探测
│   ├── cleaner.py           宿主机 sshd 隧道进程清理
│   ├── store.py             监听策略快照文件
│   ├── service.py           端口分配、状态、回收、删除的核心业务
│   └── logging_setup.py     UTC stdout 日志
├── templates/
│   ├── base.html            页面骨架
│   ├── login.html           登录页
│   ├── apply.html           申请页与结果面板
│   ├── admin.html           管理页
│   ├── error.html           错误页
│   └── _macros.html         状态徽标、字段错误、复制反馈
├── static/
│   ├── styles.css           本地样式
│   └── app.js               复制、倒计时、轮询、策略切换、删除确认
├── docker/
│   ├── sshport-cleanup      以 root 运行的受限清理 helper
│   └── healthcheck.py       容器健康检查
├── tests/
│   ├── conftest.py          测试夹具与假端口/假清理器
│   ├── test_service.py      端口分配、状态、回收、策略测试
│   └── test_web.py          登录、页面、API、CSRF、安全头测试
├── config/
│   └── config.example.toml  配置示例（复制为 config.toml 使用）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt
├── pytest.ini
├── DEPLOYMENT.md            部署与运维文档
└── README.md
```

## 模块划分

| 模块 | 职责 |
| --- | --- |
| `app/config.py` | 读取单个 TOML 文件，拒绝缺失字段、未知字段和非法值；配置错误时启动失败并列出中文错误。 |
| `app/security.py` | 多访问密钥的常量时间比较、会话 cookie、CSRF 令牌、来源 IP 提取。 |
| `app/rate_limit.py` | 登录失败锁定；申请和状态接口的滑动窗口限流。 |
| `app/host.py` | 读取 `/proc/net/tcp`、`/proc/net/tcp6` 判断端口监听；用 `ip -o addr show` 读取宿主机网卡地址。 |
| `app/cleaner.py` | 优先通过受限 sudo helper 清理宿主机 sshd 隧道进程，后备 direct 模式；只结束监听指定端口且进程名在允许列表中的进程。 |
| `app/service.py` | 端口池、同源去重、命令和提示词生成、状态探测、自动回收、手动删除、策略快照。 |
| `app/store.py` | 原子写入监听策略 JSON 快照，不使用数据库。 |
| `app/web.py` | 登录、申请、管理页和 JSON API；认证、CSRF、限流、安全响应头、健康检查。 |
| `templates/`、`static/` | 服务端渲染页面和原生交互，不依赖外部资源。 |

请求流程：

```text
浏览器申请
  -> 认证会话 + CSRF + 申请限流
  -> TunnelService 分配端口（内存记录 ∪ /proc/net/tcp）
  -> 按当前监听策略生成 ssh -R 命令和 Agent 提示词
  -> 页面展示端口、命令、提示词、状态

远程电脑执行 ssh -R
  -> 宿主机 sshd 在 127.0.0.1 / 0.0.0.0 / 指定 IP 上监听分配端口
  -> 浏览器轮询 /api/tunnels/{id}/status
  -> 服务实时探测端口，返回 pending / established / expired / unknown

到期或断开
  -> 状态请求或后台回收任务标记过期
  -> 清理该端口上残留的允许进程（示例配置为 sshd / sshd-session）
  -> 清理成功后释放端口回端口池
```

## 本地运行

要求 Python 3.12，Linux 环境（端口探测依赖 `/proc/net/tcp`，网卡地址读取依赖 `ip`）。Windows/macOS 只适合运行单元测试，不适合作为生产宿主机。

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt

cp config/config.example.toml config/config.toml
# 编辑 config/config.toml，至少替换 access.keys、tunnel.server_host、
# tunnel.public_host、tunnel.port_start/port_end 和 storage.policy_state_file

# 另按部署文档创建 tunnel 用户并设置其 SSH 密码；密码不写入配置文件。
sudo passwd tunnel

SSHPORT_CONFIG="$PWD/config/config.toml" python -m app
```

访问 `http://127.0.0.1:8080/`，使用配置中的 `access.keys` 登录。修改配置后重启进程生效。

本地直接运行时，`cleanup.strategy` 可先用 `direct`；只有以 root 或具备足够权限运行时才能看到并结束其他用户的 sshd 进程。生产环境请使用 Docker 和非 root 服务进程，通过受限 helper 清理。

运行测试：

```bash
pytest
```

测试使用假端口探测器和假清理器，不访问真实宿主机端口，也不执行 `ss` 或 `sudo`。

## Docker 部署

```bash
cp config/config.example.toml config/config.toml
mkdir -p data logs keys

# 配置含访问密钥，限制为容器用户可读；数据目录允许容器用户写入。
sudo chown 10001:10001 config/config.toml data logs
sudo chmod 0600 config/config.toml

docker compose up -d --build
curl -fsS http://127.0.0.1:8080/healthz
```

容器使用 `network_mode: host` 和 `pid: host`，不映射隧道端口；隧道端口由宿主机 sshd 直接监听。完整原因、宿主机 sshd 配置、密码认证、受限账号、权限和风险见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 访问与安全

- 默认只监听回环地址 `127.0.0.1`。选择“全部地址”会让隧道端口暴露到外部网络，管理页会显示风险提示。
- 访问密钥只从配置文件读取，不写入代码，不打印到日志；支持配置多个密钥以便轮换。
- 服务器 `tunnel` 账号的密码由宿主机管理员用 `passwd` 设置，不写入配置，也不由网站生成；生成的 `ssh -R` 命令不含密码。
- 远程电脑账号的随机密码由网站在申请时生成，只在结果页、Agent 提示词和本次 API 响应中显示，不写日志、不写入策略快照。结果页应按敏感信息处理。
- 所有修改操作需要 CSRF 令牌；管理页删除需要二次确认，JSON API 删除需要 `X-Confirm-Delete: yes`。
- 登录失败和接口请求都有按来源 IP 的限流。部署在反向代理后时，服务不信任 `X-Forwarded-For`，限流以直连来源为准。
- 时间以 UTC 存储，页面按 `server.timezone` 转换为本地时区展示；容器默认 `TZ=UTC`。
- 建议在服务前放置 HTTPS 反向代理，并将 `access.cookie_secure` 设为 `true`。

## 已知边界

- 不探测远程电脑 SSH 服务是否可用；`established` 只表示宿主机端口在监听。
- 隧道记录在内存中，服务重启后记录丢失；启动时的孤儿清理会清理端口范围内正在监听的 sshd 隧道进程（进程名由 `cleanup.process_names` 配置）。客户端需要重新申请。
- 监听策略可以在管理页修改并持久化到 JSON 快照；修改后只影响新申请的隧道。已签发的 `ssh -R` 命令仍按原策略绑定。
- 修改监听策略后，宿主机 `/etc/ssh/sshport-tunnel.conf` 中 `Match User tunnel` 的 `PermitListen` 限制也要同步重新生成并 reload sshd。OpenSSH 不支持端口范围写法，需要为每个端口生成一个 `PermitListen` 项；否则新地址会被 sshd 拒绝。详见部署文档。
- 不包含数据库、用户注册、多用户角色、计费或审计报表。

## 文档与依赖

- 部署文档：[DEPLOYMENT.md](DEPLOYMENT.md)
- 运行时依赖：[requirements.txt](requirements.txt)
- 开发/测试依赖：[requirements-dev.txt](requirements-dev.txt)
- 配置示例：[config/config.example.toml](config/config.example.toml)
