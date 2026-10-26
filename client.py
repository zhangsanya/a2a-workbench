"""零依赖 A2A 客户端，用来验证服务端。

只用标准库：给 AI 框架沙箱用的话，装不了第三方包是常态。

环境变量：
    A2A_BASE   服务地址，默认 http://127.0.0.1:8080
    A2A_KEY    Bearer token
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

BASE = os.environ.get("A2A_BASE", "http://127.0.0.1:8080").rstrip("/")
KEY = os.environ.get("A2A_KEY", "")


def request(
    method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 180
):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("A2A-Version", "1.0")
    if KEY:
        req.add_header("Authorization", f"Bearer {KEY}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()
        print(f"HTTP {exc.code}: {detail}", file=sys.stderr)
        raise SystemExit(1)
    except urllib.error.URLError as exc:
        print(f"连接失败：{exc.reason}", file=sys.stderr)
        raise SystemExit(1)


def rpc(agent: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    resp = request("POST", f"/a2a/{agent}", payload)
    if "error" in resp:
        print(f"RPC 错误 {resp['error']}", file=sys.stderr)
        raise SystemExit(1)
    return resp["result"]


def show_task(task: dict[str, Any], *, with_history: bool = False) -> None:
    status = task.get("status") or {}
    print(f"task {task['id']}  context {task.get('contextId')}")
    print(f"状态 {status.get('state')}  {status.get('timestamp')}")
    msg = status.get("message")
    if msg:
        for part in msg.get("parts", []):
            if part.get("text"):
                print(f"  进度：{part['text']}")
    for art in task.get("artifacts") or []:
        print(f"\n产物 {art.get('name') or art['artifactId']}")
        for part in art.get("parts", []):
            if part.get("text"):
                print(part["text"])
            elif part.get("data") is not None:
                print(json.dumps(part["data"], ensure_ascii=False, indent=2))
    if with_history:
        print("\n历史：")
        for item in task.get("history") or []:
            text = " ".join(
                p.get("text", "") for p in item.get("parts", []) if p.get("text")
            )
            print(f"  [{item['role']}] {text}")


def cmd_discover(_: argparse.Namespace) -> None:
    data = request("GET", "/agents")
    for item in data["agents"]:
        card = item["card"]
        print(f"{item['id']:<10} {card['name']} — {card['description']}")
        for skill in card["skills"]:
            print(f"           技能 {skill['id']}: {skill['description']}")


def cmd_card(args: argparse.Namespace) -> None:
    print(json.dumps(request("GET", f"/agents/{args.agent}/.well-known/agent-card.json"),
                     ensure_ascii=False, indent=2))


def cmd_send(args: argparse.Namespace) -> None:
    """异步提交后轮询。长任务的正确姿势。"""
    context_id = args.context or f"ctx-{int(time.time() * 1000)}"
    result = rpc(
        args.agent,
        "SendMessage",
        {
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": args.text, "mediaType": "text/plain"}],
                "contextId": context_id,
            },
            "configuration": {"returnImmediately": True},
        },
    )
    task = result["task"]
    task_id = task["id"]
    print(f"已提交 task={task_id} context={context_id}")

    seen: set[str] = set()
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        task = rpc(args.agent, "GetTask", {"id": task_id})["task"]
        status = task["status"]
        note = ""
        if status.get("message"):
            note = " ".join(
                p.get("text", "") for p in status["message"].get("parts", [])
            )
        if note and note not in seen:
            seen.add(note)
            print(f"  · {note}")
        if status["state"] in (
            "TASK_STATE_COMPLETED",
            "TASK_STATE_FAILED",
            "TASK_STATE_CANCELED",
            "TASK_STATE_REJECTED",
        ):
            print()
            show_task(task)
            return
        time.sleep(args.interval)

    print(f"\n超过 {args.timeout}s 未结束，任务仍在跑。稍后用：")
    print(f"  python client.py result {task_id}")


def cmd_send_now(args: argparse.Namespace) -> None:
    """同步阻塞一次调用。会被服务端的 A2A_BLOCKING_TIMEOUT 截断。"""
    result = rpc(
        args.agent,
        "SendMessage",
        {
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": args.text, "mediaType": "text/plain"}],
            },
            "configuration": {"returnImmediately": False},
        },
    )
    show_task(result["task"], with_history=True)


def cmd_result(args: argparse.Namespace) -> None:
    show_task(rpc(args.agent, "GetTask", {"id": args.task_id})["task"], with_history=True)


def cmd_list(args: argparse.Namespace) -> None:
    result = rpc(args.agent, "ListTasks", {"pageSize": args.limit})
    for task in result.get("tasks") or []:
        print(f"{task['id']}  {task['status']['state']}  {task['status']['timestamp']}")


def cmd_cancel(args: argparse.Namespace) -> None:
    show_task(rpc(args.agent, "CancelTask", {"id": args.task_id})["task"])


def cmd_watch(args: argparse.Namespace) -> None:
    """订阅 SSE。任务在库里，断线重连不丢。"""
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "SubscribeToTask", "params": {"id": args.task_id}}
    ).encode()
    req = urllib.request.Request(f"{BASE}/a2a/{args.agent}", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("A2A-Version", "1.0")
    if KEY:
        req.add_header("Authorization", f"Bearer {KEY}")
    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            payload = json.loads(line[5:].strip())
            status = payload["result"]["task"]["status"]
            note = ""
            if status.get("message"):
                note = " ".join(p.get("text", "") for p in status["message"]["parts"])
            print(f"{status['state']:<26} {note}")
            if status["state"].startswith(("TASK_STATE_COMPLETED", "TASK_STATE_FAILED",
                                           "TASK_STATE_CANCELED", "TASK_STATE_REJECTED")):
                show_task(payload["result"]["task"])
                return


def main() -> None:
    parser = argparse.ArgumentParser(description="A2A 客户端")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("discover", help="列出全部 agent")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("card", help="查看 Agent Card")
    p.add_argument("agent")
    p.set_defaults(func=cmd_card)

    p = sub.add_parser("send", help="异步提交并轮询结果")
    p.add_argument("agent")
    p.add_argument("text")
    p.add_argument("--context", help="预生成 contextId，断线后可凭它恢复")
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--interval", type=float, default=2.0)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("send-now", help="同步阻塞一次调用")
    p.add_argument("agent")
    p.add_argument("text")
    p.set_defaults(func=cmd_send_now)

    p = sub.add_parser("result", help="凭 task id 取结果")
    p.add_argument("task_id")
    p.add_argument("--agent", default="report")
    p.set_defaults(func=cmd_result)

    p = sub.add_parser("list", help="列出任务")
    p.add_argument("agent")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("cancel", help="取消任务")
    p.add_argument("task_id")
    p.add_argument("--agent", default="report")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("watch", help="订阅任务状态流（SSE）")
    p.add_argument("task_id")
    p.add_argument("--agent", default="report")
    p.set_defaults(func=cmd_watch)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
