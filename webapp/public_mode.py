"""Нийтийн "зөвхөн орчуулга" горим (PUBLIC_SIMPLE=1).

Интернэтэд (Cloudflare Tunnel-ээр) нээхэд зөвхөн бүртгэлгүй /simple хуудас ба түүний API
харагдана. Нэвтрэх, бүртгүүлэх, төлбөр, админ зэрэг бүх хуудас 404 болно.
Энэ горимгүйгээр (PUBLIC_SIMPLE тохируулаагүй) сервер өмнөх шигээ бүрэн ажиллана.

Хамгаалалт:
  * IP тус бүр: текст минутад 10, өдөрт 300; файл цагт 5.
  * Файлын дараалал 20-оос урт бол шинэ файл хүлээж авахгүй (GPU-г нэг хүн дүүргэхгүй).
  * Орчуулсан текстийг хадгалахгүй, лог-д бичихгүй (uvicorn зөвхөн хаягийг бичнэ, текстийг биш).
  * Орчуулсан файлыг 2 цагийн дараа устгана.
"""
import os
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

ENABLED = os.environ.get("PUBLIC_SIMPLE", "") == "1"

TEXT_PER_MINUTE = int(os.environ.get("PUBLIC_TEXT_PER_MINUTE", "10"))
TEXT_PER_DAY = int(os.environ.get("PUBLIC_TEXT_PER_DAY", "300"))
FILES_PER_HOUR = int(os.environ.get("PUBLIC_FILES_PER_HOUR", "5"))
MAX_QUEUE = 20
RESULT_TTL_HOURS = 2

# Нийтэд нээлттэй хаягууд (бусад бүгд 404)
ALLOWED_EXACT = {"/", "/simple", "/api/simple/translate", "/api/simple/translate-file",
                 "/contribute", "/api/contribute/start", "/api/contribute/verify", "/donate",
                 "/download-zip", "/favicon.ico", "/robots.txt"}
ALLOWED_PREFIX = ("/static/", "/api/jobs/", "/download/")

_hits = defaultdict(deque)  # (ip, kind) -> цагийн тэмдэг


def _allow(key, limit, window, record=True):
    now, q = time.time(), _hits[key]
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= limit:
        return False
    if record:
        q.append(now)
    return True


def _too_many(msg):
    return JSONResponse({"error": msg}, status_code=429)


def register(app, client_ip, jobs_q):
    if not ENABLED:
        return
    @app.middleware("http")
    async def public_only(request: Request, call_next):
        path = request.url.path
        if not (path in ALLOWED_EXACT or path.startswith(ALLOWED_PREFIX)):
            if path.startswith("/api/"):
                return JSONResponse({"error": "Олдсонгүй."}, status_code=404)
            return RedirectResponse("/", status_code=302)
        if path == "/robots.txt":
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse("User-agent: *\nDisallow: /api/\nDisallow: /download\n")
        if path == "/":  # нүүр хуудас = энгийн орчуулагч
            request.scope["path"] = "/simple"
        if request.method == "POST":
            ip = client_ip(request)
            if path == "/api/simple/translate":
                if not _allow((ip, "m"), TEXT_PER_MINUTE, 60):
                    return _too_many("Хэт олон хүсэлт илгээлээ. Нэг минут хүлээгээд дахин оролдоно уу.")
                if not _allow((ip, "d"), TEXT_PER_DAY, 86400):
                    return _too_many("Өнөөдрийн хязгаар дууслаа. Маргааш дахин оролдоно уу.")
            elif path == "/api/simple/translate-file":
                if jobs_q.qsize() >= MAX_QUEUE:
                    return JSONResponse({"error": "Сервер завгүй байна. Хэдэн минутын дараа дахин оролдоно уу."},
                                        status_code=503)
                # Зөвхөн амжилттай илгээсэн файлыг тоолно ("хуваах уу?" гэсэн асуулт, алдаа тоологдохгүй)
                if not _allow((ip, "f"), FILES_PER_HOUR, 3600, record=False):
                    return _too_many(f"Цагт {FILES_PER_HOUR} файл хүртэл орчуулна. Түр хүлээнэ үү.")
            elif path in ("/api/contribute/start", "/api/contribute/verify"):
                if not _allow((ip, "c"), 10, 3600):
                    return _too_many("Хэт олон оролдлого. Нэг цагийн дараа дахин оролдоно уу.")
            elif not (path.startswith("/api/jobs/") and path.endswith("/cancel")):
                return JSONResponse({"error": "Олдсонгүй."}, status_code=404)
        resp = await call_next(request)
        if request.method == "POST" and path == "/api/simple/translate-file" and resp.status_code == 200:
            _hits[(client_ip(request), "f")].append(time.time())
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp
