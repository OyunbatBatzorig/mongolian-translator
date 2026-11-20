#!/usr/bin/env bash
# Үнэгүй, бүртгэлгүй "зөвхөн орчуулга" сайтыг интернэтэд нээнэ (туршилтын хувилбар).
#   1) Нийтийн горимтой сервер (PUBLIC_SIMPLE=1) порт 8080 дээр асна.
#      Таны бүрэн хувилбар (8000, админ, нэвтрэх) өөрийн компьютер дээр л үлдэнэ.
#   2) Cloudflare quick tunnel https://<санамсаргүй>.trycloudflare.com хаяг өгнө.
#      Хаяг нь скрипт дахин асах бүрт өөрчлөгдөнө. Домэйн авсны дараа "named tunnel" болгоно.
# Зогсоох: Ctrl+C
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8080}"

cd webapp
PUBLIC_SIMPLE=1 APP_DB="$PWD/instance_public/app.db" ../venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port "$PORT" > ../public_server.log 2>&1 &
SERVER=$!
cd ..
trap 'echo; echo "Зогсоож байна..."; kill $SERVER 2>/dev/null; exit 0' INT TERM

echo "Сервер асаж, загвар ачаалж байна (30-60 сек)..."
for i in $(seq 1 90); do
  curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break
  kill -0 $SERVER 2>/dev/null || { echo "Сервер асаагүй. public_server.log-г үзнэ үү."; exit 1; }
  sleep 2
done
echo "Сервер бэлэн: http://127.0.0.1:$PORT"
echo "Cloudflare tunnel нээж байна. Доор гарах https://....trycloudflare.com хаягийг бусдад өгнө."
tools/cloudflared tunnel --no-autoupdate --protocol http2 --url "http://127.0.0.1:$PORT"
kill $SERVER 2>/dev/null
