"""Agent 注册表与本地实现。

三个 Agent：
- notes  本地 Markdown 笔记检索
- report 笔记汇总报告（刻意做慢，用于验证长任务与断线）
- qwen   调用本机 llama.cpp 上的 Qwen3.8（OpenAI 兼容接口）

想接自己的业务，在 AGENTS 里加一条 AgentSpec 并写一个 handler 即可。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import models

LLAMA_URL = os.environ.get(
    "A2A_LLAMA_URL", "http://127.0.0.1:8001/v1/chat/completions"
)
LLAMA_MODEL = os.environ.get(
    "A2A_LLAMA_MODEL", "Qwen3.8-27B-Uncensored-Q4_K_M.gguf"
)


@dataclass
class AgentContext:
    task_id: str
    context_id: str
    text: str
    data_dir: Path
    metadata: dict[str, Any] = field(default_factory=dict)
    _report: Callable[[str], None] | None = field(default=None, repr=False)
    _cancel: threading.Event | None = field(default=None, repr=False)

    def report(self, text: str) -> None:
        if self._report is not None:
            self._report(text)

    @property
    def cancelled(self) -> bool:
        return bool(self._cancel and self._cancel.is_set())

    def check_cancel(self) -> None:
        if self.cancelled:
            raise TaskCancelled()


class TaskCancelled(Exception):
    pass


def notes_dir(data_dir: Path) -> Path:
    directory = data_dir / "notes"
    directory.mkdir(parents=True, exist_ok=True)
    if not any(directory.glob("*.md")):
        (directory / "welcome.md").write_text(
            "# 欢迎\n\n这是本地笔记目录。放 Markdown 文件进来，就会被 notes agent 检索到。\n",
            encoding="utf-8",
        )
        (directory / "a2a.md").write_text(
            "# A2A 备忘\n\n"
            "- 协作单位是任务，不是消息\n"
            "- agent 之间保持不透明，只交换任务和产物\n"
            "- contextId 可以由客户端预先生成\n",
            encoding="utf-8",
        )
    return directory


def handle_notes(ctx: AgentContext) -> list[dict[str, Any]]:
    directory = notes_dir(ctx.data_dir)
    terms = [t for t in ctx.text.replace("，", " ").replace(",", " ").split() if t]
    if not terms:
        return [
            {
                "name": "hint.md",
                "parts": [
                    models.text_part("请给我一个关键词，例如：A2A 上下文", "text/markdown")
                ],
            }
        ]

    hits: list[tuple[str, int, str]] = []
    for path in sorted(directory.glob("*.md")):
        ctx.check_cancel()
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            lowered = line.lower()
            if any(term.lower() in lowered for term in terms):
                hits.append((path.name, lineno, line.strip()))

    ctx.report(f"扫描 {len(list(directory.glob('*.md')))} 个文件，命中 {len(hits)} 行")

    if not hits:
        body = f"没有找到包含 {terms} 的内容。"
    else:
        lines = [f"共 {len(hits)} 处命中：", ""]
        lines += [f"- `{name}:{lineno}` {text}" for name, lineno, text in hits[:40]]
        body = "\n".join(lines)

    return [{"name": "search-result.md", "parts": [models.text_part(body, "text/markdown")]}]


def handle_report(ctx: AgentContext) -> list[dict[str, Any]]:
    directory = notes_dir(ctx.data_dir)
    files = sorted(directory.glob("*.md"))

    ctx.report("阶段 1/3：扫描本地笔记目录")
    time.sleep(2)
    ctx.check_cancel()

    ctx.report(f"阶段 2/3：统计 {len(files)} 个文件")
    rows: list[str] = []
    total_chars = 0
    for path in files:
        time.sleep(1)
        ctx.check_cancel()
        content = path.read_text(encoding="utf-8")
        total_chars += len(content)
        rows.append(f"| {path.name} | {len(content.splitlines())} | {len(content)} |")

    ctx.report("阶段 3/3：生成报告")
    time.sleep(2)
    ctx.check_cancel()

    body = "\n".join(
        [
            "# 本地笔记报告",
            "",
            f"- 文件数：{len(files)}",
            f"- 总字符数：{total_chars}",
            f"- 生成任务：`{ctx.task_id}`",
            "",
            "| 文件 | 行数 | 字符数 |",
            "|---|---|---|",
            *rows,
        ]
    )
    return [{"name": "report.md", "parts": [models.text_part(body, "text/markdown")]}]


def handle_qwen(ctx: AgentContext) -> list[dict[str, Any]]:
    """把任务转给本机 llama.cpp 上的 Qwen3.8。"""
    ctx.report(f"调用本地模型 {LLAMA_MODEL}")
    payload = {
        "model": LLAMA_MODEL,
        "messages": [{"role": "user", "content": ctx.text}],
        "temperature": 0.7,
        "stream": False,
    }
    request = urllib.request.Request(
        LLAMA_URL,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        ctx.report(f"模型调用失败：{type(exc).__name__}")
        raise

    answer = data["choices"][0]["message"]["content"]
    usage = data.get("usage") or {}
    footer = ""
    if usage:
        footer = (
            f"\n\n---\n*{usage.get('prompt_tokens', '?')} in / "
            f"{usage.get('completion_tokens', '?')} out*"
        )
    return [
        {
            "name": "answer.md",
            "parts": [models.text_part(answer + footer, "text/markdown")],
        }
    ]


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    name: str
    description: str
    skills: list[dict[str, Any]]
    handler: Callable[[AgentContext], list[dict[str, Any]]]
    heavy: bool = False


AGENTS: dict[str, AgentSpec] = {
    "notes": AgentSpec(
        agent_id="notes",
        name="本地笔记检索",
        description="在服务器 /mnt/data/a2a-server/notes 的 Markdown 文件里做关键词检索。",
        skills=[
            {
                "id": "search-notes",
                "name": "检索笔记",
                "description": "输入关键词，返回命中行及文件名与行号。",
                "tags": ["search", "markdown", "local"],
                "examples": ["A2A 上下文", "不透明"],
            }
        ],
        handler=handle_notes,
        heavy=False,
    ),
    "report": AgentSpec(
        agent_id="report",
        name="笔记汇总报告",
        description="统计本地笔记目录，生成 Markdown 报告。刻意做慢，用于验证长任务与断线恢复。",
        skills=[
            {
                "id": "summarize-notes",
                "name": "汇总笔记",
                "description": "扫描全部 Markdown，输出文件级统计报告。",
                "tags": ["report", "markdown", "long-running"],
                "examples": ["给我一份笔记报告"],
            }
        ],
        handler=handle_report,
        heavy=True,
    ),
    "qwen": AgentSpec(
        agent_id="qwen",
        name="本地 Qwen3.8",
        description="把任务转给本机 llama.cpp 上的 Qwen3.8-27B，返回模型回答。",
        skills=[
            {
                "id": "chat",
                "name": "对话与推理",
                "description": "单轮问答，由本地 27B 模型生成回答。",
                "tags": ["llm", "local", "chat"],
                "examples": ["用一句话解释 A2A 协议"],
            }
        ],
        handler=handle_qwen,
        heavy=True,
    ),
}
