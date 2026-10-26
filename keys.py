"""API key 与信封公钥管理。本地运维脚本，直接操作 SQLite。

    python keys.py add alice                    # 生成一把新 key，明文只显示这一次
    python keys.py list                         # 列出属主（不显示 key 本身）

    python keys.py seal-server                  # 打印服务器公钥，agent 用它加密回传
    python keys.py seal-add muse <公钥base64>    # 登记某个 agent 的公钥
    python keys.py seal-list                    # 看谁已经能收密文
    python keys.py seal-rm muse                 # 撤销（之后该 agent 退回明文）

审计要落在 key 的属主上，不要相信消息 metadata 里自报的名字。
每个调用方单独发一把，出问题才追得清。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path

import conf  # noqa: F401  和服务进程读同一份配置，否则会连到另一个库
from store import Store

DB_PATH = Path(os.environ.get("A2A_DB", "./data/a2a.db"))


def main() -> None:
    parser = argparse.ArgumentParser(description="A2A API key / 信封公钥管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add")
    p.add_argument("owner")
    p.add_argument("--key", help="自定义 key，不填则随机生成")

    sub.add_parser("list")
    sub.add_parser("seal-server")

    p = sub.add_parser("seal-add", help="登记 agent 公钥")
    p.add_argument("agent")
    p.add_argument("pubkey", help="seal.py keygen 输出的公钥（base64url）")
    p.add_argument("--from-file", action="store_true", help="pubkey 参数是密钥文件路径")

    sub.add_parser("seal-list")

    p = sub.add_parser("seal-rm")
    p.add_argument("agent")

    args = parser.parse_args()
    store = Store(DB_PATH)

    if args.cmd == "add":
        key = args.key or f"a2a_{secrets.token_urlsafe(32)}"
        store.add_api_key(key, args.owner)
        print(f"owner={args.owner}")
        print(f"key={key}")
        print("\n明文只显示这一次，请立即保存。")
    elif args.cmd == "list":
        import sqlite3

        conn = sqlite3.connect(DB_PATH)
        for owner, created in conn.execute(
            "SELECT owner, created_at FROM api_keys ORDER BY created_at"
        ):
            print(f"{owner:<20} {created}")
        conn.close()
    elif args.cmd in {"seal-server", "seal-add", "seal-list", "seal-rm"}:
        import envelope

        with store._connect() as conn:
            conn.executescript(envelope.SCHEMA)
        if args.cmd == "seal-server":
            print(envelope.server_pub_b64())
        elif args.cmd == "seal-add":
            pub = args.pubkey
            if args.from_file:
                with open(pub, "r", encoding="utf-8") as fh:
                    raw = fh.read().strip()
                pub = json.loads(raw)["pub"] if raw.startswith("{") else raw
            envelope.set_pubkey(store, args.agent, pub)
            print(f"{args.agent} 已登记公钥，之后投给它的任务都会加密。")
        elif args.cmd == "seal-list":
            agents = envelope.sealed_agents(store)
            if not agents:
                print("（还没有 agent 登记公钥，所有投递都是明文）")
            for a in agents:
                print(f"{a:<16} {envelope.pubkey_of(store, a)}")
        elif args.cmd == "seal-rm":
            print("已撤销" if envelope.del_pubkey(store, args.agent) else "没找到这个 agent")


if __name__ == "__main__":
    main()
