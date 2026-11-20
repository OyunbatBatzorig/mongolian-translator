"""Анхны админ хэрэглэгч үүсгэх (нэг л удаа ажиллуулна):
    cd webapp && ../venv/bin/python create_admin.py you@example.com
Нууц үгийг дэлгэц дээр харуулахгүйгээр асууна.
"""
import getpass
import sys

import auth
import db

email = (sys.argv[1] if len(sys.argv) > 1 else input("Имэйл: ")).strip().lower()
password = getpass.getpass("Нууц үг (8+ тэмдэгт): ")
if len(password) < 8:
    sys.exit("Нууц үг хэт богино.")
db.init()
with db.connect() as con:
    row = con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if row:
        con.execute("UPDATE users SET plan='admin', password_hash=? WHERE id=?", (auth.hash_password(password), row["id"]))
    else:
        con.execute("INSERT INTO users(email,name,password_hash,plan,created_at) VALUES (?,?,?,?,?)",
                    (email, "Админ", auth.hash_password(password), "admin", db.now_iso()))
print(f"{email} одоо админ эрхтэй.")
