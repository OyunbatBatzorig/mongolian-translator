"""SQLite өгөгдлийн сан. Python-д суурилагдсан тул нэмэлт программ хэрэггүй.

Нууцлал: хэрэглэгчийн орчуулсан ТЕКСТИЙГ ХАДГАЛАХГҮЙ. Зөвхөн хэдэн тэмдэгт,
хэдэн хуудас орчуулсныг тоолно (хязгаар хянахад хэрэгтэй).
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    plan          TEXT NOT NULL DEFAULT 'unpaid',
    paid_until    TEXT,                -- ISO огноо, зөвхөн 'paid' эрхэд
    disabled      INTEGER NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT '',  -- админы тэмдэглэл (жишээ нь: "NHRCM датасет өгсөн")
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL
);
-- Хэрэглээний бүртгэл: текст биш, зөвхөн тоо.
CREATE TABLE IF NOT EXISTS usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    kind       TEXT NOT NULL,          -- 'text' эсвэл 'file'
    direction  TEXT NOT NULL,
    chars      INTEGER NOT NULL DEFAULT 0,
    pages      INTEGER NOT NULL DEFAULT 0,
    month      TEXT NOT NULL,          -- '2026-11' гэх мэт, сарын хязгаарт
    created_at TEXT NOT NULL
);
-- Файл орчуулга удаан явдаг тул арын ажил (job) болгож хийнэ.
-- Нэвтрээгүй зочдын хэрэглээ: хэн гэдгийг биш, зөвхөн сарын нийт тоог хадгална.
CREATE TABLE IF NOT EXISTS guest_usage (
    month TEXT PRIMARY KEY,
    texts INTEGER NOT NULL DEFAULT 0,
    chars INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    filename    TEXT NOT NULL,
    direction   TEXT NOT NULL,
    pages       INTEGER NOT NULL,
    status      TEXT NOT NULL,         -- queued / running / done / failed / cancelled
    progress    REAL NOT NULL DEFAULT 0,
    error       TEXT,
    result_path TEXT,
    created_at  TEXT NOT NULL
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def this_month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


@contextmanager
def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    import secrets
    from config import GUEST_EMAIL
    import otp
    with connect() as con:
        con.executescript(SCHEMA)
        con.executescript(otp.SCHEMA)
        import billing
        con.executescript(billing.SCHEMA)
        # Хуучин өгөгдлийн санд шинэ баганууд нэмэх (migration)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(jobs)")}
        if "guest_key" not in cols:
            con.execute("ALTER TABLE jobs ADD COLUMN guest_key TEXT")
        if "guest_ip" not in cols:
            con.execute("ALTER TABLE jobs ADD COLUMN guest_ip TEXT")
        ucols = {r["name"] for r in con.execute("PRAGMA table_info(users)")}
        if "paid_since" not in ucols:  # одоогийн төлбөртэй хугацаа хэзээ эхэлсэн
            con.execute("ALTER TABLE users ADD COLUMN paid_since TEXT")
        # Бүртгэлгүй зочдын файлыг холбох дотоод бүртгэл: disabled=1 тул хэн ч нэвтэрч чадахгүй
        if not con.execute("SELECT 1 FROM users WHERE email=?", (GUEST_EMAIL,)).fetchone():
            con.execute("INSERT INTO users(email,name,password_hash,plan,disabled,created_at) "
                        "VALUES (?,?,?,?,1,?)", (GUEST_EMAIL, "Зочин", "!" + secrets.token_hex(16),
                                                 "guest", now_iso()))


def guest_user_id(con):
    from config import GUEST_EMAIL
    return con.execute("SELECT id FROM users WHERE email=?", (GUEST_EMAIL,)).fetchone()["id"]


def monthly_usage(con, user_id, month=None):
    month = month or this_month()
    row = con.execute(
        "SELECT COALESCE(SUM(kind='file'),0) AS files, COALESCE(SUM(pages),0) AS pages, "
        "COALESCE(SUM(kind='text'),0) AS texts, COALESCE(SUM(chars),0) AS chars "
        "FROM usage WHERE user_id=? AND month=?", (user_id, month)).fetchone()
    return dict(row)
