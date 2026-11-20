"""Бүртгэлгүй, зөвхөн орчуулга хийдэг хувилбар: /simple
Хэрэглэгч (2026-11-10): үндсэн хуудастай ижил UI, 5000 тэмдэгт, бүртгэл шаардлагагүй.
Үндсэн сайтын загвар, файлын дарааллыг (worker) хуваалцана, тусдаа загвар ачаалахгүй.
"""
from fastapi import File, Form, HTTPException, Request, UploadFile

import contrib
import translator

SIMPLE_MAX_CHARS = 5000
# Хандивын данс (хэрэглэгч 2026-11-15 өгсөн, Монголын Эмнести Интернэшнл)
DONATE = dict(bank="Хаан банк", account="5041292715", iban="MN82 000500 5041292715", iban_copy="MN820005005041292715",
              recipient="МОНГОЛЫН ЭМНЕСТИ ИНТЕРНЭШНЛ", phone="7000-4706", online="https://amnesty.mn/donate")
SIMPLE_MAX_PAGES = 500         # хэрэглэгч (2026-11-12): хуудасны хязгаар харуулахгүй, 500-аас урт бол upload хийхэд л хэлнэ


def register(app, check_direction, templates, queue_file_job):
    @app.get("/simple")
    def simple_page(request: Request):
        return templates.TemplateResponse(request, "simple.html", dict(
            max_chars=SIMPLE_MAX_CHARS, max_pages=SIMPLE_MAX_PAGES, is_admin=contrib.is_admin_session(request),
            directions=translator.available_directions()))

    @app.get("/donate")
    def donate_page(request: Request):
        return templates.TemplateResponse(request, "donate.html", dict(DONATE))

    @app.post("/api/simple/translate")
    def simple_translate(payload: dict):
        text, direction = payload.get("text"), payload.get("direction", "en-mn")
        if not isinstance(text, str) or not isinstance(direction, str):
            raise HTTPException(400, "Хүсэлт буруу байна.")
        check_direction(direction)
        if not text.strip():
            raise HTTPException(400, "Текст хоосон байна.")
        if len(text) > SIMPLE_MAX_CHARS:
            raise HTTPException(400, f"Нэг удаад {SIMPLE_MAX_CHARS} тэмдэгт хүртэл.")
        segs = translator.translate_segments(text, direction)
        paras = {}
        for s in segs:
            paras.setdefault(s["para"], []).append(s["translation"])
        return {"translation": "\n\n".join(" ".join(v) for _, v in sorted(paras.items())), "segments": segs}

    @app.post("/api/simple/translate-file")
    async def simple_file(request: Request, file: UploadFile = File(...), direction: str = Form("en-mn"),
                          split: str = Form("")):
        return await queue_file_job(request, file, direction, skip_quota=True, max_pages=SIMPLE_MAX_PAGES)
