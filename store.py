"""SQLite 持久化层。

设计要点：
- WAL 模式，读不阻塞写；每次操作独立连接，避免 sqlite3 跨线程共享。
- task 是真持久化的，所以 GetTask / ListTasks 走标准路径即可，
  不需要像 Elastic Agent Builder 那样绕道会话存储去轮询。
- 所有查询都带 owner 过滤，落实"只返回调用者可见的任务"。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import models

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    context_id  TEXT NOT NULL,
    state       TEXT NOT NULL,
    status_text TEXT,
    metadata    TEXT NOT NULL DEFAULT '{}',
    owner       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_context ON tasks(context_id);
CREATE INDEX IF NOT EXISTS idx_tasks_agent   ON tasks(agent_id, created_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    context_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    parts      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id, created_at);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    name        TEXT,
    parts       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id, created_at);

CREATE TABLE IF NOT EXISTS api_keys (
    key_hash   TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------------------------------------------------------------- api keys

    def add_api_key(self, key: str, owner: str) -> None:
        digest = hashlib.sha256(key.encode()).hexdigest()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO api_keys (key_hash, owner, created_at) VALUES (?,?,?)",
                (digest, owner, models.now_iso()),
            )

    def owner_for_key(self, key: str) -> str | None:
        digest = hashlib.sha256(key.encode()).hexdigest()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT owner FROM api_keys WHERE key_hash=?", (digest,)
            ).fetchone()
        return row["owner"] if row else None

    def count_keys(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM api_keys").fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------- tasks

    def create_task(
        self,
        *,
        task_id: str,
        agent_id: str,
        context_id: str,
        owner: str,
        state: str = models.TASK_STATE_SUBMITTED,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = models.now_iso()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO tasks (task_id, agent_id, context_id, state, status_text,"
                " metadata, owner, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    agent_id,
                    context_id,
                    state,
                    None,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    owner,
                    now,
                    now,
                ),
            )

    def set_state(self, task_id: str, state: str, status_text: str | None = None) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state=?, status_text=COALESCE(?, status_text),"
                " updated_at=? WHERE task_id=?",
                (state, status_text, models.now_iso(), task_id),
            )

    def get_state(self, task_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return row["state"] if row else None

    def owner_of(self, task_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT owner FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return row["owner"] if row else None

    def list_tasks(
        self, *, agent_id: str | None, owner: str, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        sql = "SELECT task_id FROM tasks WHERE owner=?"
        args: list[Any] = [owner]
        if agent_id:
            sql += " AND agent_id=?"
            args.append(agent_id)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self.task_wire(r["task_id"], history_length=0) for r in rows]  # type: ignore[misc]

    # -------------------------------------------------------------- 子对象写入

    def append_message(
        self,
        *,
        task_id: str,
        context_id: str,
        role: str,
        parts: list[dict[str, Any]],
        message_id: str | None = None,
    ) -> str:
        mid = message_id or models.new_id()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO messages (message_id, task_id, context_id, role,"
                " parts, created_at) VALUES (?,?,?,?,?,?)",
                (
                    mid,
                    task_id,
                    context_id,
                    role,
                    json.dumps(parts, ensure_ascii=False),
                    models.now_iso(),
                ),
            )
        return mid

    def append_artifact(
        self, *, task_id: str, name: str | None, parts: list[dict[str, Any]]
    ) -> str:
        aid = models.new_id("art-")
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO artifacts (artifact_id, task_id, name, parts, created_at)"
                " VALUES (?,?,?,?,?)",
                (
                    aid,
                    task_id,
                    name,
                    json.dumps(parts, ensure_ascii=False),
                    models.now_iso(),
                ),
            )
        return aid

    # ------------------------------------------------------------------ 读出

    def task_wire(self, task_id: str, history_length: int | None = None) -> dict[str, Any] | None:
        """组装成线上格式。history_length=None 表示全量，0 表示不要历史。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            msg_rows = conn.execute(
                "SELECT message_id, role, parts FROM messages WHERE task_id=?"
                " ORDER BY created_at, rowid",
                (task_id,),
            ).fetchall()
            art_rows = conn.execute(
                "SELECT artifact_id, name, parts FROM artifacts WHERE task_id=?"
                " ORDER BY created_at, rowid",
                (task_id,),
            ).fetchall()

        if history_length is not None:
            msg_rows = msg_rows[-history_length:] if history_length > 0 else []

        status: dict[str, Any] = {
            "state": row["state"],
            "timestamp": row["updated_at"],
        }
        if row["status_text"]:
            status["message"] = models.message(
                models.ROLE_AGENT, [models.text_part(row["status_text"])]
            )

        return {
            "id": row["task_id"],
            "contextId": row["context_id"],
            "status": status,
            "history": [
                models.message(
                    m["role"],
                    json.loads(m["parts"]),
                    message_id=m["message_id"],
                    context_id=row["context_id"],
                    task_id=row["task_id"],
                )
                for m in msg_rows
            ],
            "artifacts": [
                {
                    "artifactId": a["artifact_id"],
                    "name": a["name"],
                    "parts": json.loads(a["parts"]),
                }
                for a in art_rows
            ],
            "metadata": json.loads(row["metadata"]),
        }
