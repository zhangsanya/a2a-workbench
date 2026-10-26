"""配置装载。

systemd 用 EnvironmentFile 把配置喂给服务进程，但运维脚本是手工跑的，
不会自动带上这些变量。以前就踩过：手工跑 keys.py 时 A2A_DB 没设，
它自己在一个空目录里建了新库、新密钥，看起来一切正常，
实际上服务进程和脚本各自拿着一把不同的密钥。

所以统一在这里读一次 /etc/a2a-server.env，已经存在的环境变量优先
（setdefault），这样 systemd 注入的值不会被文件覆盖。
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(os.environ.get("A2A_ENV_FILE", "/etc/a2a-server.env"))


def load() -> None:
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load()
