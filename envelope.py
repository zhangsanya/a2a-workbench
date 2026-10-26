"""信封层 —— 消息正文的端到端加密。

两层加密，各管一段
------------------
1. 传输层 TLS：保护"链路"。但 TLS 会在边缘终结——Cloudflare Tunnel 就是这样，
   密文到 CF 就解开了，CF 之后到本机是明文 HTTP。反代、WAF、日志都能看见正文。
2. 信封层（本模块）：保护"正文"。任务在离开本进程之前就被封进 X25519 密封盒，
   只有目标 agent 手里的私钥能开。边缘、代理、中间任何一跳都只看到一串 base64。

两层不重复，是叠加的：TLS 挡住链路上的旁听者，信封挡住链路上的"合法中间人"。

密钥分工
--------
  服务器          一对长期密钥  <DATA_DIR>/seal-server.key（0600）
                  公钥对外公布，agent 用它加密回传的结果
  每个外部 agent  各自一对密钥，由 `seal.py keygen <名字>` 在 agent 那侧生成
                  私钥永不外传，公钥注册到服务器（本模块的 agent_keys 表）

所以：
  - 群里发 "@muse 干活"，服务器用 muse 的公钥封起来 → muse 才能开
  - muse 交回结果，用服务器的公钥封起来 → 服务器才能开
  - 中间任何一跳两个都开不了

注册公钥的两种方式
------------------
  A. 命令行（运维）：python3 keys.py seal-add muse <公钥base64>
  B. agent 自助：POST /crypto/pubkey  {"agent":"muse","pubkey":"..."}
     只能给自己注册——属主名必须等于 agent 名，防止 A 顶替 B 收信。

没有公钥的 agent
----------------
仍然可以投递，但走明文，并且群聊里会明确提示"未配置公钥，明文投递"。
设 A2A_REQUIRE_SEAL=1 可以强制拒绝明文投递。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse

import seal

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_keys (
    agent      TEXT PRIMARY KEY,
    pubkey     TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

ALG = "x25519-hkdf-sha256-chacha20-hmac-sha256"


# ------------------------------------------------------------------ 服务器身份

def _data_dir() -> Path:
    return Path(os.environ.get("A2A_DATA_DIR", "./data")).resolve()


def _key_path() -> Path:
    """服务器密钥必须跟数据库待在同一个目录。

    这条不是洁癖：手工跑 keys.py 时如果工作目录和服务进程不一致，
    两边会各自生成一把密钥，于是 agent 用 A 加密回传、服务用 B 去解，
    永远解不开，而且报错看起来像"被篡改"。让密钥锚在 DB 旁边就没有这种可能。
    """
    override = os.environ.get("A2A_SEAL_KEY")
    if override:
        return Path(override).resolve()
    db = os.environ.get("A2A_DB")
    if db:
        return Path(db).resolve().parent / "seal-server.key"
    return _data_dir() / "seal-server.key"


def server_keys() -> tuple[bytes, bytes]:
    """返回 (私钥, 公钥)。首次调用时生成并落盘。"""
    path = _key_path()
    if path.exists():
        return seal.load_priv(str(path)), seal.pub_of(seal.load_priv(str(path)))
    path.parent.mkdir(parents=True, exist_ok=True)
    priv, pub = seal.keygen()
    seal.save_keyfile(str(path), "server", priv, pub)
    return priv, pub


def server_pub_b64() -> str:
    return seal.b64e(server_keys()[1])


# ------------------------------------------------------------------ 公钥登记

def set_pubkey(store, agent: str, pubkey: str) -> None:
    agent = agent.strip().lower()
    raw = seal.b64d(pubkey)
    if len(raw) != 32:
        raise ValueError("公钥必须是 32 字节（base64url 编码）")
    now = time.time()
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO agent_keys(agent, pubkey, created_at, updated_at) VALUES(?,?,?,?)"
            " ON CONFLICT(agent) DO UPDATE SET pubkey=excluded.pubkey, updated_at=excluded.updated_at",
            (agent, seal.b64e(raw), now, now),
        )


def del_pubkey(store, agent: str) -> bool:
    with store._connect() as conn:
        cur = conn.execute("DELETE FROM agent_keys WHERE agent=?", (agent.strip().lower(),))
        return cur.rowcount > 0


def pubkey_of(store, agent: str) -> str | None:
    with store._connect() as conn:
        row = conn.execute(
            "SELECT pubkey FROM agent_keys WHERE agent=?", (agent.strip().lower(),)
        ).fetchone()
    return row["pubkey"] if row else None


def sealed_agents(store) -> list[str]:
    with store._connect() as conn:
        rows = conn.execute("SELECT agent FROM agent_keys ORDER BY agent").fetchall()
    return [r["agent"] for r in rows]


def require_seal() -> bool:
    return os.environ.get("A2A_REQUIRE_SEAL", "").strip().lower() in {"1", "true", "yes", "on"}


# ------------------------------------------------------------------ 封装 / 拆封

def seal_for(store, agent: str, text: str) -> tuple[str, bool]:
    """把正文封给某个 agent。返回 (payload, 是否已加密)。"""
    pub = pubkey_of(store, agent)
    if pub is None:
        if require_seal():
            raise ValueError(f"{agent} 未注册公钥，而 A2A_REQUIRE_SEAL=1，拒绝明文投递")
        return text, False
    return seal.seal_text(pub, text), True


def open_from(store, blob: str) -> str:
    """拆开 agent 回传的信封。"""
    priv, _ = server_keys()
    return seal.unseal_text(priv, blob)


def try_open(store, blob: str) -> tuple[str, str]:
    """尽力拆封，返回 (明文, 错误)。两个都不为空说明拆失败。"""
    try:
        return open_from(store, blob), ""
    except Exception as exc:
        return "", str(exc)


# ------------------------------------------------------------------ 路由

def _guard(request: Request, main):
    try:
        main.require_owner(request)
        return None
    except main.RpcError as exc:
        return JSONResponse({"error": exc.message}, status_code=401)


def register(app, main) -> None:
    store = main.STORE
    with store._connect() as conn:
        conn.executescript(SCHEMA)

    # 首次启动就把服务器密钥准备好，免得第一条消息才发现写不了盘
    server_keys()

    @app.get("/crypto/info")
    async def crypto_info(request: Request) -> JSONResponse:
        denied = _guard(request, main)
        if denied is not None:
            return denied
        return JSONResponse(
            {
                "alg": ALG,
                "server_pubkey": server_pub_b64(),
                "sealed_agents": sealed_agents(store),
                "require_seal": require_seal(),
                "how": {
                    "decrypt": "python3 seal.py open <你的.key> '<envelope>'",
                    "encrypt": "python3 seal.py seal <server_pubkey> '<明文>'",
                    "keygen": "python3 seal.py keygen <你的名字>",
                    "download": "/a2a/seal.py",
                },
            }
        )

    @app.get("/a2a/seal.py")
    async def crypto_tool() -> PlainTextResponse:
        """把 seal.py 本体发出去，agent 那边不用装任何依赖。"""
        src = Path(__file__).resolve().parent / "seal.py"
        return PlainTextResponse(src.read_text(encoding="utf-8"), media_type="text/x-python")

    @app.post("/crypto/pubkey")
    async def crypto_pubkey(request: Request) -> JSONResponse:
        try:
            owner = main.require_owner(request)
        except main.RpcError as exc:
            return JSONResponse({"error": exc.message}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        agent = str(body.get("agent") or owner).strip().lower()
        pubkey = str(body.get("pubkey") or "").strip()
        if not pubkey:
            return JSONResponse({"error": "pubkey required"}, status_code=400)
        # 只能给自己登记，否则 A 可以用自己的公钥顶替 B 收信
        if agent != owner.strip().lower():
            return JSONResponse(
                {"error": f"属主 {owner} 不能给 {agent} 注册公钥"}, status_code=403
            )
        try:
            set_pubkey(store, agent, pubkey)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "agent": agent, "sealed": True})
