"""Нууц үг болон нэвтрэлт (session).

Нууц үгийг хэзээ ч шууд хадгалдаггүй: PBKDF2-SHA256 (200,000 давталт, санамсаргүй
salt)-аар хувиргаж хадгална. Нэвтэрсэн хүнд санамсаргүй token өгч cookie-д хийнэ.
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone  # noqa: F401

from config import SESSION_DAYS
import db

ITERATIONS = 200_000
COOKIE = "session"


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS).hex()
    return f"pbkdf2${ITERATIONS}${salt}${digest}"


def check_password(password, stored):
    try:
        _, it, salt, digest = stored.split("$")
    except ValueError:
        return False
    test = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(it)).hex()
    return hmac.compare_digest(test, digest)  # цаг хугацаагаар таах халдлагаас хамгаална


def create_session(con, user_id):
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    con.execute("INSERT INTO sessions(token, user_id, expires_at) VALUES (?,?,?)",
                (token, user_id, expires.isoformat(timespec="seconds")))
    return token


def user_from_token(con, token):
    if not token:
        return None
    row = con.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token=? AND s.expires_at > ? AND u.disabled = 0",
        (token, db.now_iso())).fetchone()
    return dict(row) if row else None


def delete_session(con, token):
    con.execute("DELETE FROM sessions WHERE token=?", (token,))


def effective_plan(user):
    """Зочин -> 'guest'. Төлбөрийн хугацаа дууссан 'paid' хэрэглэгчийг 'unpaid' гэж үзнэ."""
    if user is None:
        return "guest"
    if user["plan"] == "paid" and not (
            user["paid_until"] and user["paid_until"] >= datetime.now(timezone.utc).date().isoformat()):
        return "unpaid"
    return user["plan"]


def trial_until(user):
    """Бүртгүүлсэн үнэгүй хэрэглэгчийн 5000 тэмдэгтийн туршилт дуусах огноо, эсвэл None."""
    from config import TRIAL_DAYS
    if user is None or effective_plan(user) != "unpaid":
        return None
    end = datetime.fromisoformat(user["created_at"]) + timedelta(days=TRIAL_DAYS)
    return end if end > datetime.now(timezone.utc) else None


def text_limit(user):
    from config import TEXT_LIMITS
    if trial_until(user):
        return TEXT_LIMITS["trial"]
    return TEXT_LIMITS.get(effective_plan(user), TEXT_LIMITS["guest"])


def has_access(user):
    """Текст орчуулах эрх: хүн бүр, нэвтрээгүй зочин ч гэсэн (хязгаар нь эрхээс хамаарна)."""
    return True, ""


def file_quota(user):
    """Сард орчуулж болох файлын тоо (None = хязгааргүй)."""
    from config import FILES_PER_MONTH
    return FILES_PER_MONTH.get(effective_plan(user), FILES_PER_MONTH["guest"])


def quota_message(user, quota):
    """Сарын файлын эрх дуусахад харуулах тайлбар: дараагийн шат юу өгөхийг хэлнэ."""
    from config import FILES_PER_MONTH, TEXT_LIMITS, TRIAL_DAYS
    plan = effective_plan(user)
    msg = f"Энэ сарын файл орчуулах эрх ({quota}) дууссан байна."
    if plan == "guest":
        return msg + (f" Үнэгүй бүртгүүлбэл +1 файл болон {TRIAL_DAYS} хоногийн турш "
                      f"{TEXT_LIMITS['trial']} тэмдэгтийн эрх нэмэгдэнэ.")
    if plan == "unpaid":
        return msg + f" Төлбөр төлбөл сард {FILES_PER_MONTH['paid']} файл орчуулна."
    return msg + " Дараа сард дахин нээгдэнэ."


def has_file_access(user):
    """Файл орчуулах эрх: хүн бүр, зочин ч гэсэн (тоо нь эрхээс хамаарна)."""
    return True, ""
