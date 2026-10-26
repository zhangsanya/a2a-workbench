"""A2A v1.0 最小服务端。

端点：
    GET  /agents                                 列出全部 Agent Card
    GET  /.well-known/agent-card.json            默认 Agent 的 Card（v1.0 探测路径）
    GET  /agents/{agent_id}/.well-known/agent-card.json
    POST /a2a/{agent_id}                         JSON-RPC 2.0 入口
    GET  /healthz

实现的方法：SendMessage / SendStreamingMessage(SSE) / GetTask / ListTasks /
CancelTask / SubscribeToTask / GetExtendedAgentCard

只声明 JSONRPC 一种绑定。声明了就必须真能连——等 REST 绑定写完再往
Agent Card 里加 HTTP+JSON。
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

import agents
import conf  # noqa: F401  先装载 /etc/a2a-server.env，下面几行才读得到
import models
from store import Store

DATA_DIR = Path(os.environ.get("A2A_DATA_DIR", "./data")).resolve()
DB_PATH = Path(os.environ.get("A2A_DB", DATA_DIR / "a2a.db"))
BASE_URL = os.environ.get("A2A_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
BLOCKING_TIMEOUT = float(os.environ.get("A2A_BLOCKING_TIMEOUT", "25"))
DEFAULT_AGENT = os.environ.get("A2A_DEFAULT_AGENT", "notes")

STORE = Store(DB_PATH)
EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="a2a-worker")
CANCELS: dict[str, threading.Event] = {}

app = FastAPI(title="A2A Server", version="1.0.0")


# --------------------------------------------------------------------- 鉴权


def resolve_owner(authorization: str | None) -> str | None:
    """Bearer token -> 调用方身份。

    身份只认 API key 的属主，不认消息 metadata 里自报的名字——
    自报字段可以伪造，审计不能建立在它上面。
    """
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return STORE.owner_for_key(token.strip())


def require_owner(request: Request) -> str:
    owner = resolve_owner(request.headers.get("authorization"))
    if owner is None:
        raise RpcError(-32001, "unauthorized: 缺少或无效的 Bearer token")
    return owner


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# ----------------------------------------------------------------- Agent Card


def build_card(agent_id: str) -> dict[str, Any]:
    spec = agents.AGENTS[agent_id]
    return {
        "name": spec.name,
        "description": spec.description,
        "version": "1.0.0",
        "provider": {"organization": "local"},
        "supportedInterfaces": [
            {
                "url": f"{BASE_URL}/a2a/{agent_id}",
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "extendedAgentCard": True,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain", "text/markdown"],
        "skills": spec.skills,
    }


@app.get("/agents")
async def list_agents(request: Request) -> JSONResponse:
    try:
        require_owner(request)
    except RpcError as exc:
        return JSONResponse({"error": exc.message}, status_code=401)
    return JSONResponse(
        {"agents": [{"id": aid, "card": build_card(aid)} for aid in agents.AGENTS]}
    )


@app.get("/.well-known/agent-card.json")
async def well_known_card() -> JSONResponse:
    if DEFAULT_AGENT not in agents.AGENTS:
        return JSONResponse(
            {"error": "no default agent", "available": list(agents.AGENTS)}, status_code=404
        )
    return JSONResponse(build_card(DEFAULT_AGENT), media_type="application/a2a+json")


@app.get("/agents/{agent_id}/.well-known/agent-card.json")
async def agent_card(agent_id: str) -> JSONResponse:
    if agent_id not in agents.AGENTS:
        return JSONResponse({"error": "unknown agent"}, status_code=404)
    return JSONResponse(build_card(agent_id), media_type="application/a2a+json")


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True, "agents": list(agents.AGENTS)})


# ---------------------------------------------------------------- 任务执行


def run_agent(
    spec: agents.AgentSpec,
    *,
    task_id: str,
    context_id: str,
    text: str,
    cancel: threading.Event,
) -> None:
    STORE.set_state(task_id, models.TASK_STATE_WORKING, "已接收，开始处理")
    ctx = agents.AgentContext(
        task_id=task_id,
        context_id=context_id,
        text=text,
        data_dir=DATA_DIR,
        _report=lambda msg: STORE.set_state(task_id, models.TASK_STATE_WORKING, msg),
        _cancel=cancel,
    )
    try:
        artifacts = spec.handler(ctx)
    except agents.TaskCancelled:
        STORE.set_state(task_id, models.TASK_STATE_CANCELED, "已取消")
        return
    except Exception as exc:  # noqa: BLE001
        STORE.set_state(
            task_id, models.TASK_STATE_FAILED, f"{type(exc).__name__}: {exc}"
        )
        return
    finally:
        CANCELS.pop(task_id, None)

    if cancel.is_set():
        STORE.set_state(task_id, models.TASK_STATE_CANCELED, "已取消")
        return

    for art in artifacts:
        STORE.append_artifact(task_id=task_id, name=art.get("name"), parts=art["parts"])

    STORE.append_message(
        task_id=task_id,
        context_id=context_id,
        role=models.ROLE_AGENT,
        parts=[models.text_part(f"已产出 {len(artifacts)} 个产物")],
    )
    STORE.set_state(task_id, models.TASK_STATE_COMPLETED, "完成")


def submit_task(
    spec: agents.AgentSpec, *, context_id: str, text: str, owner: str, metadata: dict | None
) -> str:
    task_id = models.new_id("task-")
    STORE.create_task(
        task_id=task_id,
        agent_id=spec.agent_id,
        context_id=context_id,
        owner=owner,
        metadata=metadata,
    )
    STORE.append_message(
        task_id=task_id,
        context_id=context_id,
        role=models.ROLE_USER,
        parts=[models.text_part(text)],
    )
    cancel = threading.Event()
    CANCELS[task_id] = cancel
    EXECUTOR.submit(
        run_agent,
        spec,
        task_id=task_id,
        context_id=context_id,
        text=text,
        cancel=cancel,
    )
    return task_id


# -------------------------------------------------------------- JSON-RPC 方法


def rpc_send_message(agent_id: str, params: dict[str, Any], owner: str) -> dict[str, Any]:
    spec = agents.AGENTS.get(agent_id)
    if spec is None:
        raise RpcError(-32602, f"unknown agent: {agent_id}")

    msg = params.get("message")
    if not isinstance(msg, dict):
        raise RpcError(-32602, "params.message 缺失")

    text = models.extract_text(msg)
    # contextId 允许客户端预先生成，服务端照单全收——这是断线恢复的关键
    context_id = msg.get("contextId") or models.new_id("ctx-")
    task_id = submit_task(
        spec,
        context_id=context_id,
        text=text,
        owner=owner,
        metadata=params.get("metadata"),
    )

    config = params.get("configuration") or {}
    if not config.get("returnImmediately", False):
        deadline = time.time() + BLOCKING_TIMEOUT
        while time.time() < deadline:
            state = STORE.get_state(task_id)
            if state in models.SETTLED_STATES:
                break
            time.sleep(0.25)

    return {"task": STORE.task_wire(task_id)}


def rpc_get_task(params: dict[str, Any], owner: str) -> dict[str, Any]:
    task_id = params.get("id") or params.get("taskId")
    if not task_id:
        raise RpcError(-32602, "params.id 缺失")
    if STORE.owner_of(task_id) != owner:
        raise RpcError(-32001, "task not found")
    history_length = params.get("historyLength")
    wire = STORE.task_wire(
        task_id, history_length=int(history_length) if history_length is not None else None
    )
    if wire is None:
        raise RpcError(-32602, "task not found")
    return {"task": wire}


def rpc_list_tasks(agent_id: str, params: dict[str, Any], owner: str) -> dict[str, Any]:
    page_size = int(params.get("pageSize") or 20)
    offset = int(params.get("pageToken") or 0)
    rows = STORE.list_tasks(
        agent_id=agent_id, owner=owner, limit=page_size, offset=offset
    )
    next_token = str(offset + page_size) if len(rows) == page_size else None
    result: dict[str, Any] = {"tasks": rows}
    if next_token:
        result["nextPageToken"] = next_token
    return result


def rpc_cancel_task(params: dict[str, Any], owner: str) -> dict[str, Any]:
    task_id = params.get("id") or params.get("taskId")
    if not task_id:
        raise RpcError(-32602, "params.id 缺失")
    if STORE.owner_of(task_id) != owner:
        raise RpcError(-32001, "task not found")
    state = STORE.get_state(task_id)
    if state in models.TERMINAL_STATES:
        return {"task": STORE.task_wire(task_id)}
    event = CANCELS.get(task_id)
    if event is not None:
        event.set()
    STORE.set_state(task_id, models.TASK_STATE_CANCELED, "已取消")
    return {"task": STORE.task_wire(task_id)}


def dispatch(agent_id: str, method: str, params: dict[str, Any], owner: str) -> Any:
    if method in ("SendMessage", "message/send"):
        return rpc_send_message(agent_id, params, owner)
    if method in ("GetTask", "tasks/get"):
        return rpc_get_task(params, owner)
    if method in ("ListTasks", "tasks/list"):
        return rpc_list_tasks(agent_id, params, owner)
    if method in ("CancelTask", "tasks/cancel"):
        return rpc_cancel_task(params, owner)
    if method in ("GetExtendedAgentCard", "agent/getAuthenticatedExtendedCard"):
        if agent_id not in agents.AGENTS:
            raise RpcError(-32602, f"unknown agent: {agent_id}")
        return {"agentCard": build_card(agent_id)}
    raise RpcError(-32601, f"method not found: {method}")


# ------------------------------------------------------------------ 入口


@app.post("/a2a/{agent_id}")
async def a2a_endpoint(agent_id: str, request: Request):
    if request.headers.get("A2A-Version", "1.0") not in ("1.0", "1.0.0"):
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32001, "message": "version not supported"},
            },
            status_code=400,
        )

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}},
            status_code=400,
        )

    rpc_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}

    try:
        owner = require_owner(request)
    except RpcError as exc:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": exc.code, "message": exc.message}},
            status_code=401,
        )

    # 流式方法需要直接返回 SSE，不包 JSON 响应
    if method in ("SubscribeToTask", "tasks/resubscribe"):
        task_id = params.get("id") or params.get("taskId")
        if not task_id or STORE.owner_of(task_id) != owner:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32602, "message": "task not found"},
                },
                status_code=404,
            )
        return StreamingResponse(
            _sse_task(rpc_id, task_id), media_type="text/event-stream"
        )

    try:
        result = await asyncio.to_thread(dispatch, agent_id, method, params, owner)
    except RpcError as exc:
        error: dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.data is not None:
            error["data"] = exc.data
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": error})

    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})


async def _sse_task(rpc_id: Any, task_id: str):
    """按秒轮询数据库，状态变了就推一帧，终态后关闭流。

    这是最朴素但最稳的实现：任务状态在库里，推流只是读取，
    客户端断线重连用 SubscribeToTask 即可，不会丢活。
    """
    last: str | None = None
    while True:
        wire = STORE.task_wire(task_id)
        if wire is None:
            yield f"data: {json.dumps({'error': 'task not found'})}\n\n"
            return
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": wire}},
            ensure_ascii=False,
        )
        if payload != last:
            yield f"data: {payload}\n\n"
            last = payload
        if wire["status"]["state"] in models.TERMINAL_STATES:
            return
        await asyncio.sleep(1.0)


@app.on_event("startup")
def bootstrap_key() -> None:
    """首次启动时用环境变量里的 key 建一条，避免空库无人可进。"""
    key = os.environ.get("A2A_BOOTSTRAP_KEY")
    if key and STORE.count_keys() == 0:
        STORE.add_api_key(key, os.environ.get("A2A_BOOTSTRAP_OWNER", "local-admin"))


# ---- 群聊工作台（挂在最后，避免影响既有 A2A 路由）----
import sys as _sys

# 信封层必须排在 mailbox 前面：mailbox 投递时要拿公钥封装
import envelope as _envelope

_envelope.register(app, _sys.modules[__name__])

import mailbox as _mailbox

_mailbox.register(app, _sys.modules[__name__])

import workbench as _workbench

_workbench.register(app, _sys.modules[__name__])
