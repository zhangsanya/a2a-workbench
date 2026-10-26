"""外部 agent 邮箱（取件口）—— 让 Muse / Grok Bot 这类云端 agent 主动来领任务。

为什么需要它
------------
Muse 跑在 Meta 的 Secure VM 里：Tailscale 集成只接出站，shields-up 拒绝一切入站。
所以"从外面把任务推给它"这条路是死的。

但 Meta 的设计手记写得很清楚：
  "Once you have set a goal or a task, Muse continues to work on a schedule
   and in response to relevant events, taking the next step and following up."
它自己会定时醒来。醒来之后有终端、有浏览器、能写代码。

所以方向反过来：不在外面敲它的门，让它按时来取件。这个模块就是取件口。

三个动作，任何能发 HTTP 的东西都能实现
--------------------------------------
  GET  /mailbox/poll?agent=muse     领一个任务（原子认领 + 租约）
  POST /mailbox/result              交结果
  POST /mailbox/push                投任务（群里发消息时自动调用）

给 Muse 的定时任务提示词可以这么写：

  每 15 分钟访问一次
  http://<你的地址>/mailbox/poll?agent=muse
  （请求头 Authorization: Bearer <key>）
  如果返回的 task 是 null，本轮立刻结束，不要做任何事、不要回复我。
  如果有任务，按 task.text 执行，然后把结果 POST 到 /mailbox/result，
  body 为 {"id": "<task.id>", "ok": true, "result": "<你的产出>"}。

三个关键设计
------------
1. 租约：认领后 lease 秒内不交结果，任务自动回到 pending。
   agent 半路挂掉不会把任务永久锁死。
2. 原子认领：一条 UPDATE ... WHERE id = (SELECT ...) RETURNING，
   两个 poller 同时来也不会拿到同一条。
3. 空队列极简响应：定时唤醒的 agent 每次都要花 token 思考，
   "没活"这件事必须一眼可见，所以回一个 hint 让它立刻停。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

from fastapi import Request
from fastapi.responses import JSONResponse

import envelope
import models

SCHEMA = """
CREATE TABLE IF NOT EXISTS mailbox (
    id            TEXT PRIMARY KEY,
    agent         TEXT NOT NULL,
    text          TEXT NOT NULL,
    context_id    TEXT NOT NULL DEFAULT '',
    state         TEXT NOT NULL DEFAULT 'pending',
    lease_until   REAL NOT NULL DEFAULT 0,
    claimed_by    TEXT NOT NULL DEFAULT '',
    result        TEXT,
    error         TEXT,
    origin        TEXT NOT NULL DEFAULT '',
    notified      INTEGER NOT NULL DEFAULT 0,
    sealed        INTEGER NOT NULL DEFAULT 0,
    result_sealed INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mailbox_pick   ON mailbox(state, agent, created_at);
CREATE INDEX IF NOT EXISTS idx_mailbox_notify ON mailbox(notified, state);
"""

# 老库升级：SQLite 没有 ADD COLUMN IF NOT EXISTS，只能先查 pragma
MIGRATIONS = {
    "sealed": "ALTER TABLE mailbox ADD COLUMN sealed INTEGER NOT NULL DEFAULT 0",
    "result_sealed": "ALTER TABLE mailbox ADD COLUMN result_sealed INTEGER NOT NULL DEFAULT 0",
}


def migrate(conn) -> None:
    have = {row[1] for row in conn.execute("PRAGMA table_info(mailbox)")}
    for col, sql in MIGRATIONS.items():
        if col not in have:
            conn.execute(sql)

DEFAULT_EXTERNAL = "muse,grok"

_sink: Callable[[dict[str, Any]], None] | None = None


def external_agents() -> list[str]:
    raw = os.environ.get("A2A_EXTERNAL_AGENTS", DEFAULT_EXTERNAL)
    return [a.strip().lower() for a in raw.split(",") if a.strip()]


def set_sink(fn: Callable[[dict[str, Any]], None] | None) -> None:
    global _sink
    _sink = fn


# ---------------------------------------------------------------- 队列操作

def push(store, agent: str, text: str, context_id: str = "", origin: str = "workbench") -> dict[str, Any]:
    """投一封任务。正文在落库之前就封好，数据库里存的是密文。

    返回 {"id", "agent", "sealed", "pending"}。sealed=False 说明对方还没登记公钥，
    这条是明文投递，调用方有义务把这件事显式告诉用户。
    """
    task_id = models.new_id("mb-")
    now = time.time()
    payload, sealed = envelope.seal_for(store, agent, text)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO mailbox(id, agent, text, context_id, state, origin, sealed, created_at, updated_at)"
            " VALUES(?,?,?,?,'pending',?,?,?,?)",
            (task_id, agent.lower(), payload, context_id, origin, 1 if sealed else 0, now, now),
        )
    return {
        "id": task_id,
        "agent": agent.lower(),
        "sealed": sealed,
        "pending": pending_count(store, agent),
    }


def claim(store, agent: str, lease_seconds: int = 300) -> dict[str, Any] | None:
    """原子认领一条待办。没有待办返回 None。

    返回的 payload 字段按 sealed 区分：加密的放 envelope，明文的放 text。
    """
    now = time.time()
    who = agent.lower()
    with store._connect() as conn:
        row = conn.execute(
            """
            UPDATE mailbox
               SET state = 'claimed', claimed_by = ?, lease_until = ?, updated_at = ?
             WHERE id = (
                   SELECT id FROM mailbox
                    WHERE state = 'pending' AND (agent = ? OR ? = 'any')
                    ORDER BY created_at
                    LIMIT 1)
         RETURNING id, agent, text, context_id, sealed
            """,
            (who, now + lease_seconds, now, who, who),
        ).fetchone()
    if row is None:
        return None
    sealed = bool(row["sealed"])
    task: dict[str, Any] = {
        "id": row["id"],
        "agent": row["agent"],
        "context_id": row["context_id"],
        "lease_seconds": lease_seconds,
        "sealed": sealed,
    }
    if sealed:
        task["envelope"] = row["text"]
    else:
        task["text"] = row["text"]
    return task


def finish(store, task_id: str, ok: bool, payload: str, sealed: bool = False) -> tuple[bool, str]:
    """收结果。sealed=True 时 payload 是信封，先用服务器私钥拆开。

    返回 (是否写入成功, 错误说明)。拆封失败不算"任务不存在"，
    要单独区分开——agent 会看到是加密层的问题而不是 id 写错了。
    """
    if sealed:
        plain, err = envelope.try_open(store, payload)
        if err:
            return False, f"结果信封解不开：{err}"
        payload = plain
    now = time.time()
    with store._connect() as conn:
        cur = conn.execute(
            "UPDATE mailbox SET state=?, result=?, error=?, result_sealed=?, updated_at=?"
            " WHERE id=? AND state IN ('pending','claimed')",
            ("done" if ok else "failed",
             payload if ok else None,
             None if ok else payload,
             1 if sealed else 0,
             now, task_id),
        )
        if cur.rowcount <= 0:
            return False, "任务不存在或已关闭"
    return True, ""


def reap(store) -> int:
    """把超出租约的 claimed 任务放回 pending。"""
    now = time.time()
    with store._connect() as conn:
        cur = conn.execute(
            "UPDATE mailbox SET state='pending', claimed_by='', lease_until=0, updated_at=?"
            " WHERE state='claimed' AND lease_until > 0 AND lease_until < ?",
            (now, now),
        )
        return cur.rowcount


def pending_count(store, agent: str | None = None) -> int:
    with store._connect() as conn:
        if agent:
            row = conn.execute(
                "SELECT COUNT(*) FROM mailbox WHERE state='pending' AND agent=?",
                (agent.lower(),),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM mailbox WHERE state='pending'").fetchone()
    return int(row[0])


def recent(store, limit: int = 30) -> list[dict[str, Any]]:
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, agent, text, state, result, error, origin, sealed, result_sealed,"
            " created_at, updated_at"
            " FROM mailbox ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        # 密文对运维没用，只留长度，免得把一屏 base64 甩到界面上
        if item.get("sealed"):
            item["text"] = f"[已加密 {len(item['text'] or '')} 字节]"
        out.append(item)
    return out


def take_finished(store, limit: int = 20) -> list[dict[str, Any]]:
    """取走已完成但还没通知过的任务，并标记已通知（保证只通知一次）。

    必须一条 SQL 搞定。以前是先 SELECT 再 UPDATE：两条语句之间有个缝，
    另一个线程能读到同一批行，于是同一条结果在群里出现两遍。
    UPDATE ... RETURNING 是原子的，抢不到的行就轮不到你，天然去重。
    """
    with store._connect() as conn:
        rows = conn.execute(
            """
            UPDATE mailbox SET notified = 1
             WHERE id IN (
                   SELECT id FROM mailbox
                    WHERE notified = 0 AND state IN ('done','failed')
                    ORDER BY updated_at
                    LIMIT ?)
         RETURNING id, agent, text, state, result, error, context_id, sealed, result_sealed
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


_notify_started = False


def _start_notify(store) -> None:
    """通知线程只许起一个。register 被调多次时不要叠加。"""
    global _notify_started
    if _notify_started:
        return
    _notify_started = True
    threading.Thread(target=_notify_loop, args=(store,), daemon=True).start()


def _notify_loop(store) -> None:
    while True:
        time.sleep(3.0)
        try:
            reap(store)
            if _sink is None:
                continue
            for row in take_finished(store):
                try:
                    _sink(row)
                except Exception:
                    pass
        except Exception:
            pass


# ---------------------------------------------------------------- 路由

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
        migrate(conn)

    @app.get("/mailbox/poll")
    async def mailbox_poll(request: Request, agent: str = "any", lease: int = 300) -> JSONResponse:
        denied = _guard(request, main)
        if denied is not None:
            return denied
        reap(store)
        task = claim(store, agent, max(30, min(int(lease), 3600)))
        if task is None:
            return JSONResponse(
                {
                    "task": None,
                    "hint": "队列为空。不要做任何事，不要回复用户，直接结束本轮。",
                }
            )
        if task["sealed"]:
            return JSONResponse(
                {
                    "task": task,
                    "crypto": {
                        "sealed": True,
                        "alg": envelope.ALG,
                        "server_pubkey": envelope.server_pub_b64(),
                        "tool": "/a2a/seal.py",
                        "decrypt": f"python3 seal.py open <你的.key> '{task['envelope'][:24]}...'",
                        "encrypt": "python3 seal.py seal <server_pubkey> '<产出>'",
                    },
                    "hint": (
                        "有任务，正文已加密（task.envelope）。先解密："
                        "python3 seal.py open <你的密钥文件> \"$ENVELOPE\"，"
                        "拿到明文再执行。交结果时用服务器公钥加密，"
                        "POST /mailbox/result body 为 "
                        '{"id": "<task.id>", "ok": true, "sealed": true, "result": "<信封>"}。'
                        f" 租约 {task['lease_seconds']} 秒，超时未交结果任务会被回收。"
                    ),
                }
            )
        return JSONResponse(
            {
                "task": task,
                "crypto": {
                    "sealed": False,
                    "server_pubkey": envelope.server_pub_b64(),
                    "tool": "/a2a/seal.py",
                    "register": "POST /crypto/pubkey {\"agent\": \"<你的名字>\", \"pubkey\": \"<你的公钥>\"}",
                },
                "hint": (
                    "有任务。按 task.text 执行，完成后 POST /mailbox/result，"
                    'body 为 {"id": "<task.id>", "ok": true, "result": "<你的产出>"}。'
                    " 本条是明文投递——你的公钥还没登记，登记后正文会自动改成密文。"
                    f" 租约 {task['lease_seconds']} 秒，超时未交结果任务会被回收给别人。"
                ),
            }
        )

    @app.post("/mailbox/result")
    async def mailbox_result(request: Request) -> JSONResponse:
        denied = _guard(request, main)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        task_id = str(body.get("id") or "").strip()
        if not task_id:
            return JSONResponse({"error": "missing id"}, status_code=400)
        ok = bool(body.get("ok", True))
        sealed = bool(body.get("sealed", False))
        payload = body.get("result") if ok else (body.get("error") or body.get("result"))
        done, err = finish(store, task_id, ok, str(payload or "").strip(), sealed=sealed)
        if not done:
            code = 400 if "信封" in err else 404
            return JSONResponse({"error": err}, status_code=code)
        return JSONResponse({"ok": True})

    @app.post("/mailbox/push")
    async def mailbox_push(request: Request) -> JSONResponse:
        denied = _guard(request, main)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        agent = str(body.get("agent") or "").strip().lower()
        text = str(body.get("text") or "").strip()
        if not agent or not text:
            return JSONResponse({"error": "agent and text required"}, status_code=400)
        try:
            info = push(
                store, agent, text,
                str(body.get("context_id") or ""),
                str(body.get("origin") or "api"),
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse(info)

    @app.get("/mailbox/list")
    async def mailbox_list(request: Request, limit: int = 30) -> JSONResponse:
        denied = _guard(request, main)
        if denied is not None:
            return denied
        agents = external_agents()
        return JSONResponse(
            {
                "external_agents": agents,
                "sealed_agents": envelope.sealed_agents(store),
                "require_seal": envelope.require_seal(),
                "pending": {a: pending_count(store, a) for a in agents},
                "tasks": recent(store, max(1, min(int(limit), 200))),
            }
        )

    _start_notify(store)
