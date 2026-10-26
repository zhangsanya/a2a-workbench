# A2A 服务端最小实现（本地部署 / 本地存储）

把自家的 agent 按 A2A v1.0 暴露出去，数据全部落本地 SQLite，不依赖任何云服务。

## 为什么能做

A2A 服务端的最小契约只有三样东西：

1. 一个 **Agent Card**（`/.well-known/agent-card.json`），声明能力、绑定、鉴权
2. 一个 **JSON-RPC 入口**，实现 `SendMessage` / `GetTask` / `CancelTask` / `ListTasks`
3. 一套 **任务状态机**，`SUBMITTED → WORKING → COMPLETED|FAILED|CANCELED|REJECTED`

协议本身不重。真正的工程量在长任务、断线、上下文过载、身份审计这几件事上——这个仓库把这几个坑都处理了。

## 和 Elastic Agent Builder 那套的差异

Elastic 不持久化 task，所以 `tasks/get` 会返回 `Task not found`，只能绕道会话存储轮询 `a2a-{contextId}` 文档。

**自己做的最大好处是：直接把 task 存进库，`GetTask` 走标准路径就是通的**，不需要任何绕道。但那个「客户端预生成 contextId」的技巧仍然值得保留——它让跨会话、跨机器恢复成为可能。

## 目录

```
main.py            FastAPI 应用、JSON-RPC 分发、SSE 订阅
store.py           SQLite 持久化（WAL，按 owner 隔离）
agents.py          Agent 注册表与两个本地实现（notes / report）
models.py          数据对象构造、状态常量
client.py          零依赖客户端，用来验证
keys.py            API key 与信封公钥管理
conf.py            配置装载（/etc/a2a-server.env，脚本与服务共用）
seal.py            零依赖密封盒（X25519 + ChaCha20），也可直接发给 agent 用
envelope.py        信封层：投递时封装、回传时拆封
mailbox.py         外部 agent 取件口
workbench.py       群聊后端
static/workbench.html  群聊前端（单文件，无构建）
deploy_vps.sh      海外 VPS 一键部署脚本
Caddyfile.example  Caddy 自动 TLS 配置样例
data/              运行后生成：a2a.db + notes/*.md
```

## 一键部署到 VPS

```bash
scp a2a-server.tar.gz root@<vps>:/tmp/
scp deploy_vps.sh      root@<vps>:/tmp/
ssh root@<vps> 'BASE_URL=https://a2a.example.com bash /tmp/deploy_vps.sh'
```

装完再配 Caddy 拿证书（`Caddyfile.example`），最后把域名 A 记录指过去。
为什么不是 Cloudflare Tunnel，见「部署在哪」一节。

## 本地跑起来

```bash
pip install -r requirements.txt

# 建第一把 key（明文只显示一次）
python keys.py add alice
export A2A_KEY=a2a_xxxxx

uvicorn main:app --host 0.0.0.0 --port 8080
```

或者用环境变量直接引导：

```bash
A2A_BOOTSTRAP_KEY=dev-key A2A_BOOTSTRAP_OWNER=alice uvicorn main:app --port 8080
```

## 验证

```bash
export A2A_BASE=http://127.0.0.1:8080
export A2A_KEY=dev-key

python client.py discover
python client.py card notes
python client.py send notes "A2A 上下文"

# 长任务：故意跑 6 秒以上，中途可以 Ctrl-C 再凭 id 取回
python client.py send report "生成报告"
python client.py result task-xxxxx
python client.py watch task-xxxxx
```

直接用 curl：

```bash
curl -s localhost:8080/agents -H "Authorization: Bearer dev-key" | jq

curl -s -X POST localhost:8080/a2a/notes \
  -H "Authorization: Bearer dev-key" \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "SendMessage",
    "params": {
      "message": {
        "role": "ROLE_USER",
        "parts": [{"text": "A2A", "mediaType": "text/plain"}],
        "contextId": "ctx-demo-001"
      },
      "configuration": {"returnImmediately": true}
    }
  }' | jq
```

## 几个关键设计决策

| 决策 | 原因 |
|---|---|
| **task 直接落库** | 断线后 `GetTask` 能拿到真实状态，不依赖会话存储绕道 |
| **接受客户端预生成的 contextId** | 请求发出前就已知恢复凭据，网络断了也能续 |
| **默认异步，`returnImmediately` 可控** | 入口代理 30–60s 会掐连接，阻塞等长任务必然 502 |
| **`A2A_BLOCKING_TIMEOUT` 默认 25s** | 比代理超时短，宁可返回 `WORKING` 也不返回 502 |
| **身份只认 API key 属主** | 消息 metadata 自报的调用方可以伪造，审计不能建在上面 |
| **只声明 JSONRPC 一种绑定** | Card 里写了 `GRPC` 客户端就会去探测，连不上直接判失败 |
| **进度写进 `status.message`** | 轮询和 SSE 都能看到，不用额外的进度通道 |

## 接自己的 agent

在 `agents.py` 里加一条：

```python
def handle_mine(ctx: agents.AgentContext) -> list[dict]:
    ctx.report("阶段 1/2：查库")
    rows = ...
    ctx.check_cancel()          # 长循环里要主动让位
    ctx.report("阶段 2/2：生成")
    return [{"name": "out.md", "parts": [models.text_part(md, "text/markdown")]}]

AGENTS["mine"] = AgentSpec(
    agent_id="mine", name="我的助手", description="...",
    skills=[{"id": "do-thing", "name": "干活", "description": "...", "tags": ["x"]}],
    handler=handle_mine, heavy=True,
)
```

重启即生效，Card 会自动更新。调用方不用改一行代码——这就是协议的价值。

## 上生产前要补的

- **REST 绑定**：补完再把 `HTTP+JSON` 写进 `supportedInterfaces`（现在只声明 JSONRPC 是故意的）
- **推送通知**：`CreateTaskPushNotificationConfig` 四件套，给不能长连的调用方用
- **TLS 与反代**：A2A 生产环境要求 HTTPS，`A2A_BASE_URL` 要指向公网域名
- **限流与并发**：`EXECUTOR` 目前固定 4 个 worker，重任务要换成队列
- **多租户**：`AgentInterface.tenant` 字段目前没实现
- **审计日志**：现在只记了 owner，生产要记调用方、入参摘要、耗时、产物哈希
- **`history` 裁剪策略**：任务历史会长得很快，建议加定期归档

## 已知限制

- `SubscribeToTask` 用轮询数据库实现，1 秒粒度；高频场景换成事件总线
- 取消是协作式的，handler 必须主动调 `check_cancel()` 才会生效
- 没有实现 `INPUT_REQUIRED` 的交互回路（状态常量已留好）

---

## 群聊工作台（workbench）

浏览器打开 `http://localhost:8080/` 就是一个群。不依赖任何 IM 平台。

### 三种发任务方式

| 写法 | 行为 |
|---|---|
| `帮我统计笔记目录` | 由本地模型判断该派给谁（失败时退回关键词规则） |
| `@report 统计笔记` | 只派给指定 agent |
| `@all 各自汇报` | 广播给所有 agent，并行执行 |

### 实现要点

- 消息落 `chat_messages` 表，前端用游标 `?since=N` 轮询，断线重连不丢消息
- 一条用户消息生成一个 `contextId`，派出去的所有子任务共享它，便于串联
- 派发用 `ThreadPoolExecutor`，多个 agent 同时跑
- 进度写在 `status` 消息里，前端就地更新，任务完成后自动收起
- 受 llama-server `--parallel 1` 限制，多个 agent 同时调模型时会排队

### 接口

```
GET  /                 群聊页面
GET  /chat/members     群成员
GET  /chat/messages    ?since=N 拉新消息
POST /chat/send        {"text": "..."}
POST /chat/clear       清空群聊记录（不影响任务表）
```

### 已知限制

- agent 之间看不到彼此的发言，每条任务只收到用户那句话（不透明原则的副作用）
- 没有文件上传、没有语音、没有多群
- 鉴权沿用 A2A 的 Bearer token，工作台页面本身是匿名可读（内网使用）

---

## 外部 agent 取件口（mailbox）

云端 agent（Muse / Grok Bot 等）跑在厂商的机器上，没有公网入站，也不能被外部唤醒。
但它们自己会定时醒来。所以方向反过来：**不在外面敲它的门，让它按时来取件。**

### 四个接口

```
GET  /mailbox/poll?agent=muse&lease=300   领一个任务（原子认领 + 租约）
POST /mailbox/result                      交结果 {"id":..., "ok":true, "result":"..."}
POST /mailbox/push                        投任务 {"agent":"muse","text":"..."}
GET  /mailbox/list                        队列概览
```

全部需要 `Authorization: Bearer <key>`，无 token 返回 401。

### 在群里派活给外部 agent

群里发 `@muse 帮我统计笔记目录`，本地没有 `muse` 这个 handler，就自动投进取件口，
并在群里留一条「已投进 @muse 的取件口，等它下次醒来领走」。

`@all` 会同时投给本地 agent 和全部外部 agent（`A2A_EXTERNAL_AGENTS`，默认 `muse,grok`）。

### 给 Muse 配定时任务

```
每 15 分钟访问一次
http://<你的地址>/mailbox/poll?agent=muse
（请求头 Authorization: Bearer <key>）
如果返回的 task 是 null，本轮立刻结束，不要做任何事、不要回复我。
如果有任务，按 task.text 执行，然后把结果 POST 到 /mailbox/result，
body 为 {"id": "<task.id>", "ok": true, "result": "<你的产出>"}。
```

### 三个关键设计

| 设计 | 为什么 |
|---|---|
| **租约** | 认领后 lease 秒内不交结果，任务自动回到 pending。agent 半路挂掉不会把任务永久锁死 |
| **原子认领** | 一条 `UPDATE ... WHERE id = (SELECT ...) RETURNING`，两个 poller 同时来不会拿到同一条 |
| **空队列极简响应** | 定时唤醒的 agent 每次都要花 token 思考，「没活」必须一眼可见，所以回一个 hint 让它立刻停 |

### 结果回灌

后台线程每 3 秒扫一次，把已完成且未通知过的任务回灌到群里（`notified` 标记保证只通知一次），
带上原来的 `contextId`，所以能接在派活那条消息后面。

### 还没做的

- 附件传递

---

## 部署在哪：为什么最后放弃了 Cloudflare Tunnel

外部 agent 要能访问到你，就必须有一个公网入口。最自然的想法是 Cloudflare Tunnel，
不用公网 IP、不用开端口、边缘还替你终结 TLS。但它在两种真实网络里都会撞墙。

### 情况一：内网机器没有直连出网

这台机器只能通过宿主机上的一个 HTTP 代理出网（`HTTPS_PROXY=http://<gateway>:10810`）。
实测：

```
curl -x <proxy> https://api.trycloudflare.com/      -> 200 (3.2s)     代理能通
cloudflared tunnel --url ... --protocol http2       -> context deadline exceeded
```

同一个域名，curl 走代理没问题，cloudflared 直接超时——**它不读 `HTTPS_PROXY`**。
隧道连接和 API 调用都绕开代理直连，于是从这台机器根本到不了 CF 边缘。
改代理的路由规则、加白名单都没用，因为流量压根不经过代理。

### 情况二：机器在墙内

```
openssl s_client -servername www.cloudflare.com        @<edge-ip>:443  -> TLSv1.3 握手成功
openssl s_client -servername region1.v2.argotunnel.com @<edge-ip>:443  -> alert 40 handshake_failure
openssl s_client -servername region2.v2.argotunnel.com @<edge-ip>:443  -> alert 40 handshake_failure
```

同一个 IP、同一套 TLS 栈、只换 SNI，结果完全不同。要么是 CF 边缘不认这个 SNI，
要么是 GFW 按域名注入告警——不管是哪种，`*.argotunnel.com` 这条链在路上就是断的。

> 顺带记一笔：这类机器上"给 argotunnel 加直连规则"是反向操作。
> 它本来会被代理节点带出去，加了直连反而让它直接去撞墙。

### 结论：把服务放到墙外

既然入口必须在墙外，就别在墙内做入口了。整个服务部署到海外 VPS：

```
Muse / Grok  ──HTTPS──▶  海外 VPS（本服务 + Caddy 自动 TLS）  ◀──HTTPS──  你的浏览器
```

- 两端都是主动外连，**谁都不需要入站**，也就不需要隧道
- 域名一条 A 记录指过去，证书 Caddy 自己续
- GFW 只影响你自己打开工作台页面（用现有代理即可），
  对 Muse 完全无影响——它跑在 Meta 的云上，连 VPS 不经过墙
- 信封加密仍然有价值：VPS 和 CF 边缘都不是"可信终端"

代价是拿不到内网那台机器上的本地模型和文件。群聊的智能路由会退回关键词规则，
需要模型的话给 VPS 配一个云端 API。

部署脚本见 `deploy_vps.sh`。

---

## 消息加密（信封层）

### 为什么 TLS 不够

TLS 保护的是**链路**，不是**正文**。任务要经过：

```
本机 → 出口代理 → 公网 → 隧道/反代边缘 → 目标 agent
```

只要中间某一跳做了 TLS 终结（Cloudflare Tunnel 就是这样），正文在那里就是明文。
「HTTPS 了就安全了」这个结论在边缘终结的架构下不成立——边缘、代理、WAF、访问日志
都能看见你的任务正文。

所以正文自己再套一层，见 `seal.py` / `envelope.py`。

### 两层各管什么

| 层 | 保护对象 | 谁看得见明文 |
|---|---|---|
| TLS | 链路 | 两端 + 所有终结 TLS 的中间节点 |
| 信封 | 正文 | 只有收发两端 |

叠加使用，不是二选一。

### 算法

```
临时密钥对   X25519（每封信现生成，用完即弃 → 前向保密）
共享密钥     ECDH(临时私钥, 收件人公钥)
派生         HKDF-SHA256(共享密钥, salt=临时公钥||收件人公钥, info="a2a-seal-v1")
加密         ChaCha20（96-bit nonce）
完整性       HMAC-SHA256（encrypt-then-MAC）
```

零依赖，只用标准库，Python 3.8+ 直接跑。实现对齐 RFC 7748 / 8439 / 5869 的官方测试向量：

```bash
python3 seal.py selftest
```

### 信封格式

```
1B 版本 0x01 | 32B 临时公钥 | 12B nonce | nB 密文 | 32B HMAC
```

base64url 无填充。同样的正文每次封出来的密文都不一样（临时密钥对是新的），
但长度会泄露正文长度——这是可接受的，因为接收方本来就知道大概多长。

### 密钥分工

| 谁 | 私钥在哪 | 公钥在哪 |
|---|---|---|
| 服务器 | `/mnt/data/a2a-server/seal-server.key`（0600） | 通过 `GET /crypto/info` 公布 |
| 每个外部 agent | agent 自己的机器上 | 注册到服务器的 `agent_keys` 表 |

所以：群里的任务只有目标 agent 能读，agent 的产出只有服务器能读，中间任何一跳都读不了。

### agent 侧怎么用

`seal.py` 不用安装，直接从服务器下载：

```bash
curl -s -o seal.py <BASE>/a2a/seal.py

# 1. 生成自己的密钥对（私钥留在本地，权限 600）
python3 seal.py keygen muse

# 2. 把公钥注册给服务器（只能给自己注册）
python3 seal.py pub muse.key
curl -s -X POST <BASE>/crypto/pubkey \
  -H "Authorization: Bearer <KEY>" -H "Content-Type: application/json" \
  -d '{"agent":"muse","pubkey":"<上一步输出的公钥>"}'

# 3. 收到信封后解密
python3 seal.py open muse.key "<task.envelope>"

# 4. 交回结果时用服务器公钥加密
python3 seal.py seal "<server_pubkey>" "<你的产出>"
```

### 接口

```
GET  /crypto/info        服务器公钥、已加密的 agent 列表
GET  /a2a/seal.py        下载 seal.py 本体（无任何密钥，公开）
POST /crypto/pubkey      注册自己的公钥 {"agent":"muse","pubkey":"..."}
```

`POST /crypto/pubkey` 只能给自己注册（属主名必须等于 agent 名），
否则 A 可以用自己的公钥顶替 B 收信。

### 投递/回传格式

`poll` 拿到加密任务时，`task` 里是 `envelope` 而不是 `text`：

```json
{"task": {"id": "mb-...", "sealed": true, "envelope": "AfSh-nLQ..."},
 "crypto": {"server_pubkey": "...", "decrypt": "python3 seal.py open ..."}}
```

交结果时带 `"sealed": true`：

```json
{"id": "mb-...", "ok": true, "sealed": true, "result": "AVHNDU8X..."}
```

服务器拆封失败会返回 **400**（而不是 404），错误信息里明确写「信封解不开」，
便于区分是加密层的问题还是 id 写错了。

### 没有公钥的 agent

默认允许明文投递，但群里会明确写「这条是明文」。设 `A2A_REQUIRE_SEAL=1`
可以强制拒绝——适合已经全部迁移完成、不想留后门的阶段。

### 踩过的坑

| 坑 | 现象 | 修法 |
|---|---|---|
| 脚本和服务各自生成密钥 | agent 用 A 加密、服务用 B 解，永远解不开，报错像「被篡改」 | 密钥锚定在 `A2A_DB` 同目录；两边统一读 `/etc/a2a-server.env` |
| 回灌读后写竞态 | 同一条结果在群里出现两遍 | `take_finished` 改成单条 `UPDATE ... RETURNING` |
