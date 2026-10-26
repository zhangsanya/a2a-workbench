#!/bin/bash
# 在海外 VPS 上部署 A2A 服务端（群聊 + 取件口 + 加密层）
# 用法：把 a2a-server.tar.gz 传到 /tmp/，然后
#   BASE_URL=https://a2a.example.com bash deploy_vps.sh
set -euo pipefail

APP_DIR=/opt/a2a-server
DATA_DIR=/var/lib/a2a-server
SRC=/tmp/a2a-server.tar.gz
BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"

[ -f "$SRC" ] || { echo "找不到 $SRC"; exit 1; }

echo "== 1. 系统依赖 =="
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip curl >/dev/null
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q python3 python3-pip curl >/dev/null
fi
python3 -V

echo "== 2. 解包到 $APP_DIR =="
mkdir -p "$APP_DIR" "$DATA_DIR"
tar xzf "$SRC" -C "$APP_DIR"
chmod 755 "$DATA_DIR"

echo "== 3. 虚拟环境 =="
[ -d "$APP_DIR/venv" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
echo "  依赖：$("$APP_DIR/venv/bin/pip" list 2>/dev/null | grep -iE '^(fastapi|uvicorn)' | tr '\n' ' ')"

echo "== 4. 配置 /etc/a2a-server.env =="
cat > /etc/a2a-server.env <<EOF
# 海外 VPS 实例。本地那台的配置不适用：这里没有 llama.cpp，路由退回关键词规则。
A2A_DATA_DIR=$DATA_DIR
A2A_DB=$DATA_DIR/a2a.db
A2A_BASE_URL=$BASE_URL
A2A_DEFAULT_AGENT=notes
A2A_EXTERNAL_AGENTS=muse,grok
A2A_BLOCKING_TIMEOUT=25

# 1 = 没登记公钥的 agent 一律拒绝投递。等 muse/grok 都注册完可以打开。
A2A_REQUIRE_SEAL=0
EOF
chmod 600 /etc/a2a-server.env

echo "== 5. systemd 单元 =="
cat > /etc/systemd/system/a2a-server.service <<EOF
[Unit]
Description=A2A Server (群聊 + 取件口 + 信封加密)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=/etc/a2a-server.env
ExecStart=$APP_DIR/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8080 --log-level info
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now a2a-server >/dev/null 2>&1
sleep 3

echo "== 6. 自检 =="
systemctl is-active a2a-server
curl -s -o /dev/null -w "  /healthz     %{http_code}\n" http://127.0.0.1:8080/healthz
curl -s -o /dev/null -w "  /a2a/seal.py %{http_code}\n" http://127.0.0.1:8080/a2a/seal.py
echo
echo "服务器公钥（agent 用它加密回传）："
cd "$APP_DIR" && "$APP_DIR/venv/bin/python" keys.py seal-server 2>/dev/null || echo "  （先建一把 API key 再看）"
echo
echo "下一步：建 muse / grok 的 key"
echo "  cd $APP_DIR && ./venv/bin/python keys.py add muse"
echo "  cd $APP_DIR && ./venv/bin/python keys.py add grok"
