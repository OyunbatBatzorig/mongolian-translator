"""Орчуулгын вебийн сервер (FastAPI).

Ажиллуулах:   cd webapp && ../venv/bin/python -m uvicorn main:app --port 8000
Нээх:          http://localhost:8000

Хуудаснууд:
  /                 орчуулах хуудас (текст + файл)
  /login /register  нэвтрэх, бүртгүүлэх
  /account          миний эрх, энэ сарын хэрэглээ
  /admin            админ самбар (зөвхөн админ)
API (хуудас доторх JavaScript дууддаг):
  POST /api/translate          {"text": "...", "direction": "en-mn"}
  POST /api/translate-file     файл илгээх -> job id
  GET  /api/jobs/{id}          файл орчуулгын явц
  GET  /download/{id}          орчуулсан DOCX татах
"""
import os
import queue
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import re

import auth
import db
import documents
import mailer
import otp
import translator
from config import (BASE_DIR, EBARIMT_ENABLED, FILES_PER_MONTH, GUEST_EMAIL, GUEST_REQUESTS_PER_MINUTE, TRIAL_DAYS, MAX_FILE_PAGES, MAX_UPLOAD_MB, TEXT_LIMITS,
                    PLAN_LABELS, PLANS, RESULT_DIR, RESULT_TTL_HOURS, UPLOAD_DIR)

app = FastAPI(title="MN-EN хүний эрхийн орчуулагч")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


@app.middleware("http")
async def security_headers(request, call_next):
    """Хөтөчийн хамгаалалт: өөр сайт манай хуудсыг iframe-д хийх (clickjacking), гадны script ачаалах,
    файлын төрлийг буруу таних зэргийг хориглоно."""
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.url.path.startswith(("/simple/admin", "/admin", "/api/")):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def _local(iso):
    """UTC ISO огноог Улаанбаатарын цагаар 'YYYY-MM-DD HH:MM' болгож харуулна."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromisoformat(iso).astimezone(ZoneInfo("Asia/Ulaanbaatar")).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return (iso or "")[:16].replace("T", " ")


templates.env.filters["local"] = _local
DIRECTION_LABELS = {"en-mn": "Англи → Монгол", "mn-en": "Монгол → Англи"}


# ---------------------------------------------------------------- туслах функцууд
def client_ip(request):
    """Жинхэнэ хэрэглэгчийн IP. Cloudflare Tunnel-ээр орж ирэхэд бүх хүсэлт 127.0.0.1-ээс ирсэн мэт
    харагддаг тул Cloudflare-ийн CF-Connecting-IP толгойг ашиглана (зөвхөн локал proxy-оос ирсэн үед итгэнэ)."""
    host = request.client.host if request.client else "?"
    if host in ("127.0.0.1", "::1"):
        return request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", host).split(",")[0].strip()
    return host


def is_https(request):
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def set_session_cookie(resp, request, token):
    # HTTPS-ээр (Cloudflare) орж ирсэн бол cookie-г зөвхөн HTTPS-ээр дамжуулна
    resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="strict", max_age=14 * 86400,
                    secure=is_https(request))


def current_user(request):
    with db.connect() as con:
        return auth.user_from_token(con, request.cookies.get(auth.COOKIE))


def page(request, name, **ctx):
    user = current_user(request)
    ok, why = auth.has_access(user)
    fok, fwhy = auth.has_file_access(user)
    return templates.TemplateResponse(request, name, {
        "user": user, "can_translate": ok, "access_msg": why, "can_files": fok, "file_msg": fwhy,
        "plan_labels": PLAN_LABELS, "max_chars": auth.text_limit(user), "text_limits": TEXT_LIMITS,
        "max_pages": MAX_FILE_PAGES,
        "files_per_month": auth.file_quota(user), "file_quotas": FILES_PER_MONTH,
        "ebarimt_enabled": EBARIMT_ENABLED, "plan_now": auth.effective_plan(user), "trial_until": auth.trial_until(user), "trial_days": TRIAL_DAYS,
        "directions": translator.available_directions(),
        "direction_labels": DIRECTION_LABELS, **ctx})


GUEST_COOKIE = "gid"


def owner(request, con):
    """Файлын эзэн: нэвтэрсэн хэрэглэгч, эсвэл зочин (cookie + IP хаягаар таних).
    Буцаах нь (user, user_id, guest_key, guest_ip)."""
    user = auth.user_from_token(con, request.cookies.get(auth.COOKIE))
    if user:
        return user, user["id"], None, None
    gid = request.cookies.get(GUEST_COOKIE) or ""
    ip = client_ip(request)
    return None, db.guest_user_id(con), gid, ip


def job_owner_sql(user, gid):
    """Тухайн хүн зөвхөн өөрийн ажлыг харах SQL нөхцөл."""
    if user:
        return "user_id=?", (user["id"],)
    return "guest_key=? AND guest_key!=''", (gid,)


def require_admin(request):
    user = current_user(request)
    if not user or user["plan"] != "admin":
        raise HTTPException(403, "Зөвхөн админ.")
    return user


def check_direction(direction):
    if not translator.available_directions().get(direction):
        raise HTTPException(400, "Энэ чиглэлийн загвар хараахан бэлэн болоогүй байна.")


def files_this_month(con, user_id, guest_key=None, guest_ip=None):
    month = db.this_month()
    if guest_ip is None:
        return con.execute(
            "SELECT COUNT(*) FROM jobs WHERE user_id=? AND substr(created_at,1,7)=? AND status!='failed'",
            (user_id, month)).fetchone()[0]
    # Зочин: cookie-г устгаад дахин оролдвол IP-ээр нь таньна
    return con.execute(
        "SELECT COUNT(*) FROM jobs WHERE user_id=? AND substr(created_at,1,7)=? AND status!='failed' "
        "AND ((guest_key=? AND guest_key!='') OR guest_ip=?)",
        (user_id, month, guest_key or "", guest_ip)).fetchone()[0]


_guest_hits = defaultdict(deque)
_login_hits = defaultdict(deque)


def guest_rate_ok(ip):
    """Нэвтрээгүй зочин нэг IP-ээс минутад GUEST_REQUESTS_PER_MINUTE-ээс их хүсэлт илгээхгүй."""
    now, q = time.time(), _guest_hits[ip]
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= GUEST_REQUESTS_PER_MINUTE:
        return False
    q.append(now)
    return True


# ---------------------------------------------------------------- файл орчуулгын арын ажил
jobs_q = queue.Queue()


class JobCancelled(Exception):
    pass


def worker():
    while True:
        job_id, path, ext = jobs_q.get()
        try:
            with db.connect() as con:
                # Хэрэглэгч дараалалд байхад нь цуцалсан бол алгасна
                if con.execute("UPDATE jobs SET status='running' WHERE id=? AND status='queued'", (job_id,)).rowcount == 0:
                    continue
                job = dict(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            paras, _ = documents.read_document(path, ext) if ext != ".docx" else ([], 0)

            def progress(p):
                with db.connect() as con:
                    con.execute("UPDATE jobs SET progress=? WHERE id=?", (round(p, 3), job_id))
                    if con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "cancelled":
                        raise JobCancelled()  # орчуулж байхад нь цуцалсан: GPU-г дараагийн хүнд чөлөөлнө

            os.makedirs(RESULT_DIR, exist_ok=True)
            result = os.path.join(RESULT_DIR, f"{job_id}.docx")
            if ext == ".docx":  # эх файлын хэлбэржилт, хүснэгт, зургийг хадгалан орчуулна
                def tr(texts):
                    paras.extend(texts)
                    return translator.translate_paragraphs(texts, job["direction"], on_progress=progress)
                try:
                    documents.translate_docx(path, result, tr)
                except JobCancelled:
                    raise
                except Exception:  # noqa: BLE001  (эвдэрхий бүтэц) -> энгийн DOCX
                    paras = documents.read_document(path, ext)[0]
                    out = translator.translate_paragraphs(paras, job["direction"], on_progress=progress)
                    documents.write_docx([p for p in out if p], result)
            else:
                out = translator.translate_paragraphs(paras, job["direction"], on_progress=progress)
                documents.write_docx([p for p in out if p], result)
            with db.connect() as con:
                con.execute("UPDATE jobs SET status='done', progress=1, result_path=? WHERE id=?",
                            (result, job_id))
                con.execute("INSERT INTO usage(user_id,kind,direction,chars,pages,month,created_at) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (job["user_id"], "file", job["direction"], sum(map(len, paras)),
                             job["pages"], db.this_month(), db.now_iso()))
        except JobCancelled:
            pass
        except Exception as e:  # noqa: BLE001
            with db.connect() as con:
                con.execute("UPDATE jobs SET status='failed', error=? WHERE id=? AND status!='cancelled'",
                            (str(e)[:300], job_id))
        finally:
            if os.path.exists(path):
                os.remove(path)  # эх файлыг серверт үлдээхгүй


def cleanup_results():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=RESULT_TTL_HOURS)).isoformat()
    with db.connect() as con:
        for r in con.execute("SELECT id, result_path FROM jobs WHERE result_path IS NOT NULL "
                             "AND created_at < ?", (cutoff,)).fetchall():
            if r["result_path"] and os.path.exists(r["result_path"]):
                os.remove(r["result_path"])
            # Файлын нэр ч хувийн мэдээлэл агуулж болзошгүй ("Хохирогч_Б_мэдүүлэг.pdf") -> хугацаа дуусахад арилгана
            con.execute("UPDATE jobs SET result_path=NULL, filename='(устгасан)' WHERE id=?", (r["id"],))


@app.on_event("startup")
def startup():
    db.init()
    with db.connect() as con:  # сервер унтарч асахад дутуу үлдсэн ажлууд
        con.execute("UPDATE jobs SET status='failed', error='Сервер дахин асав' "
                    "WHERE status IN ('queued','running')")
    # Дутуу үлдсэн ажлын эх файлуудыг серверт үлдээхгүй (дараалал шинээр эхэлж байгаа тул бүгд хуучных)
    if os.path.isdir(UPLOAD_DIR):
        for f in os.listdir(UPLOAD_DIR):
            try:
                os.remove(os.path.join(UPLOAD_DIR, f))
            except OSError:
                pass
    threading.Thread(target=worker, daemon=True).start()

    def janitor():  # хугацаа нь дууссан орчуулсан файлыг хэн ч татаагүй байсан ч 10 минут тутам устгана
        import time
        while True:
            try:
                cleanup_results()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(600)
    threading.Thread(target=janitor, daemon=True).start()
    # Загварыг эхэнд нь ачаалчихна (эс бөгөөс анхны орчуулга ~40 сек хүлээлгэнэ)
    for d, ok in translator.available_directions().items():
        if ok:
            threading.Thread(target=translator._load, args=(d,), daemon=True).start()


# ---------------------------------------------------------------- хуудаснууд
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return page(request, "index.html")


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = ""):
    return page(request, "login.html", error=None, next=safe_next(next))


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("")):
    # QA #5: нууц үг таах (brute force) оролдлогоос хамгаалах — нэг IP-ээс минутад 10 оролдлого
    ip = client_ip(request)
    q = _login_hits[ip]
    now = time.time()
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= 10:
        return page(request, "login.html", error="Хэт олон удаа оролдлоо. 1 минутын дараа дахин оролдоно уу.",
                    next=safe_next(next))
    q.append(now)
    with db.connect() as con:
        row = con.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
        if not row or row["disabled"] or not auth.check_password(password, row["password_hash"]):
            return page(request, "login.html", error="Имэйл эсвэл нууц үг буруу байна.", next=safe_next(next))
        token = auth.create_session(con, row["id"])
    resp = RedirectResponse(safe_next(next) or "/", status_code=303)
    set_session_cookie(resp, request, token)
    return resp


def safe_next(nxt):
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else ""


@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request, next: str = ""):
    return page(request, "register.html", error=None, step="email", email="", next=safe_next(next))


@app.post("/register")
def register(request: Request, email: str = Form(...), next: str = Form("")):
    """1-р алхам: имэйл оруулахад баталгаажуулах код илгээнэ."""
    email = email.strip().lower()
    if not EMAIL_RE.fullmatch(email):
        return page(request, "register.html", error="Зөв имэйл хаяг оруулна уу.", step="email", email=email,
                    next=safe_next(next))
    with db.connect() as con:
        if con.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            return page(request, "register.html", error="Энэ имэйлээр бүртгэл аль хэдийн байна. Нэвтэрнэ үү.",
                        step="email", email=email)
        ok, why = otp.send_code(con, email, "register")
    return page(request, "register.html", error=None if ok else why, step="code", email=email,
                info=f"{email} хаяг руу 6 оронтой код илгээлээ.", next=safe_next(next))


@app.post("/register/verify")
def register_verify(request: Request, email: str = Form(...), code: str = Form(...), name: str = Form(""),
                    password: str = Form(...), password2: str = Form(...), next: str = Form("")):
    """2-р алхам: код + нууц үг. Код зөв бол бүртгэл үүсгэж нэвтрүүлнэ."""
    email = email.strip().lower()
    err = password_problem(password, password2)
    if err:
        return page(request, "register.html", error=err, step="code", email=email, next=safe_next(next))
    with db.connect() as con:
        ok, why = otp.check_code(con, email, "register", code)
        if not ok:
            return page(request, "register.html", error=why, step="code", email=email, next=safe_next(next))
        if con.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            return page(request, "register.html", error="Энэ имэйлээр бүртгэл аль хэдийн байна.", step="email",
                        email=email)
        cur = con.execute("INSERT INTO users(email,name,password_hash,plan,created_at) VALUES (?,?,?,?,?)",
                          (email, name.strip(), auth.hash_password(password), "unpaid", db.now_iso()))
        token = auth.create_session(con, cur.lastrowid)
    resp = RedirectResponse(safe_next(next) or "/account", status_code=303)
    set_session_cookie(resp, request, token)
    return resp


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")  # хашилт, < > зэргийг зөвшөөрөхгүй (XSS)


def password_problem(p1, p2):
    if len(p1) < 8:
        return "Нууц үг 8-аас дээш тэмдэгттэй байх ёстой."
    if p1 != p2:
        return "Хоёр нууц үг таарахгүй байна."
    return None


@app.get("/forgot", response_class=HTMLResponse)
def forgot_form(request: Request):
    return page(request, "forgot.html", error=None, info=None, step="email", email="")


@app.post("/forgot")
def forgot(request: Request, email: str = Form(...)):
    email = email.strip().lower()
    with db.connect() as con:
        row = con.execute("SELECT id FROM users WHERE email=? AND disabled=0", (email,)).fetchone()
        if row:
            otp.send_code(con, email, "password", data=str(row["id"]))
    # Бүртгэл байгаа эсэхийг илчлэхгүйн тулд ямар ч тохиолдолд ижил хариу өгнө
    return page(request, "forgot.html", error=None, step="code", email=email,
                info="Энэ имэйлээр бүртгэл байгаа бол код илгээлээ.")


@app.post("/forgot/verify")
def forgot_verify(request: Request, email: str = Form(...), code: str = Form(...), password: str = Form(...),
                  password2: str = Form(...)):
    email = email.strip().lower()
    err = password_problem(password, password2)
    if err:
        return page(request, "forgot.html", error=err, info=None, step="code", email=email)
    with db.connect() as con:
        ok, data = otp.check_code(con, email, "password", code)
        if not ok:
            return page(request, "forgot.html", error=data, info=None, step="code", email=email)
        con.execute("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(password), int(data)))
        con.execute("DELETE FROM sessions WHERE user_id=?", (int(data),))
    return page(request, "login.html", error=None, info="Нууц үг солигдлоо. Шинэ нууц үгээрээ нэвтэрнэ үү.")


@app.post("/logout")
def logout(request: Request):
    with db.connect() as con:
        auth.delete_session(con, request.cookies.get(auth.COOKIE))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


def days_left(user):
    """Эрх дуусахад үлдсэн хоног, хугацаагүй эрхэд None."""
    plan = auth.effective_plan(user)
    if plan == "paid" and user["paid_until"]:
        return (date.fromisoformat(user["paid_until"]) - date.today()).days
    t = auth.trial_until(user)
    if t:
        return max(0, (t.date() - date.today()).days)
    return None


@app.get("/account", response_class=HTMLResponse)
def account(request: Request, msg: str = "", err: str = "", step: str = ""):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    month = db.this_month()
    with db.connect() as con:
        usage = db.monthly_usage(con, user["id"])
        files_used = files_this_month(con, user["id"])
        # Түүх: зөвхөн огноо, хэмжээ, чиглэл. Орчуулсан текст, файлын нэр/агуулгыг харуулахгүй (хадгалдаггүй).
        text_hist = [dict(r) for r in con.execute(
            "SELECT created_at, chars, direction FROM usage WHERE user_id=? AND kind='text' AND month=? "
            "ORDER BY created_at DESC LIMIT 200", (user["id"], month))]
        file_hist = [dict(r) for r in con.execute(
            "SELECT created_at, pages, direction, status FROM jobs WHERE user_id=? AND substr(created_at,1,7)=? "
            "ORDER BY created_at DESC", (user["id"], month))]
        orders = [dict(o) for o in con.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 20", (user["id"],))]
        pending_email = con.execute("SELECT email FROM otps WHERE purpose='email_change' AND data=? "
                                    "ORDER BY id DESC LIMIT 1", (str(user["id"]),)).fetchone()
    return page(request, "account.html", usage=usage, files_used=files_used, today=date.today().isoformat(),
                days_left=days_left(user), text_hist=text_hist, file_hist=file_hist, msg=msg, err=err,
                step=step, pending_email=pending_email["email"] if pending_email else "",
                direction_labels=DIRECTION_LABELS, orders=orders, status_labels=billing.STATUS_LABELS,
                period_labels=billing.PERIOD_LABELS)


def back(msg="", err="", step=""):
    from urllib.parse import urlencode
    q = urlencode({k: v for k, v in {"msg": msg, "err": err, "step": step}.items() if v})
    return RedirectResponse("/account" + ("?" + q if q else "") + "#security", status_code=303)


@app.post("/account/email")
def account_email(request: Request, new_email: str = Form(...)):
    """Имэйл солих 1-р алхам: ШИНЭ хаяг руу код илгээнэ (тэр хаяг үнэхээр таных гэдгийг батлах)."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    new_email = new_email.strip().lower()
    if not EMAIL_RE.fullmatch(new_email):
        return back(err="Зөв имэйл хаяг оруулна уу.")
    with db.connect() as con:
        if con.execute("SELECT 1 FROM users WHERE email=?", (new_email,)).fetchone():
            return back(err="Энэ имэйлээр өөр бүртгэл байна.")
        ok, why = otp.send_code(con, new_email, "email_change", data=str(user["id"]))
    return back(msg=f"{new_email} хаяг руу код илгээлээ." if ok else "", err=why, step="email")


@app.post("/account/email/verify")
def account_email_verify(request: Request, new_email: str = Form(...), code: str = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    new_email = new_email.strip().lower()
    with db.connect() as con:
        ok, data = otp.check_code(con, new_email, "email_change", code)
        if not ok:
            return back(err=data, step="email")
        if data != str(user["id"]):
            return back(err="Код энэ бүртгэлд хамаарахгүй байна.")
        con.execute("UPDATE users SET email=? WHERE id=?", (new_email, user["id"]))
    mailer.send(user["email"], "Хүний эрхийн орчуулагч: имэйл хаяг солигдлоо",
                f"Таны бүртгэлийн имэйл {new_email} болж солигдлоо. Хэрэв та өөрөө солиогүй бол админд хандана уу.")
    return back(msg="Имэйл хаяг солигдлоо.")


@app.post("/account/password")
def account_password(request: Request):
    """Нууц үг солих 1-р алхам: одоогийн имэйл рүү код илгээнэ."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    with db.connect() as con:
        ok, why = otp.send_code(con, user["email"], "password", data=str(user["id"]))
    return back(msg=f"{user['email']} хаяг руу код илгээлээ." if ok else "", err=why, step="password")


@app.post("/account/password/verify")
def account_password_verify(request: Request, code: str = Form(...), password: str = Form(...),
                            password2: str = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    err = password_problem(password, password2)
    if err:
        return back(err=err, step="password")
    with db.connect() as con:
        ok, data = otp.check_code(con, user["email"], "password", code)
        if not ok:
            return back(err=data, step="password")
        con.execute("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(password), user["id"]))
        # Бусад төхөөрөмж дээрх нэвтрэлтийг хаана (одоогийнхоос бусад)
        con.execute("DELETE FROM sessions WHERE user_id=? AND token!=?", (user["id"], request.cookies.get(auth.COOKIE)))
    return back(msg="Нууц үг солигдлоо.")


# ---------------------------------------------------------------- орчуулгын API
@app.post("/api/translate")
async def api_translate(request: Request):
    user = current_user(request)  # None байж болно: зочин 1200 тэмдэгт хүртэл орчуулна
    if user is None and not guest_rate_ok(client_ip(request)):
        raise HTTPException(429, "Хэт олон хүсэлт илгээлээ. Түр хүлээгээд дахин оролдоно уу.")
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001  (QA bug #1: эвдэрсэн JSON 500 алдаа өгч байсан)
        raise HTTPException(400, "Хүсэлт буруу байна.")
    if not isinstance(data, dict) or not isinstance(data.get("text", ""), str) \
            or not isinstance(data.get("direction", "en-mn"), str):
        raise HTTPException(400, "Хүсэлт буруу байна.")  # QA bug #2: текст биш утга 500 өгч байсан
    text, direction = (data.get("text") or "").strip(), data.get("direction", "en-mn")
    check_direction(direction)
    if not text:
        raise HTTPException(400, "Текст хоосон байна.")
    limit = auth.text_limit(user)
    if len(text) > limit:
        more = " Бүртгүүлж төлбөр төлбөл илүү урт текст орчуулна." if limit < TEXT_LIMITS["paid"] else ""
        raise HTTPException(400, f"Нэг удаад {limit} тэмдэгт хүртэл орчуулна.{more}")
    # Эх текстийн байрлалтай харьцуулж харуулахын тулд урд/хойд хоосон зайг тасдахгүй
    raw = data.get("text") or ""
    segments = translator.translate_segments(raw, direction)
    with db.connect() as con:  # текстийг биш, зөвхөн уртыг нь бүртгэнэ
        if user is None:
            con.execute("INSERT INTO guest_usage(month,texts,chars) VALUES (?,1,?) "
                        "ON CONFLICT(month) DO UPDATE SET texts=texts+1, chars=chars+excluded.chars",
                        (db.this_month(), len(text)))
        else:
            con.execute("INSERT INTO usage(user_id,kind,direction,chars,month,created_at) VALUES (?,?,?,?,?,?)",
                        (user["id"], "text", direction, len(text), db.this_month(), db.now_iso()))
    paras = {}
    for seg in segments:
        paras.setdefault(seg["para"], []).append(seg["translation"])
    result = "\n\n".join(" ".join(v) for _, v in sorted(paras.items()))
    return {"translation": result, "segments": segments}


@app.post("/api/translate-file")
async def api_translate_file(request: Request, file: UploadFile = File(...),
                             direction: str = Form("en-mn")):
    return await queue_file_job(request, file, direction)


async def queue_file_job(request, file, direction, skip_quota=False, max_pages=MAX_FILE_PAGES,
                         can_split=False, split=False, split_max_pages=0):
    """Файлыг орчуулгын дараалалд оруулна. skip_quota=True: бүртгэлгүй /simple хуудас (хязгааргүй).
    can_split=True (/simple): max_pages-аас урт файлд эхлээд 409 {needs_split} буцаана; хэрэглэгч
    зөвшөөрвөл (split=True) файлыг max_pages хуудастай хэсгүүдэд хувааж, хэсэг бүрийг тусдаа ажил болгоно."""
    check_direction(direction)
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in documents.ALLOWED:
        raise HTTPException(400, "Зөвхөн PDF, DOCX, DOC, ODT, RTF, TXT файл.")
    with db.connect() as con:
        user, uid, gid, gip = owner(request, con)
        quota = None if skip_quota else auth.file_quota(user)
        if quota is not None and files_this_month(con, uid, gid, gip) >= quota:
            raise HTTPException(403, auth.quota_message(user, quota))
    new_gid = None
    if user is None and not gid:
        gid = new_gid = secrets.token_urlsafe(16)
    data = await file.read(MAX_UPLOAD_MB * 1024 * 1024 + 1)
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(400, f"Файл {MAX_UPLOAD_MB} MB-аас ихгүй байх ёстой.")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    job_id = secrets.token_urlsafe(12)
    path = os.path.join(UPLOAD_DIR, job_id + ext)  # хэрэглэгчийн файлын нэрийг зам болгож ашиглахгүй
    with open(path, "wb") as f:
        f.write(data)
    from starlette.concurrency import run_in_threadpool
    if ext in documents.LEGACY:  # хуучин Word (.doc), OpenDocument (.odt), RTF -> DOCX
        try:
            new = await run_in_threadpool(documents.convert_legacy, path)
        except Exception:  # noqa: BLE001
            os.remove(path)
            raise HTTPException(400, "Файлыг хөрвүүлж чадсангүй. Word дээрээ нээгээд DOCX эсвэл PDF болгож хадгалаад оруулна уу.")
        os.remove(path)
        path, ext = new, ".docx"
    try:  # тусдаа процесст, хугацаа/санах ойн хязгаартай: эвдэрсэн эсвэл хор хөнөөлтэй файл серверийг гацаахгүй
        pages, chars, scanned = await run_in_threadpool(documents.inspect_safe, path, ext)
    except Exception as ex:  # noqa: BLE001
        os.remove(path)
        if "password" in str(ex):
            raise HTTPException(400, "Энэ PDF нууц үгээр хамгаалагдсан байна. Нууц үггүйгээр хадгалаад дахин оруулна уу.")
        raise HTTPException(400, "Файлыг уншиж чадсангүй. Файл эвдэрсэн эсвэл бүтэц нь хэт нарийн байна.")
    if scanned and not documents.ocr_available():
        scanned = 0
    if chars == 0 and not scanned:
        os.remove(path)
        raise HTTPException(400, "Файлаас текст олдсонгүй. Скан хийсэн (зураг хэлбэрийн) PDF байж магадгүй. Ийм файлыг одоогоор орчуулах боломжгүй."
                            if ext == ".pdf" else "Файл хоосон байна эсвэл зөвхөн зурагтай байна.")
    if chars > documents.MAX_FILE_CHARS:
        os.remove(path)
        raise HTTPException(400, "Файл дахь текст хэт их байна. Хэд хуваагаад оруулна уу.")
    name = (file.filename or "file")[:200]
    parts = [(job_id, path, ext, pages, name, None)]
    if pages > max_pages:
        if not can_split:
            os.remove(path)
            raise HTTPException(400, f"Файл {pages} хуудастай байна. {max_pages} хуудас хүртэл зөвшөөрнө.")
        n_parts = -(-pages // max_pages)
        if split_max_pages and pages > split_max_pages:
            os.remove(path)
            raise HTTPException(400, f"Файл {pages} хуудастай байна. Хувааж орчуулахад ч {split_max_pages} хуудас хүртэл зөвшөөрнө.")
        if not split:  # эхлээд хэрэглэгчээс асууна (файлыг серверт үлдээхгүй, зөвшөөрвөл хөтөч дахин илгээнэ)
            os.remove(path)
            return JSONResponse({"needs_split": True, "pages": pages, "per_part": max_pages, "parts": n_parts,
                                 "error": f"Файл {pages} хуудастай ({max_pages}-аас их)."}, status_code=409)
        try:
            pieces = documents.split_document(path, ext, pages, max_pages)
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "Файлыг хувааж чадсангүй.")
        finally:
            os.remove(path)
        stem, ext0 = os.path.splitext(name)
        parts = [(secrets.token_urlsafe(12), pth, e, n, f"{stem} ({a}–{b}-р хуудас){ext0}"[:200], (a, b))
                 for pth, e, n, (a, b) in pieces]
    with db.connect() as con:
        for jid, _, _, n, fname, _ in parts:
            con.execute("INSERT INTO jobs(id,user_id,filename,direction,pages,status,created_at,guest_key,guest_ip) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (jid, uid, fname, direction, n, "queued", db.now_iso(), gid, gip))
    for jid, pth, e, _, _, _ in parts:
        jobs_q.put((jid, pth, e))
    resp = JSONResponse({"job_id": parts[0][0], "pages": pages,
                         "jobs": [{"job_id": jid, "pages": n, "name": fname, "range": rng}
                                  for jid, _, _, n, fname, rng in parts]})
    if new_gid:  # зочныг дараагийн удаа танихын тулд
        resp.set_cookie(GUEST_COOKIE, new_gid, httponly=True, samesite="strict", max_age=400 * 86400,
                        secure=is_https(request))
    return resp


@app.get("/api/jobs/{job_id}")
def api_job(request: Request, job_id: str):
    with db.connect() as con:
        user, _, gid, _ = owner(request, con)
        cond, args = job_owner_sql(user, gid)
        job = con.execute(f"SELECT id,filename,status,progress,error,pages FROM jobs WHERE id=? AND {cond}",
                          (job_id, *args)).fetchone()
    if not job:
        raise HTTPException(404, "Олдсонгүй.")
    return dict(job)


@app.post("/api/jobs/{job_id}/cancel")
def api_job_cancel(request: Request, job_id: str):
    """Буруу файл оруулсан бол орчуулж байх үед нь цуцлах (зөвхөн өөрийн ажлыг)."""
    with db.connect() as con:
        user, _, gid, _ = owner(request, con)
        cond, args = job_owner_sql(user, gid)
        n = con.execute(f"UPDATE jobs SET status='cancelled', error='Цуцалсан' WHERE id=? AND {cond} "
                        "AND status IN ('queued','running')", (job_id, *args)).rowcount
    if not n:
        raise HTTPException(404, "Олдсонгүй.")
    return {"ok": True}


@app.get("/download/{job_id}")
def download(request: Request, job_id: str):
    cleanup_results()
    with db.connect() as con:
        user, _, gid, _ = owner(request, con)
        cond, args = job_owner_sql(user, gid)
        job = con.execute(f"SELECT * FROM jobs WHERE id=? AND {cond}", (job_id, *args)).fetchone()
    if not job or job["status"] != "done" or not job["result_path"] or not os.path.exists(job["result_path"]):
        raise HTTPException(404, "Файл олдсонгүй эсвэл хугацаа нь дууссан.")
    name = os.path.splitext(job["filename"])[0] + "_орчуулга.docx"
    return FileResponse(job["result_path"], filename=name,
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.get("/download-zip")
def download_zip(request: Request, ids: str = ""):
    """Олон файлын орчуулгыг нэг ZIP болгож татах (зөвхөн өөрийн, бэлэн болсон файлууд)."""
    import io
    import zipfile
    from fastapi.responses import Response
    cleanup_results()
    wanted = [i for i in ids.split(",") if i][:50]
    with db.connect() as con:
        user, _, gid, _ = owner(request, con)
        cond, args = job_owner_sql(user, gid)
        jobs = [con.execute(f"SELECT * FROM jobs WHERE id=? AND {cond}", (j, *args)).fetchone() for j in wanted]
    jobs = [j for j in jobs if j and j["status"] == "done" and j["result_path"] and os.path.exists(j["result_path"])]
    if not jobs:
        raise HTTPException(404, "Татах файл олдсонгүй эсвэл хугацаа нь дууссан.")
    buf, used = io.BytesIO(), set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for j in jobs:
            base = os.path.splitext(os.path.basename(j["filename"]))[0] + "_орчуулга"
            name, n = base + ".docx", 2
            while name in used:  # ижил нэртэй файл давхцахгүй
                name, n = f"{base} ({n}).docx", n + 1
            used.add(name)
            z.write(j["result_path"], name)
    from urllib.parse import quote
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=translations.zip; "
                                                    f"filename*=UTF-8''{quote('орчуулгууд.zip')}"})


# ---------------------------------------------------------------- админ самбар
@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    require_admin(request)
    month = db.this_month()
    with db.connect() as con:
        users = [dict(u) for u in con.execute("SELECT * FROM users WHERE email!=? ORDER BY created_at DESC",
                                              (GUEST_EMAIL,))]
        for u in users:
            u["usage"] = db.monthly_usage(con, u["id"], month)
        totals = dict(con.execute(
            "SELECT COALESCE(SUM(kind='text'),0) texts, COALESCE(SUM(chars),0) chars, "
            "COALESCE(SUM(kind='file'),0) files, COALESCE(SUM(pages),0) pages FROM usage WHERE month=?",
            (month,)).fetchone())
        g = con.execute("SELECT texts, chars FROM guest_usage WHERE month=?", (month,)).fetchone()
        totals["guest_texts"] = g["texts"] if g else 0
        totals["guest_files"] = files_this_month(con, db.guest_user_id(con))
        jobs = [dict(j) for j in con.execute(
            "SELECT j.*, u.email FROM jobs j JOIN users u ON u.id=j.user_id "
            "ORDER BY j.created_at DESC LIMIT 30")]
        orders = [dict(o) for o in con.execute(
            "SELECT o.*, u.email FROM orders o JOIN users u ON u.id=o.user_id ORDER BY "
            "CASE o.status WHEN 'claimed' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, o.created_at DESC LIMIT 200")]
        totals["revenue"] = con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='confirmed' AND substr(confirmed_at,1,7)=?",
            (month,)).fetchone()[0]
        totals["to_check"] = sum(o["status"] == "claimed" for o in orders)
        totals["active_paid"] = sum(1 for u in users if u["plan"] == "paid" and u["paid_until"]
                                    and u["paid_until"] >= date.today().isoformat())
    return page(request, "admin.html", users=users, totals=totals, jobs=jobs, plans=PLANS, orders=orders,
                status_labels=billing.STATUS_LABELS, period_labels=billing.PERIOD_LABELS,
                month=month, today=date.today().isoformat())


@app.post("/admin/users/{user_id}")
def admin_update(request: Request, user_id: int, action: str = Form(...), plan: str = Form(""),
                 note: str = Form(""), months: int = Form(1)):
    me = require_admin(request)
    with db.connect() as con:
        u = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not u:
            raise HTTPException(404)
        if u["id"] == me["id"] and action in ("plan", "toggle"):
            raise HTTPException(400, "Өөрийнхөө эрхийг өөрчлөх боломжгүй.")
        if action == "plan" and plan in PLANS:
            con.execute("UPDATE users SET plan=? WHERE id=?", (plan, user_id))
        elif action == "extend":  # төлбөр хүлээн авсны дараа N сараар сунгах
            active = u["plan"] == "paid" and u["paid_until"] and u["paid_until"] >= date.today().isoformat()
            start = date.fromisoformat(u["paid_until"]) if active else date.today()
            until = start + timedelta(days=30 * max(1, min(months, 24)))
            # Шинээр эхэлж байгаа бол өнөөдрийг эхлэх огноо болгоно, сунгаж байгаа бол хуучныг хадгална
            since = u["paid_since"] if active and u["paid_since"] else date.today().isoformat()
            con.execute("UPDATE users SET plan='paid', paid_until=?, paid_since=? WHERE id=?",
                        (until.isoformat(), since, user_id))
        elif action == "toggle":
            con.execute("UPDATE users SET disabled=1-disabled WHERE id=?", (user_id,))
            con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        elif action == "note":
            con.execute("UPDATE users SET note=? WHERE id=?", (note[:300], user_id))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/users")
def admin_create(request: Request, email: str = Form(...), name: str = Form(""),
                 password: str = Form(...), plan: str = Form("contributor"), note: str = Form("")):
    require_admin(request)
    email = email.strip().lower()
    if plan not in PLANS or len(password) < 8:
        raise HTTPException(400, "Нууц үг 8-аас дээш тэмдэгт байх ёстой.")
    with db.connect() as con:
        if con.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise HTTPException(400, "Энэ имэйл бүртгэлтэй байна.")
        con.execute("INSERT INTO users(email,name,password_hash,plan,note,created_at) VALUES (?,?,?,?,?,?)",
                    (email, name.strip(), auth.hash_password(password), plan, note[:300], db.now_iso()))
    return RedirectResponse("/admin", status_code=303)


import billing  # noqa: E402

billing.register(app, page, current_user, require_admin)

import simple  # noqa: E402  (бүртгэлгүй энгийн орчуулгын хуудас /simple)
simple.register(app, check_direction, templates, queue_file_job)

import public_mode  # noqa: E402  (PUBLIC_SIMPLE=1: интернэтэд зөвхөн /simple)
import contrib  # noqa: E402  (сургалтын дата өгөх /contribute, админ /simple/admin)
contrib.register(app, page, current_user, require_admin, client_ip, set_session_cookie, templates)
public_mode.register(app, client_ip, jobs_q)
if public_mode.ENABLED:
    RESULT_TTL_HOURS = public_mode.RESULT_TTL_HOURS
templates.env.globals["result_ttl"] = RESULT_TTL_HOURS   # footer-т хэрэглэгчид бодит хугацааг харуулна


from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402


@app.exception_handler(translator.Busy)  # GPU дүүрсэн: 500 биш, ойлгомжтой мессеж
def busy_error(request: Request, exc: translator.Busy):
    return JSONResponse({"error": str(exc)}, status_code=503)


@app.exception_handler(Exception)  # бусад гэнэтийн алдаа: хөтөч "Internal Server Error" текстийг JSON гэж уншиж чадахгүй байсан
def any_error(request: Request, exc: Exception):
    import traceback
    traceback.print_exc()
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "Серверт алдаа гарлаа. Дахин оролдоно уу."}, status_code=500)
    return HTMLResponse("Серверт алдаа гарлаа. Дахин оролдоно уу.", status_code=500)


@app.exception_handler(StarletteHTTPException)  # QA bug #3: байхгүй хуудас JSON {"detail"} харуулж байсан
def http_error(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    resp = page(request, "error.html", message=exc.detail)
    resp.status_code = exc.status_code
    return resp
