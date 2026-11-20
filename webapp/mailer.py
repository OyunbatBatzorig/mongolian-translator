"""Имэйл илгээх (OTP код).

Жинхэнэ имэйл илгээхийн тулд серверийг эдгээр орчны хувьсагчтай асаана:
    SMTP_HOST=smtp.gmail.com SMTP_PORT=587 SMTP_USER=таны@gmail.com \
    SMTP_PASSWORD=<Gmail "App password"> SMTP_FROM=таны@gmail.com \
    ../venv/bin/python -m uvicorn main:app --port 8000

SMTP тохируулаагүй үед (одоо, өөрийн компьютер дээр туршиж байхад) имэйл илгээхгүй,
харин кодыг серверийн терминал дээр хэвлэнэ. Ингэснээр имэйл сервергүйгээр туршиж болно.
"""
import os
import smtplib
from email.message import EmailMessage


def configured():
    return bool(os.environ.get("SMTP_HOST"))


def send(to, subject, body):
    if not configured():
        print(f"\n===== [ИМЭЙЛ, туршилтын горим] =====\nХэнд: {to}\nГарчиг: {subject}\n{body}\n"
              "====================================\n", flush=True)
        return
    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", ""))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=20) as s:
        s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)
