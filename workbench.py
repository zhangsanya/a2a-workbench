"""群聊工作台 —— 浏览器打开就是一个群。

设计要点
--------
1. 群是 UI，任务才是工作单元。用户发一条消息，被拆成 N 个 A2A 任务并行派发。
2. 消息全部落 SQLite，前端用游标轮询（?since=N），断线重连不丢消息。
3. 不依赖任何 IM 平台，不需要第三方账号，数据全在本机。
4. 派发用线程池，多个 agent 同时跑；受 llama-server slot 数限制时表现为排队。

路由
----
GET  /                   群聊页面
GET  /chat/members       群成员（人 + agent）
GET  /chat/messages      拉取 since 之后的新消息
POST /chat/send          发一条消息，触发派发
POST /chat/cancel        取消某条消息派出的任务
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import mailbox

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

HTML_PATH = Path(__file__).parent / "static" / "workbench.html"

CHAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    room       TEXT NOT NULL DEFAULT 'main',
    sender     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    text       TEXT NOT NULL,
    task_id    TEXT,
    context_id TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_room ON chat_messages(room, id);
"""

TERMINAL = (
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
)

MENTION = re.compile(r"@(notes|report|qwen|all)\b", re.IGNORECASE)

EXT_LABEL = {"muse": "Meta Muse", "grok": "Grok Bot"}


def mention_pattern(main) -> re.Pattern[str]:
    """@ 名单 = 本地 agent + 外部 agent + all。外部 agent 走取件口。"""
    names = list(main.agents.AGENTS) + mailbox.external_agents() + ["all"]
    return re.compile(r"@(" + "|".join(re.escape(n) for n in names) + r")\b", re.IGNORECASE)

ROUTE_PROMPT = """你是任务路由器。把用户的一句话分派给合适的 agent。

可用 agent：
{catalog}

用户消息：{text}

只输出一行 JSON，不要任何解释或代码块标记：
{{"agents": ["id"], "reason": "不超过 15 字"}}

规则：
- 只选真正需要的，通常 1 个，最多 3 个
- 检索、查找、查资料、看笔记 → notes
- 统计、汇总、出报告 → report
- 推理、解释、生成、翻译、写作 → qwen
- 完全无法判断时选 qwen
"""


# ---------------------------------------------------------------- 消息存储

def add_msg(
    store,
    sender: str,
    kind: str,
    text: str,
    task_id: str | None = None,
    context_id: str | None = None,
) -> int:
    with store._connect() as conn:
        cur = conn.execute(
            "INSERT INTO chat_messages(sender, kind, text, task_id, context_id, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (sender, kind, text, task_id, context_id, time.time()),
        )
        return int(cur.lastrowid or 0)


def fetch_msgs(store, since: int = 0, limit: int = 300) -> list[dict[str, Any]]:
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, sender, kind, text, task_id, context_id, created_at"
            " FROM chat_messages WHERE id > ? ORDER BY id LIMIT ?",
            (since, limit),
        ).fetchall()
    keys = ("id", "sender", "kind", "text", "task_id", "context_id", "created_at")
    return [dict(zip(keys, row)) for row in rows]


# ---------------------------------------------------------------- 路由决策

def route_by_rule(text: str) -> list[str]:
    t = text.lower()
    if any(k in t for k in ("报告", "汇总", "统计", "report", "总结")):
        return ["report"]
    if any(k in t for k in ("笔记", "检索", "查一下", "查找", "搜索", "notes", "文档")):
        return ["notes"]
    return ["qwen"]


def route_by_llm(main, text: str) -> tuple[list[str], str]:
    catalog = "\n".join(
        f"- {a.agent_id}: {a.description}" for a in main.agents.AGENTS.values()
    )
    prompt = ROUTE_PROMPT.format(catalog=catalog, text=text)
    payload = {
        "model": main.agents.LLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 160,
        "stream": False,
    }
    req = urllib.request.Request(
        main.agents.LLAMA_URL,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = json.loads(resp.read().decode())["choices"][0]["message"]["content"]
    except Exception:
        return route_by_rule(text), "规则"

    match = re.search(r"\{.*?\}", raw, re.S)
    if not match:
        return route_by_rule(text), "规则"
    try:
        picked = json.loads(match.group(0))
        ids = [i for i in picked.get("agents", []) if i in main.agents.AGENTS]
    except Exception:
        return route_by_rule(text), "规则"
    if not ids:
        return route_by_rule(text), "规则"
    return ids[:3], str(picked.get("reason") or "")[:40]


# ---------------------------------------------------------------- 派发执行

def extract_text(wire: dict[str, Any] | None) -> str:
    if not wire:
        return "（无结果）"
    chunks: list[str] = []
    for art in wire.get("artifacts") or []:
        for part in art.get("parts") or []:
            if part.get("text"):
                chunks.append(part["text"])
    if not chunks:
        status = (wire.get("status") or {}).get("message") or {}
        for part in status.get("parts") or []:
            if part.get("text"):
                chunks.append(part["text"])
    return "\n\n".join(chunks).strip() or "（无结果）"


def run_one(main, store, agent_id: str, text: str, context_id: str) -> None:
    spec = main.agents.AGENTS[agent_id]
    label = f"@{agent_id}"
    add_msg(store, agent_id, "status", f"{label} 接单", context_id=context_id)

    try:
        task_id = main.submit_task(
            spec,
            context_id=context_id,
            text=text,
            owner="workbench",
            metadata={"source": "workbench"},
        )
    except Exception as exc:  # noqa: BLE001
        add_msg(store, agent_id, "error", f"{label} 派发失败：{type(exc).__name__}: {exc}",
                context_id=context_id)
        return

    deadline = time.time() + 1800
    last_note = ""
    state = ""
    while time.time() < deadline:
        state = main.STORE.get_state(task_id) or ""
        wire = main.STORE.task_wire(task_id)
        note = ""
        if wire:
            msg = (wire.get("status") or {}).get("message") or {}
            note = " ".join(p.get("text", "") for p in msg.get("parts") or []).strip()
        if note and note != last_note and not state.startswith(TERMINAL):
            last_note = note
            add_msg(store, agent_id, "status", f"{label} {note}",
                    task_id=task_id, context_id=context_id)
        if state.startswith(TERMINAL):
            break
        time.sleep(1.2)
    else:
        add_msg(store, agent_id, "error", f"{label} 超时未完成", task_id=task_id,
                context_id=context_id)
        return

    wire = main.STORE.task_wire(task_id)
    body = extract_text(wire)
    if state.startswith("TASK_STATE_COMPLETED"):
        add_msg(store, agent_id, "agent", body, task_id=task_id, context_id=context_id)
    else:
        add_msg(store, agent_id, "error", f"{label} 失败（{state.split('_')[-1]}）\n\n{body}",
                task_id=task_id, context_id=context_id)


def dispatch_round(main, store, text: str, context_id: str, chosen: list[str], reason: str) -> None:
    if not chosen:
        return
    if len(chosen) > 1:
        add_msg(store, "system", "system",
                f"派给 {', '.join('@' + c for c in chosen)}" + (f"（{reason}）" if reason else ""),
                context_id=context_id)
    with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
        list(pool.map(lambda a: run_one(main, store, a, text, context_id), chosen))


# ---------------------------------------------------------------- 注册路由

def register(app, main) -> None:
    store = main.STORE
    with store._connect() as conn:
        conn.executescript(CHAT_SCHEMA)

    mention = mention_pattern(main)

    def _on_mailbox_result(row: dict[str, Any]) -> None:
        """外部 agent 交回的结果，回灌到群里。"""
        ok = row.get("state") == "done"
        body = (row.get("result") if ok else row.get("error")) or "（无内容）"
        if row.get("result_sealed"):
            body = f"{body}\n\n> 信封已解封（{row.get('agent')} 用服务器公钥加密交回）"
        add_msg(
            store,
            str(row.get("agent") or "agent"),
            "agent" if ok else "error",
            str(body),
            context_id=row.get("context_id") or None,
        )

    mailbox.set_sink(_on_mailbox_result)

    @app.get("/", response_class=HTMLResponse)
    async def workbench_page() -> HTMLResponse:
        return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))

    @app.get("/chat/members")
    async def chat_members() -> JSONResponse:
        members = [{"id": "owner", "name": "我", "kind": "human", "desc": "发任务的人"}]
        members += [
            {
                "id": a.agent_id,
                "name": a.name,
                "kind": "agent",
                "desc": a.description,
                "heavy": a.heavy,
            }
            for a in main.agents.AGENTS.values()
        ]
        sealed = set(mailbox.envelope.sealed_agents(store))
        members += [
            {
                "id": ext,
                "name": EXT_LABEL.get(ext, ext),
                "kind": "external",
                "desc": (
                    "外部 agent · 定时来取件口领任务 · 信封加密"
                    if ext in sealed
                    else "外部 agent · 定时来取件口领任务 · 未配公钥（明文）"
                ),
                "sealed": ext in sealed,
            }
            for ext in mailbox.external_agents()
        ]
        return JSONResponse({"members": members, "sealed_agents": sorted(sealed)})

    @app.get("/chat/messages")
    async def chat_messages(since: int = 0, limit: int = 300) -> JSONResponse:
        return JSONResponse({"messages": fetch_msgs(store, since, limit), "now": time.time()})

    @app.post("/chat/clear")
    async def chat_clear() -> JSONResponse:
        with store._connect() as conn:
            conn.execute("DELETE FROM chat_messages")
        return JSONResponse({"ok": True})

    @app.post("/chat/send")
    async def chat_send(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        text = str(body.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "empty"}, status_code=400)
        if len(text) > 8000:
            return JSONResponse({"error": "too long"}, status_code=413)

        context_id = main.models.new_id("ctx-")
        add_msg(store, "owner", "user", text, context_id=context_id)

        # 剥掉 @mention 再交给 agent，否则会被当成检索关键词
        clean = mention.sub("", text).strip() or text

        mentioned = {m.lower() for m in mention.findall(text)}
        externals = set(mailbox.external_agents())
        external_hits: set[str] = set()

        if "all" in mentioned:
            chosen = list(main.agents.AGENTS)
            external_hits = set(externals)
            reason = "全量广播"
        elif mentioned:
            chosen = [a for a in main.agents.AGENTS if a in mentioned]
            external_hits = mentioned & externals
            reason = "@ 指定"
        else:
            chosen, reason = route_by_llm(main, text)
            if len(chosen) > 1:
                add_msg(store, "system", "system", f"判断：{reason}", context_id=context_id)

        for ext in sorted(external_hits):
            info = mailbox.push(store, ext, clean, context_id=context_id, origin="workbench")
            if info["sealed"]:
                note = f"已封好投进 @{ext} 的取件口（信封加密，只有它能开），等它下次醒来领走"
            else:
                note = (
                    f"已投进 @{ext} 的取件口，但**这条是明文**——"
                    f"{ext} 还没登记公钥，登记后自动转密文"
                )
            add_msg(store, "system", "system", note, context_id=context_id)

        if chosen:
            threading.Thread(
                target=dispatch_round,
                args=(main, store, clean, context_id, chosen, reason),
                daemon=True,
            ).start()

        return JSONResponse(
            {
                "context_id": context_id,
                "agents": chosen,
                "external": sorted(external_hits),
                "reason": reason,
            }
        )
