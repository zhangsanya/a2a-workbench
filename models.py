"""A2A v1.0 数据对象的构造工具。

只做三件事：
1. 生成规范的 ID 与时间戳
2. 构造 Message / Part / Task / Status 结构（线上格式用 camelCase）
3. 集中定义任务状态常量与终态集合

刻意不做严格校验：A2A 有扩展机制，入站载荷里多出来的字段不应该导致 422。
"""
from __future__ import annotations

import time
import uuid
from typing import Any

ROLE_USER = "ROLE_USER"
ROLE_AGENT = "ROLE_AGENT"

TASK_STATE_SUBMITTED = "TASK_STATE_SUBMITTED"
TASK_STATE_WORKING = "TASK_STATE_WORKING"
TASK_STATE_INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
TASK_STATE_AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"
TASK_STATE_COMPLETED = "TASK_STATE_COMPLETED"
TASK_STATE_FAILED = "TASK_STATE_FAILED"
TASK_STATE_CANCELED = "TASK_STATE_CANCELED"
TASK_STATE_REJECTED = "TASK_STATE_REJECTED"

TERMINAL_STATES = frozenset(
    {
        TASK_STATE_COMPLETED,
        TASK_STATE_FAILED,
        TASK_STATE_CANCELED,
        TASK_STATE_REJECTED,
    }
)
INTERRUPTED_STATES = frozenset({TASK_STATE_INPUT_REQUIRED, TASK_STATE_AUTH_REQUIRED})
SETTLED_STATES = TERMINAL_STATES | INTERRUPTED_STATES


def new_id(prefix: str = "") -> str:
    value = uuid.uuid4().hex
    return f"{prefix}{value}" if prefix else value


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def text_part(text: str, media_type: str = "text/plain") -> dict[str, Any]:
    return {"text": text, "mediaType": media_type}


def data_part(data: Any) -> dict[str, Any]:
    return {"data": data, "mediaType": "application/json"}


def url_part(url: str, media_type: str = "application/octet-stream") -> dict[str, Any]:
    return {"url": url, "mediaType": media_type}


def message(
    role: str,
    parts: list[dict[str, Any]],
    *,
    message_id: str | None = None,
    context_id: str | None = None,
    task_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "messageId": message_id or new_id(),
        "role": role,
        "parts": parts,
    }
    if context_id:
        payload["contextId"] = context_id
    if task_id:
        payload["taskId"] = task_id
    if metadata:
        payload["metadata"] = metadata
    return payload


def extract_text(msg: dict[str, Any]) -> str:
    """把一条 Message 里的所有文本 Part 拼起来，作为交给 agent 的输入。"""
    chunks: list[str] = []
    for part in msg.get("parts") or []:
        if isinstance(part, dict):
            if isinstance(part.get("text"), str):
                chunks.append(part["text"])
            elif part.get("data") is not None:
                chunks.append(str(part["data"]))
    return "\n".join(chunks).strip()
