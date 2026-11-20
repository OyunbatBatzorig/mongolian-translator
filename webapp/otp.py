"""Нэг удаагийн код (OTP): бүртгүүлэх, имэйл солих, нууц үг солиход имэйлээр баталгаажуулна.

- 6 оронтой, 10 минут хүчинтэй, 5 удаа буруу оруулбал хүчингүй болно.
- Кодыг өгөгдлийн санд шууд биш, hash хэлбэрээр хадгална.
- Нэг имэйл рүү минутад нэгээс олон код илгээхгүй.
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import mailer

TTL_MIN = 10
MAX_ATTEMPTS = 5
PURPOSES = {
    "register": "Бүртгэл баталгаажуулах код",
    "email_change": "Имэйл хаяг солих код",
    "password": "Нууц үг солих код",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS otps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT NOT NULL,
    purpose    TEXT NOT NULL,
    code_hash  TEXT NOT NULL,
    data       TEXT NOT NULL DEFAULT '',
    attempts   INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _hash(code):
    return hashlib.sha256(code.encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc)


def send_code(con, email, purpose, data=""):
    """Шинэ код үүсгэж имэйлээр илгээнэ. (амжилттай эсэх, алдааны мессеж) буцаана."""
    recent = con.execute("SELECT created_at FROM otps WHERE email=? AND purpose=? ORDER BY id DESC LIMIT 1",
                         (email, purpose)).fetchone()
    if recent and datetime.fromisoformat(recent["created_at"]) > _now() - timedelta(minutes=1):
        return False, "Код саяхан илгээсэн. 1 минутын дараа дахин оролдоно уу."
    code = f"{secrets.randbelow(10**6):06d}"
    con.execute("DELETE FROM otps WHERE email=? AND purpose=?", (email, purpose))
    con.execute("INSERT INTO otps(email,purpose,code_hash,data,expires_at,created_at) VALUES (?,?,?,?,?,?)",
                (email, purpose, _hash(code), data,
                 (_now() + timedelta(minutes=TTL_MIN)).isoformat(timespec="seconds"),
                 _now().isoformat(timespec="seconds")))
    if not mailer.configured():  # туршилтын горим: SMTP байхгүй үед админ самбарт кодыг харуулахын тулд хадгална
        con.execute("CREATE TABLE IF NOT EXISTS otp_dev (email TEXT, purpose TEXT, code TEXT, data TEXT, created_at TEXT)")
        con.execute("DELETE FROM otp_dev WHERE email=? AND purpose=?", (email, purpose))
        con.execute("INSERT INTO otp_dev VALUES (?,?,?,?,?)", (email, purpose, code, data, _now().isoformat(timespec="seconds")))
    mailer.send(email, f"Хүний эрхийн орчуулагч: {PURPOSES[purpose]}",
                f"Таны код: {code}\n\nКод {TTL_MIN} минутын турш хүчинтэй.\n"
                "Хэрэв та энэ хүсэлтийг илгээгээгүй бол энэ имэйлийг үл тоомсорлоно уу.")
    return True, ""


def check_code(con, email, purpose, code):
    """Зөв бол (True, data), буруу бол (False, алдааны мессеж). Зөв код нэг л удаа ашиглагдана."""
    row = con.execute("SELECT * FROM otps WHERE email=? AND purpose=? ORDER BY id DESC LIMIT 1",
                      (email, purpose)).fetchone()
    if not row:
        return False, "Код олдсонгүй. Шинэ код авна уу."
    if datetime.fromisoformat(row["expires_at"]) < _now():
        return False, "Кодын хугацаа дууссан. Шинэ код авна уу."
    if row["attempts"] >= MAX_ATTEMPTS:
        return False, "Хэт олон удаа буруу оруулсан. Шинэ код авна уу."
    if not hmac.compare_digest(row["code_hash"], _hash((code or "").strip())):
        con.execute("UPDATE otps SET attempts=attempts+1 WHERE id=?", (row["id"],))
        return False, "Код буруу байна."
    con.execute("DELETE FROM otps WHERE id=?", (row["id"],))
    return True, row["data"]
