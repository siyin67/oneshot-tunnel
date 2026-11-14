# 部署与运维文档

本文档面向 Linux 宿主机。服务只负责分配和回收隧道端口；SSH 服务、sshd 配置、隧道端口的实际监听都在宿主机上完成，不运行在容器内。

## 1. 架构与边界

```text
                    +--------------------------------------+
                    | 宿主机 Linux                          |
                    |                                      |
  浏览器 ----------> |  sshport 容器（host network + pid host）|
                    |  FastAPI :8080                       |
                    |  /proc/net/tcp  -> 宿主机端口表       |
                    |  sudo helper    -> 宿主机 sshd 进程   |
                    |                                      |
  远程 Windows -----> |  宿主机 sshd :22                      |
  ssh -R 命令        |   监听 127.0.0.1 / 0.0.0.0 / 指定 IP |
                    |   端口 30000-30099                    |
                    |                                      |
  本地 Agent -------> |  连接上述监听地址和分配端口            |
                    +--------------------------------------+
```

职责划分：

- 容器：登录、端口分配、命令与提示词生成、状态探测、自动回收、手动删除、策略管理。
- 宿主机 sshd：接受远程 Windows 的 SSH 连接，按 `ssh -R` 请求监听隧道端口，维护保活和断连。
- 容器不映射任何隧道端口，不运行 SSH 服务，不代理 SSH 流量。
- 网站只探测宿主机端口是否在监听，不探测远程 Windows 上的 SSH 服务是否可用。

## 2. 前置条件

- Linux 宿主机，建议使用当前仍受支持的发行版。
- OpenSSH Server 7.6 或更新版本（`AllowTcpForwarding remote` 需要 7.6+）。
- Docker Engine 与 Docker Compose v2。必须使用 Linux 容器；Docker Desktop 的 host network 行为与 Linux 不同，不作为生产部署方式。
- 宿主机安装 `iproute2`（提供 `ip` 和 `ss`）、`sudo`、`tzdata`。镜像内已包含这些包。
- 一个专用的 tunnel 用户（示例为 `tunnel`），并为它设置独立的登录密码。
- 远程电脑为 Windows 10/11 或 Windows Server 2016+，安装 OpenSSH Server 并允许 SSH 密码认证。申请页会生成在远程 Windows 上创建/更新本地专用用户的 PowerShell 命令和随机密码；该用户默认 `sshport`，只加入 Users 组，不加入 Administrators。
- 一个专用端口范围（示例为 30000-30099），不要与网站端口、宿主机其他服务重叠。

## 3. 宿主机 sshd 配置

### 3.1 允许远程端口转发

编辑 `/etc/ssh/sshd_config`（或 `/etc/ssh/sshd_config.d/` 下的独立文件）：

```sshconfig
# 只允许远程转发，不允许本地转发和动态转发。
AllowTcpForwarding remote

# 关键：允许客户端在 -R 中指定绑定地址。
# 设为 no 会强制只绑定回环地址，all 和 specific 模式无法生效；
# 设为 yes 会忽略客户端地址并强制通配绑定，无法实现仅回环。
GatewayPorts clientspecified

# 及时感知远程 Windows 断线，配合命令中的 ServerAlive* 参数。
ClientAliveInterval 15
ClientAliveCountMax 3
```

`AllowTcpForwarding remote` 需要 OpenSSH 7.6 或更新版本。更旧的版本没有只允许远程转发的开关；不要用 `PermitOpen none` 代替，它限制的是转发目的地而不是转发方向，启用后会破坏隧道目标访问。建议升级 OpenSSH，而不是使用旧版回退配置。

检查并重载：

```bash
sudo sshd -t
sudo systemctl reload sshd
# Debian/Ubuntu 的服务名可能是 ssh
```

确认生效：

```bash
sudo sshd -T | grep -E 'allowtcpforwarding|gatewayports|clientalive'
```

### 3.2 创建受限隧道用户

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin tunnel
sudo passwd tunnel
```

说明：

- `nologin` 防止交互式 shell；`ssh -N` 不请求 shell，端口转发仍可工作。
- `passwd` 设置的是服务器 `tunnel` 账号的 SSH 登录密码。它不会写入配置文件，也不会写进网站生成的命令；执行 `ssh -R` 时由终端提示输入。
- 该密码必须独立、足够强，不能与网站访问密钥或其他账号密码相同。
- 不要给该用户 sudo 权限，也不要把它加入 docker 组。
- 不需要为这个账号安装公钥；本方案只使用密码认证。

`sshd` 侧的密码认证、禁止 shell 和监听限制统一放在 3.3 生成的 `Match User tunnel` 块中。不要在本节手工追加另一份 `Match`，否则两个 `Match` 块和 `Include` 的顺序容易互相干扰。

需要理解的限制：

- 只允许密码认证，不接受公钥；`PasswordAuthentication yes` 与 `AuthenticationMethods password` 配合。
- `MaxSessions 0` 禁止 shell、登录和 subsystem 会话，但仍允许端口转发；`ForceCommand` 和 `PermitTTY no` 是额外防线。
- 真正限制可监听地址和端口的是下一节的 `PermitListen`。
- 如果 `tunnel.user` 不是 `tunnel`，3.3 的 `Match User` 必须同步改成实际用户名。

### 3.3 配置密码认证与 PermitListen

服务器 `tunnel` 账号使用密码认证，因此不能再用 `authorized_keys` 里的 `permitlisten` 限制监听地址：那个选项只在公钥认证时读取。密码认证下，监听地址限制必须写在 `sshd_config` 的 `PermitListen` 中。

`PermitListen` 只接受单个端口（`port`）或 `主机:端口`（`host:port`），不接受 `30000-30099` 这样的范围；多个权限用**空白**分隔，不能用逗号。下面根据当前策略生成一个独立的配置文件，并把它 Include 到主配置末尾。不要把含 `Match` 的文件放进 Debian/Ubuntu 的 `sshd_config.d/`，因为该目录通常在 `sshd_config` 开头被 Include，`Match` 块可能把后面的全局配置也吞进去。

脚本假设配置中的 `tunnel.user` 为 `tunnel`、端口范围为 `30000-30099`。如果改过这两项，必须同步修改脚本中的 `Match User` 和 `seq` 范围；`BIND` 按当前监听策略填写。

```bash
# 仅回环策略；all 模式改为 0.0.0.0；specific 模式改为实际网卡 IP。
BIND=127.0.0.1

# IPv6 地址必须写成 [addr]:port，不能写成 ::1:30000。
case "$BIND" in
  *:*) BIND_SPEC="[$BIND]" ;;
  *)   BIND_SPEC="$BIND" ;;
esac

LISTEN=""
for port in $(seq 30000 30099); do
  LISTEN="$LISTEN $BIND_SPEC:$port"
done

sudo tee /etc/ssh/sshport-tunnel.conf >/dev/null <<EOF
Match User tunnel
    GatewayPorts clientspecified
    PasswordAuthentication yes
    PubkeyAuthentication no
    KbdInteractiveAuthentication no
    AuthenticationMethods password
    PermitTTY no
    MaxSessions 0
    ForceCommand /bin/false
    AllowAgentForwarding no
    AllowTcpForwarding remote
    X11Forwarding no
    PermitListen$LISTEN
    ClientAliveInterval 15
    ClientAliveCountMax 3
EOF

sudo chmod 600 /etc/ssh/sshport-tunnel.conf

# 只在主配置末尾添加一次 Include。
if ! sudo grep -qxF 'Include /etc/ssh/sshport-tunnel.conf' /etc/ssh/sshd_config; then
  echo 'Include /etc/ssh/sshport-tunnel.conf' | sudo tee -a /etc/ssh/sshd_config >/dev/null
fi

sudo sshd -t
sudo systemctl reload sshd
```

上面的 `LISTEN` 会展开成类似 `127.0.0.1:30000 127.0.0.1:30001 ... 127.0.0.1:30099` 的空白分隔列表，最终一行是 `PermitListen 127.0.0.1:30000 ...`；如果 `BIND` 是 IPv6，则每项形如 `[::1]:30000`。100 个端口约 2 KB，仍在可控范围内；不要把端口范围设得过大。

确认生效：

```bash
sudo sshd -T -C user=tunnel,host=localhost,addr=127.0.0.1 \
  | grep -E '^(passwordauthentication|pubkeyauthentication|maxsessions|permittty|forcecommand|allowtcpforwarding|gatewayports|permitlisten)'
```

策略变更后：

- 管理页切换监听策略只影响新申请的隧道。必须用新的 `BIND` 重新执行上面的生成脚本并 reload sshd，否则新地址或新端口会被 `PermitListen` 拒绝。
- 重新生成会整体替换 `/etc/ssh/sshport-tunnel.conf`，不要手工拼接旧内容。
- `specific` 模式的 `BIND` 必须是 `ip -o addr show` 中真实存在的单个地址，不能写 CIDR；地址变化后要重新生成。
- 已建立的隧道保持原绑定地址，不需要也不会被重载中断。

验证远程转发：

```bash
# 在远程 Windows 的 PowerShell 或 cmd 中执行网站生成的命令（下面只是示例）；
# 终端会提示输入服务器 tunnel 用户的密码。
ssh -N -T -p 22 \
  -o ExitOnForwardFailure=yes \
  -o PreferredAuthentications=password \
  -o PubkeyAuthentication=no \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -R 127.0.0.1:30000:localhost:22 \
  tunnel@tunnel.example.com

# 在宿主机检查
ss -ltnp 'sport = :30000'
```

验证不能登录 shell：

```bash
ssh -tt tunnel@tunnel.example.com
# 应被拒绝或立即退出，不能获得交互式 shell
```

远程 Windows 上的登录用户由申请页生成，不在这台宿主机上创建；其密码也是申请页随机生成并展示的。申请页命令使用 PowerShell 的 `New-LocalUser` / `Set-LocalUser` 创建或更新本地标准用户，并把它加入 Users 组；需要本地管理员权限，不能在域控制器上使用 `New-LocalUser`。远程 Windows 的 OpenSSH Server 必须允许密码认证，否则本地 Agent 无法通过隧道登录。

远程 Windows 上确认密码认证：

```powershell
# 以管理员身份打开 PowerShell。
Get-Content C:\ProgramData\ssh\sshd_config | Select-String 'PasswordAuthentication'
Get-Service sshd
# 如修改了 sshd_config，重启服务。
Restart-Service sshd
```

`sshd_config` 中应有 `PasswordAuthentication yes`（未被注释）。Windows 本地账号必须设置非空密码；结果页第一步生成的随机密码已满足默认复杂度策略。不要把该账号加入 Administrators。

### 3.4 防火墙和地址

- 网站端口（默认 8080）只对管理员网络开放；公网部署建议放在 HTTPS 反向代理之后。
- 仅回环策略不需要开放隧道端口。
- 全部地址策略需要防火墙放行隧道端口范围；这会把远程 Windows 的 SSH 端口暴露给能访问这些端口的网络。
- 指定地址策略只需在对应网卡和网络范围内放行。
- 云厂商安全组、宿主机 nftables/iptables、上游防火墙都要检查。

## 4. 监听地址策略

| 策略 | `ssh -R` 绑定地址 | Agent 连接地址 | 适用场景 | 风险 |
| --- | --- | --- | --- | --- |
| `loopback`（默认） | `127.0.0.1` | `127.0.0.1` | Agent 与 sshd 在同一宿主机 | 最低；其他机器无法连接 |
| `all` | `0.0.0.0` | 配置中的 `tunnel.public_host` | 需要从外部直接连接隧道端口 | 端口暴露到网络，必须配合防火墙和 Agent 侧访问控制 |
| `specific` | 配置/管理页中的单个 IP | 同一个 IP | 指定内网或虚拟网卡地址 | 依赖该地址稳定存在 |

规则：

- 默认策略是 `loopback`，配置示例也使用它。
- `all` 模式生成 Agent 提示词时绝不能使用 `0.0.0.0`；服务会使用 `tunnel.public_host`，例如公网 DNS 名称或可路由 IP。
- `specific` 只接受单个 IP，不接受 CIDR。管理页保存时，后端执行 `ip -o addr show` 校验该 IP 在当前宿主机网卡上；不存在则拒绝保存。
- 管理页修改策略只影响之后新申请的隧道；已建立隧道保留申请时的绑定地址。
- 修改策略后还要同步更新 `/etc/ssh/sshport-tunnel.conf` 中 `Match User tunnel` 的 `PermitListen` 列表，并 reload sshd，原因见 3.3。

虚拟网卡示例：

```bash
ip -o addr show
ip -o addr show dev eth0
```

把输出中的 `inet 10.0.0.5/24` 的 `10.0.0.5` 填入指定地址。虚拟网卡（bridge、tun、云厂商弹性网卡）可能在重启、迁移或网络重配后消失或换地址；这种变化会导致新申请无法监听。需要固定地址时，优先使用有稳定地址的物理/主网卡，或在系统网络配置中固定该地址。地址消失后，管理页保存会拒绝；已建立但失效的隧道应在管理页手动删除。

服务启动时也会检查当前策略中的指定 IP（来自配置文件或 `data/policy.json` 快照）。如果该 IP 不在宿主机任何网卡上，服务会抛出错误并拒绝启动，错误信息包含“不在宿主机任何网卡上”。处理方式是恢复该地址、修正配置中的 `tunnel.specific_listen_ip`，或删除策略快照后改用配置文件策略。

## 5. Docker 网络模式与权限

### 5.1 为什么使用 host network

`docker-compose.yml` 使用：

```yaml
network_mode: host
pid: host
```

原因：

1. 隧道端口由宿主机 sshd 监听，不在容器内。容器必须看到宿主机的真实端口表，才能做“端口是否已被监听”的判断。
2. 服务读取 `/proc/net/tcp` 和 `/proc/net/tcp6`。使用 host network 时，容器与宿主机共享网络命名空间，这两个文件反映宿主机监听状态；使用默认 bridge 网络时只能看到容器自己的网络命名空间。
3. 不需要也不应该为隧道端口做 `ports:` 映射。端口映射会引入额外转发层，且端口范围很大时维护困难。
4. `pid: host` 让清理 helper 能看到宿主机 sshd 的 PID。仅共享网络命名空间不足以结束宿主机进程。

`network_mode: host` 后，`server.listen_host=0.0.0.0`、`server.port=8080` 直接占用宿主机 8080。请先确认该端口未被其他服务占用。

### 5.2 清理权限与最小权限

服务进程以非 root 用户 `sshport`（uid/gid 10001）运行。它不能直接向宿主机 root 的 sshd 发送信号，因此镜像内安装了一个受限 helper：

- `/usr/local/libexec/sshport-cleanup`，root:root，0750。
- `/etc/sudoers.d/sshport` 只允许 `sshport` 无密码执行这个 helper，不允许其他命令。
- helper 只接受：端口、端口范围、超时、允许的进程名列表。
- helper 先用 `ss` 找出监听该端口的 PID，重新读取 `/proc/<pid>/comm` 校验进程名，只对允许名单中的进程先 SIGTERM，超时后才 SIGKILL。
- 端口不在配置范围内、进程名不在名单内、任一 PID 不符合条件时拒绝执行。
- helper 不读取配置文件、不执行 shell、不接收任意命令。

这比 `privileged: true`、挂载 Docker socket 或给容器完整 sudo 权限的范围小得多。代价是：容器被攻破时，攻击者可以在允许的端口范围内结束允许名单中的监听进程，造成这些隧道中断。这是清理能力本身带来的风险，应通过限制 Docker 管理权限、及时更新镜像、只暴露必要端口来降低。

注意：

- 不要使用 rootless Docker 期待同样的 host PID/网络可见性；rootless 模式与本文的清理方案不兼容。
- 不要在容器内安装或启动 sshd。
- 如果宿主机启用了 SELinux，挂载卷可能需要 `:z` 或 `:Z` 标签；先阅读发行版文档。
- helper 的 sudo 规则只允许该路径。升级 helper 后要重新构建镜像，不要手工放宽 sudoers。

### 5.3 配置、数据和日志卷

`docker-compose.yml` 默认挂载：

| 宿主机路径 | 容器路径 | 权限 | 用途 |
| --- | --- | --- | --- |
| `./config` | `/app/config` | 只读 | `config.toml`，含访问密钥 |
| `./data` | `/app/data` | 读写 | 监听策略快照 `policy.json` |
| `./keys` | `/app/keys` | 只读 | 可选：运维材料；当前密码认证方案不使用密钥，服务本身不读取 |
| `./logs` | `/app/logs` | 读写 | 可选日志收集；应用日志仍写 stdout |

配置不打进镜像。`.dockerignore` 排除了 `config/`、`data/`、`logs/`、`keys/` 和测试文件。

准备目录和权限：

```bash
sudo install -d -m 0750 -o 10001 -g 10001 config data logs keys
sudo install -m 0600 -o 10001 -g 10001 config/config.example.toml config/config.toml
```

容器用户需要读取配置，因此配置文件属主设为 10001；也可以使用只读组权限，但不要让密钥变成全局可读。`data` 必须可写，用于原子写入策略快照。

如果使用 bind mount 且宿主机启用了用户命名空间映射，uid 10001 可能对应宿主机上的其他 uid；此时用 `docker compose run --rm sshport id` 确认，或改用命名卷并调整权限。

## 6. 构建与启动

```bash
cp config/config.example.toml config/config.toml
# 编辑配置：访问密钥、隧道用户、服务器地址、端口范围、对外地址、策略
mkdir -p data logs keys
sudo chown 10001:10001 data logs config/config.toml
sudo chmod 0600 config/config.toml

docker compose build
docker compose up -d
docker compose ps
docker compose logs -f sshport
```

健康检查：

```bash
curl -fsS http://127.0.0.1:8080/healthz
# {"status":"ok","config":"loaded","host_network":"ok","timezone":"UTC","port_probe":"ok"} 之类
```

`/healthz` 会实时读取宿主机端口表；读取失败返回 503。容器健康检查每 30 秒执行一次，连续失败会显示 unhealthy。

修改配置后：

```bash
docker compose restart sshport
```

访问 `http://<宿主机>:8080/`，使用 `access.keys` 中的任意一个密钥登录。

停止：

```bash
docker compose down
```

`down` 不会删除 bind mount 的配置和数据。策略快照保留；内存中的隧道记录会丢失。

### 6.1 服务重启后的行为

- 隧道记录只存在内存中，重启后全部丢失。
- `cleanup.clean_orphans_on_startup = true` 时，服务启动会扫描配置端口范围内正在监听的端口，并调用清理器清理允许的 sshd 进程。这样可避免旧隧道继续占用端口。
- 如果端口范围内还有不是本服务创建、但由 sshd 监听的服务，会被误清理。务必使用专用端口范围，并在部署前检查。
- 已登录会话也保存在内存中，重启后需要重新登录。
- 管理页策略快照保存在 `data/policy.json`，重启后优先于配置文件中的初始策略。要回到配置文件策略，停止服务、删除该快照、重启。

## 7. 配置字段

配置文件为 TOML，路径由环境变量 `SSHPORT_CONFIG` 指定，容器内默认 `/app/config/config.toml`。除 `cleanup.clean_orphans_on_startup`（可省略，默认 `true`）和 `storage.policy_state_file`（可省略或留空，表示不持久化策略快照）外，所有字段都是必需的；未知字段、缺失字段、类型错误或非法值会导致启动失败，并在 stderr 输出中文错误列表。

### 7.1 `[server]`

| 字段 | 类型 | 说明 | 约束 |
| --- | --- | --- | --- |
| `listen_host` | 字符串 | 网站监听地址 | 通常 `0.0.0.0`；host network 下直接监听宿主机 |
| `port` | 整数 | 网站端口 | 1-65535，且不能落在隧道端口范围内 |
| `timezone` | 字符串 | 页面展示时区 | 必须是有效的 IANA 时区，如 `UTC`、`Asia/Shanghai` |

存储始终为 UTC；页面通过该时区转换显示。

### 7.2 `[access]`

| 字段 | 类型 | 说明 | 约束 |
| --- | --- | --- | --- |
| `keys` | 字符串数组 | 登录/管理共用的访问密钥 | 非空、不能重复；生产环境使用长随机值 |
| `session_ttl_seconds` | 整数 | 会话绝对有效期 | 60-31536000 |
| `cookie_secure` | 布尔 | 仅通过 HTTPS 发送 cookie | HTTPS 部署设为 `true` |

`[access.login_rate_limit]`：

| 字段 | 说明 | 约束 |
| --- | --- | --- |
| `max_failures` | 窗口内允许的失败次数 | 正整数 |
| `window_seconds` | 失败计数窗口 | 正整数 |
| `lockout_seconds` | 达到上限后的锁定时长 | 正整数 |

登录失败统一返回同一提示，不区分密钥错误和限流。

### 7.3 `[tunnel]`

| 字段 | 类型 | 说明 | 约束 |
| --- | --- | --- | --- |
| `user` | 字符串 | 远程 Windows 连接服务器使用的受限 SSH 用户 | 合法用户名；该账号需在宿主机设置密码，密码不写入本配置 |
| `server_host` | 字符串 | 隧道服务器地址，写入 `ssh -R` 命令 | 主机名或 IP，不带端口/协议 |
| `server_port` | 整数 | 隧道服务器 SSH 端口 | 1-65535，且不在隧道端口范围内 |
| `port_start` / `port_end` | 整数 | 宿主机可分配的闭区间端口范围 | 1-65535，start <= end |
| `default_ttl_seconds` | 整数 | 申请页默认有效期 | 60-31536000，且必须属于 allowed 档位 |
| `allowed_ttl_seconds` | 整数数组 | 申请页下拉档位（秒） | 非空、无重复、60-31536000 |
| `dedupe_window_seconds` | 整数 | 同源重复申请复用窗口 | 1-86400 |
| `expired_retention_seconds` | 整数 | 过期记录在管理页保留时间 | 60-86400 |
| `cleanup_timeout_seconds` | 整数 | 清理进程等待时间 | 1-120 |
| `listen_mode` | 字符串 | 初始策略 | `loopback`、`all`、`specific` |
| `specific_listen_ip` | 字符串 | specific 模式的单个 IP | specific 时必填；不能是 CIDR 或通配地址 |
| `public_host` | 字符串 | all 模式给 Agent 的对外地址 | all 时必填；不能是 `0.0.0.0`/`::`，不能带端口 |
| `keepalive_interval_seconds` | 整数 | `ServerAliveInterval` | 1-3600 |
| `keepalive_count_max` | 整数 | `ServerAliveCountMax` | 正整数 |

`tunnel.user` 只是服务器侧的 SSH 账号名。该账号的密码由宿主机管理员用 `passwd` 设置，不在配置文件中，也不由网站生成或保存。远程 Windows 上被操作的账号则是另一回事：它由申请页生成，用户名默认 `sshport`，密码随机生成并显示在结果页。

`listen_mode` 是首次启动的初始值。管理页保存过策略后，`data/policy.json` 优先；删除该文件可恢复配置文件值。

### 7.4 `[limits.application]` 与 `[limits.status]`

| 字段 | 说明 |
| --- | --- |
| `max_requests` | 窗口内最大请求数 |
| `window_seconds` | 滑动窗口长度 |

状态接口的限流阈值必须严格高于申请接口（按有效速率比较）。示例为申请 10 次/60 秒、状态 180 次/60 秒，前端轮询间隔 5 秒。

### 7.5 `[status]`、`[reaper]`

| 字段 | 说明 | 约束 |
| --- | --- | --- |
| `status.poll_interval_seconds` | 前端轮询间隔 | 5-300 |
| `reaper.interval_seconds` | 自动回收检查周期 | 1-3600 |

### 7.6 `[cleanup]`

| 字段 | 说明 |
| --- | --- |
| `strategy` | `auto`、`direct` 或 `helper` |
| `helper_path` | 镜像内 helper 路径，默认 `/usr/local/libexec/sshport-cleanup` |
| `sudo_path` | `sudo` 路径，默认 `/usr/bin/sudo` |
| `process_names` | 允许清理的进程名；示例包含 `sshd` 和 `sshd-session`（OpenSSH 9.8+ 会话进程名） |
| `clean_orphans_on_startup` | 启动时清理端口范围内的残留监听；可省略，默认 `true` |

`auto` 先尝试 `sudo -n helper`，失败后尝试 direct。非 root 容器内 direct 通常看不到其他用户进程的 PID，因此生产环境应使用 helper。`helper` 模式只允许 helper，失败就报告。

### 7.7 `[logging]`、`[storage]`

| 字段 | 说明 |
| --- | --- |
| `logging.level` | `DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL` |
| `logging.access_log` | 是否记录 HTTP 方法/路径/状态/耗时/来源 |
| `storage.policy_state_file` | 策略 JSON 快照路径；相对路径相对配置文件目录；可省略或留空（不持久化）；容器内建议 `/app/data/policy.json` |

日志写 stdout，UTC 时间戳，不记录请求体、访问密钥、完整 SSH 命令或 Agent 提示词。

## 8. 日常操作

### 8.1 申请隧道

1. 登录后打开申请页。
2. 填写远程 Windows 的 SSH 用户名（默认 `sshport`，必须是专用标准用户，最多 20 个字符）、远程 SSH 端口（默认 22）、有效期、备注。填写 `root` 或 `Administrator` 会被拒绝。
3. 提交后，结果页第一步给出在远程 Windows 上执行的 PowerShell 命令和随机密码。在远程 Windows 上以管理员身份打开 PowerShell，粘贴执行一次。命令会创建或更新本地用户、设置不过期密码、启用账户，并加入 Users 组；若同名用户已存在会重置其密码，因此请确认用户名是专用账号。命令包含密码，不要在多人可见的终端或日志中保留。
4. 确认远程 Windows 的 OpenSSH Server 允许密码认证。若远程 sshd 禁止密码登录，本方案无法建立 Agent 连接；不要为此修改网站或重置远程 SSH 配置，先与远程电脑拥有者确认。
5. 结果页第二步复制 `ssh -R` 命令，在**远程 Windows** 的 PowerShell 或 cmd 中执行；终端提示的是服务器 `tunnel` 用户的密码，不是上一步生成的远程密码。
6. 把第三步的 Agent 提示词复制给本地 Agent。Agent 使用远程 Windows 用户名和随机密码登录远程电脑，需要时通过 PowerShell 执行命令。
7. 观察状态从“已分配，待连接”变为“已建立”。

远程用户密码只在申请结果页、Agent 提示词和本次 API 响应中出现；服务不把它写入日志或策略快照。隧道记录在内存中，服务重启后密码和记录一起丢失。

状态只反映宿主机端口监听情况，不代表远程 SSH 服务一定可用。

### 8.2 管理页

- 查看所有隧道记录、创建时间、剩余有效期、监听地址、状态、备注。
- 删除单条隧道：先点“删除”，再点“确认删除”。后端会立即调用清理逻辑；清理成功才回收端口。
- 修改监听策略：选择仅回环、全部地址或指定地址；指定地址会校验宿主机网卡。保存只影响新申请。

### 8.3 自动回收

后台按 `reaper.interval_seconds` 检查：

- 超过有效期：标记过期并清理。
- 曾经建立、现在端口不再监听：标记过期并清理。
- 从未建立的“待连接”记录不会仅因为端口未监听而立即过期；它会在有效期到达时过期。
- 清理失败的记录保留为“清理待重试”，下轮继续；端口不会在清理失败时被重新分配，避免误杀新隧道。
- 清理成功后端口回到可用池；过期记录保留 `expired_retention_seconds` 后在管理页移除。

## 9. 故障排查

### 9.1 端口没有监听

检查顺序：

1. 远程 Windows 上的 `ssh` 进程是否仍在运行，命令是否包含 `ExitOnForwardFailure=yes`。
2. 宿主机 `sshd -T` 是否显示 `gatewayports clientspecified` 和 `allowtcpforwarding remote`；如果全局配置了 `AllowUsers`/`AllowGroups`，确认 `tunnel` 被允许。
3. `/etc/ssh/sshport-tunnel.conf` 的 `Match User tunnel` 是否包含当前策略地址和端口对应的 `PermitListen`；改过策略但没重新生成或没 reload 是常见原因。
4. 服务器 `tunnel` 用户是否已设置密码，`PasswordAuthentication` 是否在 Match 中启用；密码错误会直接认证失败。
5. 远程 Windows 的 OpenSSH Server 是否允许密码认证，本地账号是否启用或锁定；在远程 Windows 的 PowerShell 中检查 `Get-Content C:\ProgramData\ssh\sshd_config | Select-String PasswordAuthentication`、`Get-Service sshd`、`Get-LocalUser -Name <远程用户>`，必要时查看 `OpenSSH/Operational` 事件日志。
6. 端口是否在 `tunnel.port_start..port_end` 内。
7. 宿主机防火墙/云安全组是否放行（all/specific 模式）。
8. `journalctl -u sshd` 或 `/var/log/auth.log` 中是否有 `remote port forwarding failed`、`administratively prohibited`。

### 9.2 全部地址模式仍只能本机连接

- `GatewayPorts no` 会强制绑定回环；改为 `clientspecified`。
- 只开放了 `127.0.0.1` 的防火墙规则。
- `public_host` 不是实际可达地址，或 DNS 未解析到宿主机。
- 云厂商安全组未放行端口范围。

### 9.3 指定地址保存被拒绝

管理页会提示“IP ... 不在宿主机任何网卡上”。在宿主机执行：

```bash
ip -o addr show
```

确认地址存在且没有写错。容器使用 host network，因此看到的是宿主机网卡；如果是虚拟网卡，确认其已启动。CIDR（如 `10.0.0.5/24`）会被拒绝，只填单个 IP。

### 9.4 清理失败

- 确认容器以 `pid: host` 运行。
- 确认 helper 存在且 sudo 规则生效：

```bash
docker compose exec sshport sudo -n /usr/local/libexec/sshport-cleanup 30000 30000 30099 5 sshd
```

- 确认 `cleanup.process_names` 包含实际监听进程名。传统 OpenSSH 为 `sshd`，OpenSSH 9.8+ 的会话进程通常为 `sshd-session`；示例配置同时包含两者。
- 查看容器日志中的 `隧道清理失败` 和 `killed_pids`。
- 不要用 `docker compose down` 代替清理；停止容器不会自动结束宿主机上已建立的 sshd 隧道进程，除非启动时孤儿清理再次运行。

### 9.5 健康检查失败

```bash
docker compose logs sshport
docker compose exec sshport python /app/docker/healthcheck.py
```

常见原因：配置非法、`/proc/net/tcp` 不可读、8080 被占用、host network 未生效、时区配置无效。

### 9.6 登录被锁定

等待 `lockout_seconds` 后重试，或重启服务清空内存中的失败计数。密钥轮换时在 `access.keys` 中保留旧密钥一段时间，再移除旧值并重启。

## 10. 安全清单

- [ ] 已替换示例访问密钥，使用足够长的随机值，且未提交到 Git。
- [ ] `config.toml` 权限为 0600，属主为容器用户；配置目录不对普通用户开放。
- [ ] 网站前有 HTTPS 反向代理，`cookie_secure = true`。
- [ ] 默认使用 `loopback` 策略；只有明确需要时才使用 `all`，并配置防火墙。
- [ ] `tunnel` 用户只使用密码认证、不能 shell、只能远程转发；密码独立且足够强。
- [ ] `/etc/ssh/sshport-tunnel.conf` 的 `Match User tunnel` 使用 `MaxSessions 0`、`PermitTTY no`、`ForceCommand /bin/false`，并按策略生成 `PermitListen`；策略变更后重新生成并 reload。
- [ ] 远程 Windows 使用申请页创建的本地标准用户（不是 Administrator，且不在 Administrators 组）；随机密码不写入日志或持久化文件。
- [ ] 服务器 `tunnel` 密码未写入 `config.toml`、未提交 Git、未与网站访问密钥复用。
- [ ] 端口范围专用，启动孤儿清理不会命中其他服务。
- [ ] Docker 管理权限仅授予可信管理员；不使用 `privileged` 或 Docker socket 挂载。
- [ ] 定期更新基础镜像和 Python 依赖，重建容器。
- [ ] 备份 `data/policy.json`（如果希望保留管理页策略）。

## 11. 备份、升级与卸载

备份策略快照：

```bash
sudo cp data/policy.json data/policy.json.bak
```

升级：

```bash
git pull
docker compose build
docker compose up -d
```

升级会重启服务，内存中的隧道记录丢失；启动孤儿清理会处理端口范围内的残留监听。

卸载：

```bash
docker compose down
# 确认不再需要后，再删除配置、数据和日志
```

不要直接删除正在使用的端口范围或 sshd 配置，先确认没有活动隧道。

## 12. 验收对照

| 验收项 | 操作 |
| --- | --- |
| 未登录拦截 | 清除 cookie 后访问 `/apply`、`/admin`，应 303 到 `/login`；API 返回 401 |
| 密钥配置生效 | 修改 `access.keys` 并 `docker compose restart`，旧密钥失效、新密钥可登录 |
| 申请返回命令 | 登录后申请，页面显示端口、`ssh -R` 命令、远程用户创建命令和 Agent 提示词 |
| 远程用户创建 | 在远程 Windows 的管理员 PowerShell 执行结果页第一步命令，`Get-LocalUser -Name sshport` 显示 Enabled，`Get-LocalGroupMember -Group Users` 包含该用户，密码为页面显示值 |
| 密码认证生效 | 服务器 `ssh -R` 终端提示输入 `tunnel` 密码；Agent 连接远程 Windows 时使用生成的远程密码 |
| 远程执行后监听 | 在另一台机器执行命令，宿主机 `ss -ltnp` 看到对应地址和端口 |
| 策略隔离 | 切换策略后新申请使用新绑定地址，旧记录保留原地址 |
| 指定地址校验 | 管理页填不存在的 IP，返回 400 和“不在宿主机任何网卡上” |
| 状态轮询 | 端口未监听为 pending，执行命令后 established，断线/到期后 expired |
| 页面隐藏暂停 | 切换浏览器标签页，Network 面板不再发状态请求；返回后恢复 |
| 轮询失败 | 临时断开网络/让接口 500，徽标保留上次状态并显示重试提示 |
| 到期回收 | 有效期过后端口从 `ss` 消失，管理页显示过期 |
| 手动删除 | 二次确认后端口立即不再监听，日志包含端口、时间、来源 |
| 自动回收 | 不手动删除，等待 reaper 周期后过期记录被处理 |
| 端口池耗尽 | 将范围设得很小并占满，申请返回明确错误 |
| 同源去重 | 短时间重复申请返回同一隧道，不增加记录 |
| 无 emoji/CDN | 页面源码无 emoji、无外部 URL；断网后样式和脚本仍可用 |
| 窄屏可用 | 浏览器宽度缩到 360px，表单和表格可操作 |
| Docker 健康 | `docker compose ps` 为 healthy，`/healthz` 返回 ok |
