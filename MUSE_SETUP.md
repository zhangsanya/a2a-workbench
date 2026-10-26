# 把 Muse 接进取件口（加密版）

前置：服务器已有公网 HTTPS 地址（例：`https://a2a.example.com`）。
下面 `<BASE>` 都替换成这个地址，`<KEY>` 换成 Muse 专用 key。

---

## 一、Muse 侧只有一个动作：建一个定时任务

在 Muse 里新建 Goal 或定时任务，把下面整段贴进去。**注意第 0 步只在第一次做。**

```
这是一个固定流程，每次按顺序执行，不要跳过、不要改写。

【第 0 步 · 只在第一次做】
如果 /tmp/muse.key 不存在，说明还没初始化，执行：
  curl -s -o /tmp/seal.py <BASE>/a2a/seal.py
  cd /tmp && python3 seal.py keygen muse
  cd /tmp && python3 seal.py pub muse.key
  拿到输出的公钥后注册：
  curl -s -X POST <BASE>/crypto/pubkey \
    -H "Authorization: Bearer <KEY>" -H "Content-Type: application/json" \
    -d '{"agent":"muse","pubkey":"<上一步输出的公钥>"}'
如果 /tmp/muse.key 已存在，跳过这一步。

【第 1 步】取件
  curl -s -H "Authorization: Bearer <KEY>" \
    "<BASE>/mailbox/poll?agent=muse&lease=900"

【第 2 步】如果返回的 "task" 是 null
  本轮立刻结束。不要做任何事，不要通知我，不要说任何话。

【第 3 步】如果有任务
  3a. 正文是加密的，在 task.envelope 里。解密：
      cd /tmp && python3 seal.py open muse.key "<task.envelope>"
      解出来的明文就是我要你做的事。
  3b. 按明文内容执行，产出真实结果。不要复述任务，不要解释你在做什么。
  3c. 把结果加密后交回：
      先拿服务器公钥：
        curl -s -H "Authorization: Bearer <KEY>" "<BASE>/crypto/info"
      从中取 "server_pubkey" 的值，然后：
        cd /tmp && python3 seal.py seal "<server_pubkey>" "<你的产出>"
      把输出的那串密文作为 result 提交：
        curl -s -X POST -H "Authorization: Bearer <KEY>" \
          -H "Content-Type: application/json" \
          -d '{"id":"<task.id>","ok":true,"sealed":true,"result":"<密文>"}' \
          "<BASE>/mailbox/result"
  3d. 失败就用 {"id":"<task.id>","ok":false,"sealed":false,"error":"<原因>"}

【第 4 步】提交成功后本轮结束，不需要再回复我。
```

要点：

- **第 2 步必须写得这么硬。** 不然 Muse 每次空跑都会回你一句「队列是空的」，一天几十条，很烦，也费 token。
- **第 0 步只跑一次。** 密钥存在它自己的机器上，`/tmp` 重启会没，建议改成 `~/muse.key` 这类持久路径。
- `lease=900` 是 15 分钟。任务跑得久就调大，但别超过 3600。
- 提交成功的结果会自动回灌到 workbench 群里，你不用在 Muse 里看。
- **私钥永远不离开 Muse 那台机器。** 你在群里发的任务正文，服务器发出去时就是密文，只有它能解。

---

## 二、加密到底防住了谁

任务从群里出发到 Muse 手里，要过：

```
服务器 → 出口代理 → 公网 → 隧道边缘 → Muse
```

- **TLS** 只管链路。隧道边缘会终结 TLS，所以边缘那一层看到的是明文 HTTP。
- **信封** 管正文。服务器在发出去之前就封好了，边缘、代理、任何中间节点拿到的只是一串 base64。

即使哪天公网入口被中间人接管，它拿到的也只是密文。
反过来，Muse 交回的结果也用服务器公钥加密，同样只有服务器能解。

---

## 三、第一次会卡在 Sentinel

Muse 每次对外发请求都要经 Sentinel 检查。第一次访问 `<BASE>` 这个陌生域名，大概率弹审批。

- 选 Allow 之后，同类请求通常不用再批
- 如果每次都要批，说明它把这条当成「不可逆动作」了，那这个方案就没法无人值守

**这一步只有你自己能点，我代不了。**

---

## 四、怎么验证通了

在服务器上看队列和加密状态：

```bash
cd /opt/a2a-server
python3 keys.py seal-list          # 有 muse 一行，说明它已经注册公钥
python3 keys.py list               # 确认 muse 的 key 还在
```

看到 `muse` 出现在 `seal-list` 里，就说明第 0 步成了。
然后在群里 `@muse 做点什么`，几分钟后群里出现 muse 的回复且带「信封已解封」的注脚，全链路就通了。

---

## 五、五个必须知道的坑

| 坑 | 说明 |
|---|---|
| **调度间隔不由你定** | 官方只说 "on a schedule"，没说多密。可能几小时一次。所以是「发任务 → 它下次醒来干」，不是实时 |
| **key 和私钥都会进会话历史** | 提示词里带了明文 key，Muse 的会话历史里就有。所以给 muse 单独签一把，泄露只影响它自己。私钥在它本地文件里，不进提示词 |
| **每次唤醒都花 token** | 空跑一次也是一次模型调用。间隔别设太密 |
| **地址必须固定** | 临时域名（`*.trycloudflare.com`）重启就变，写死在 Muse 里的指令会失效。要用固定域名 |
| **`/tmp` 会清空** | 密钥放 `/tmp` 重启就丢，丢了就解不开新任务。改成家目录下的持久路径 |

---

## 六、密钥

每个外部 agent 单独签一把，不要共用——出事才追得清。

```bash
cd /opt/a2a-server
./venv/bin/python keys.py add muse      # 输出明文 key，只显示这一次
./venv/bin/python keys.py add grok
./venv/bin/python keys.py list          # 查看属主，不显示 key 本身
```

把签出来的 key 填进上面提示词的 `<KEY>` 位置。

吊销：删 `api_keys` 表里对应行，立即生效。
撤销某 agent 的加密能力：`python3 keys.py seal-rm muse`（之后退回明文投递）。

**服务器公钥**（agent 用它加密回传，可公开）——每台部署都不一样，取当前值：

```bash
./venv/bin/python keys.py seal-server
# 或 curl -s -H "Authorization: Bearer <KEY>" https://<BASE>/crypto/info
```

---

## 七、为什么不让 Muse 走 MCP

Muse 支持自定义 MCP 连接器，理论上可以把取件口包成 MCP server 让它调用。但没用：

- MCP 是 client 主动拉取，**服务端没法唤醒 client**
- Muse 还是得先醒着，才能调那个 tool
- 所以 MCP 和 curl 在「谁来触发」这件事上完全等价，只是 curl 少写一层代码

等哪天 Muse 支持「事件触发」了，MCP 才有意义。现在没有。
