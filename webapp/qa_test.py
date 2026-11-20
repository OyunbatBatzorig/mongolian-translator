"""End-to-end QA of the web app. Run against a throwaway server:
    APP_DB=/tmp/test.db ../venv/bin/python -m uvicorn main:app --port 8765 > /tmp/qa.log 2>&1 &
    ../venv/bin/python qa_test.py /tmp/qa.log      (needs an admin@test.local / testpass123 user in that DB)
"""
import io
import re
import sys
import time
import zipfile

import httpx

B = "http://localhost:8765"
LOG = sys.argv[1]
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))


def otp(email):
    txt = open(LOG, encoding="utf-8", errors="replace").read()
    codes = re.findall(r"Хэнд: " + re.escape(email) + r"\nГарчиг: [^\n]*\nТаны код: (\d{6})", txt)
    return codes[-1] if codes else None


def client():
    return httpx.Client(base_url=B, follow_redirects=False, timeout=120)


def register(c, email, pw="userpass123", name="QA"):
    c.post("/register", data={"email": email})
    code = otp(email)
    return c.post("/register/verify", data={"email": email, "code": code, "name": name, "password": pw, "password2": pw})


def login(c, email, pw):
    return c.post("/login", data={"email": email, "password": pw})


# ---------------- pages, logged out
g = client()
for path, want in [("/", 200), ("/pricing", 200), ("/login", 200), ("/register", 200), ("/forgot", 200),
                   ("/account", 303), ("/admin", 403), ("/buy", 303), ("/nope", 404), ("/orders/HR-000000", 403),
                   ("/static/style.css", 200), ("/static/app.js", 200)]:
    r = g.get(path)
    check(f"guest GET {path} -> {want}", r.status_code == want, r.status_code)
check("pricing hides e-barimt", "И-баримт" not in g.get("/pricing").text)

# ---------------- translation (guest)
r = g.post("/api/translate", json={"text": "Torture is prohibited."})
check("guest translate works", r.status_code == 200 and r.json().get("translation"), r.text[:100])
check("guest translate returns segments", r.status_code == 200 and len(r.json().get("segments", [])) == 1)
r = g.post("/api/translate", json={"text": "a" * 1201})
check("guest 1201 chars rejected", r.status_code == 400, r.status_code)
r = g.post("/api/translate", json={"text": "   "})
check("empty text rejected", r.status_code == 400, r.status_code)
r = g.post("/api/translate", json={"text": "сайн", "direction": "mn-en"})
check("mn-en direction works", r.status_code == 200 and r.json().get("translation"), r.text[:100])
r = g.post("/api/translate", json={"text": "hi", "direction": "xx-yy"})
check("unknown direction rejected", r.status_code == 400, r.status_code)
r = g.post("/api/translate", content=b"not json", headers={"Content-Type": "application/json"})
check("malformed JSON -> 4xx not 500", 400 <= r.status_code < 500, r.status_code)
r = g.post("/api/translate", json={"text": 123})
check("non-string text -> 4xx not 500", 400 <= r.status_code < 500, r.status_code)
txt = "The SCC is a hybrid court composed of judges\nand staff from abroad. It has jurisdiction."
r = g.post("/api/translate", json={"text": txt})
segs = r.json().get("segments", []) if r.status_code == 200 else []
check("line-wrapped text -> 2 sentences", len(segs) == 2, len(segs))
check("segment offsets map to source", all(txt[s["start"]:s["end"]].strip() for s in segs))

# ---------------- files (guest)
docx = io.BytesIO()
with zipfile.ZipFile(docx, "w") as z:
    z.writestr("word/document.xml", '<w:document xmlns:w="x"><w:body><w:p><w:r><w:t>Everyone has the right to life.</w:t></w:r></w:p></w:body></w:document>')
r = g.post("/api/translate-file", files={"file": ("a.exe", b"MZ", "application/octet-stream")})
check("wrong file type rejected", r.status_code == 400, r.status_code)
r = g.post("/api/translate-file", files={"file": ("bad.pdf", b"not a pdf", "application/pdf")})
check("corrupt pdf -> 400 not 500", r.status_code == 400, r.status_code)
r = g.post("/api/translate-file", files={"file": ("a.docx", docx.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
check("guest docx accepted", r.status_code == 200, r.text[:120])
job = r.json().get("job_id") if r.status_code == 200 else None
if job:
    for _ in range(60):
        j = g.get(f"/api/jobs/{job}").json()
        if j.get("status") in ("done", "failed"):
            break
        time.sleep(1)
    check("guest job completes", j.get("status") == "done", j)
    d = g.get(f"/download/{job}")
    check("guest can download own result", d.status_code == 200 and d.content[:2] == b"PK", d.status_code)
    check("other client cannot see guest job", client().get(f"/api/jobs/{job}").status_code in (403, 404))
r = g.post("/api/translate-file", files={"file": ("b.txt", b"Hello world. Second sentence here.", "text/plain")})
check("guest 2nd file this month rejected", r.status_code == 403, r.status_code)

# ---------------- registration / auth
u = client()
r = u.post("/register", data={"email": "not-an-email"})
check("invalid email rejected", "Зөв имэйл" in r.text)
u.post("/register", data={"email": "qa1@test.local"})
r = u.post("/register/verify", data={"email": "qa1@test.local", "code": "000000", "password": "userpass123", "password2": "userpass123"})
check("wrong OTP rejected", "буруу" in r.text, r.text[:200])
r = u.post("/register/verify", data={"email": "qa1@test.local", "code": otp("qa1@test.local"), "password": "short", "password2": "short"})
check("short password rejected", "8-аас" in r.text)
r = u.post("/register/verify", data={"email": "qa1@test.local", "code": otp("qa1@test.local"), "password": "userpass123", "password2": "different123"})
check("password mismatch rejected", "таарахгүй" in r.text)
r = u.post("/register/verify", data={"email": "qa1@test.local", "code": otp("qa1@test.local"), "name": "<script>alert(1)</script>",
                                     "password": "userpass123", "password2": "userpass123"})
check("register succeeds", r.status_code == 303 and r.headers.get("location", "").endswith("/account"), (r.status_code, r.headers.get("location")))
acc = u.get("/account").text
check("XSS name is escaped", "<script>alert(1)</script>" not in acc and "&lt;script&gt;" in acc)
r = client().post("/register", data={"email": "qa1@test.local"})
check("duplicate email register blocked", "аль хэдийн" in r.text)
r = client().post("/register", data={"email": "QA1@test.local"})
check("duplicate email case-insensitive", "аль хэдийн" in r.text)
r = u.post("/register", data={"email": "qa2@test.local"}); r = u.post("/register", data={"email": "qa2@test.local"})
check("OTP resend rate-limited", "1 минут" in r.text, r.text[:300])
r = login(client(), "qa1@test.local", "wrongpass")
check("wrong password login rejected", "буруу" in r.text)
r = login(client(), "qa1@test.local", "userpass123")
check("login OK", r.status_code == 303)
r = client().post("/login", data={"email": "qa1@test.local", "password": "userpass123", "next": "//evil.com"})
check("open redirect blocked", r.headers.get("location", "") in ("/", "http://localhost:8765/"), r.headers.get("location"))
r = client().post("/login", data={"email": "qa1@test.local", "password": "userpass123", "next": "https://evil.com"})
check("absolute-URL redirect blocked", "evil" not in r.headers.get("location", ""), r.headers.get("location"))

# trial user: 5000 chars, 2 files
r = u.post("/api/translate", json={"text": "Hello. " * 300})
check("trial user 2100 chars OK", r.status_code == 200, r.status_code)

# ---------------- account changes
r = u.post("/account/email", data={"new_email": "qa1@test.local"})
check("change email to own/existing blocked", "err=" in r.headers.get("location", ""), r.headers.get("location"))
u.post("/account/email", data={"new_email": "qa1new@test.local"})
r = u.post("/account/email/verify", data={"new_email": "qa1new@test.local", "code": otp("qa1new@test.local")})
check("email change works", "msg=" in r.headers.get("location", ""), r.headers.get("location"))
other = client(); register(other, "qa3@test.local")
r = other.post("/account/email/verify", data={"new_email": "qa1new@test.local", "code": "123456"})
check("cannot hijack another's email OTP", "msg=" not in r.headers.get("location", ""))

# ---------------- purchase flow
r = u.post("/buy", data={"period": "monthly", "customer_type": "person", "contact_name": "QA", "phone": "99", "method": "bank"})
oid = r.headers.get("location", "").rsplit("/", 1)[-1]
check("order created", oid.startswith("HR-"), r.headers.get("location"))
check("order page hides e-barimt", "И-баримт" not in u.get(f"/orders/{oid}").text)
check("other user cannot view order", other.get(f"/orders/{oid}").status_code == 404)
check("other user cannot claim order", other.post(f"/orders/{oid}/claim").status_code == 404)
r = u.post("/buy", data={"period": "weekly", "customer_type": "person", "contact_name": "QA", "phone": "99", "method": "bank"})
check("invalid period rejected", "шалгана" in r.text, r.status_code)
r = u.post("/buy", data={"period": "monthly", "customer_type": "person", "contact_name": "", "phone": "", "method": "bank"})
check("missing contact rejected", "утасны" in r.text)
r = u.post("/buy", data={"period": "monthly", "customer_type": "person", "contact_name": "QA", "phone": "99", "method": "bank", "ebarimt": "1"})
o2 = r.headers.get("location", "").rsplit("/", 1)[-1]
u.post(f"/orders/{oid}/claim")
check("non-admin cannot confirm order", u.post(f"/admin/orders/{oid}", data={"action": "confirm"}).status_code == 403)

# ---------------- admin
a = client()
check("admin login", login(a, "admin@test.local", "testpass123").status_code == 303)
ad = a.get("/admin").text
check("admin sees claimed order", oid in ad)
check("admin hides e-barimt tag", "И-баримт</i>" not in ad)
a.post(f"/admin/orders/{oid}", data={"action": "confirm"})
acc = u.get("/account").text
check("confirmed order -> paid plan", "Төлбөртэй" in acc, re.findall(r'badge plan-\w+">[^<]*', acc))
r = u.post("/api/translate", json={"text": "Hello. " * 700})
check("paid 4900 chars OK", r.status_code == 200, r.status_code)
r = u.post("/api/translate", json={"text": "Hello. " * 800})
check("paid 5600 chars rejected", r.status_code == 400, r.status_code)
r = a.post(f"/admin/orders/{oid}", data={"action": "confirm"})
acc2 = u.get("/account").text
until1 = re.search(r"Эрх дуусах огноо</dt><dd>([0-9-]+)", acc); until2 = re.search(r"Эрх дуусах огноо</dt><dd>([0-9-]+)", acc2)
check("double confirm does not double-extend", until1 and until2 and until1.group(1) == until2.group(1), (until1 and until1.group(1), until2 and until2.group(1)))
me = re.search(r'/admin/users/(\d+)"', ad)
admin_id = None
for m in re.finditer(r'<b>Админ</b><br><small>admin@test.local', ad):
    admin_id = True
r = a.post("/admin/users", data={"email": "qa1new@test.local", "password": "12345678", "plan": "contributor"})
check("admin create duplicate user -> 400", r.status_code == 400, r.status_code)
r = a.post("/admin/users", data={"email": "c1@test.local", "password": "12345678", "plan": "nonsense"})
check("admin create bad plan -> 400", r.status_code == 400, r.status_code)
r = a.post("/admin/users/99999", data={"action": "note", "note": "x"})
check("admin update missing user -> 404", r.status_code == 404, r.status_code)
uid = re.search(r'/admin/users/(\d+)"[^>]*>\s*<input type="hidden" name="action" value="plan">', a.get("/admin").text)
# disable user -> session killed
ids = re.findall(r'action="/admin/users/(\d+)"', a.get("/admin").text)
users_html = a.get("/admin").text.split('id="users-table"')[1]
qa1_row = re.search(r'qa1new@test.local.*?action="/admin/users/(\d+)"', users_html, re.S)
if qa1_row:
    a.post(f"/admin/users/{qa1_row.group(1)}", data={"action": "toggle"})
    check("disabled user logged out", u.get("/account").status_code == 303, qa1_row.group(1))
    check("disabled user cannot log in", "буруу" in login(client(), "qa1new@test.local", "userpass123").text)
    a.post(f"/admin/users/{qa1_row.group(1)}", data={"action": "toggle"})
# admin cannot demote self
self_row = re.search(r'admin@test.local.*?action="/admin/users/(\d+)"', a.get("/admin").text.split('id="users-table"')[1], re.S)
if self_row:
    check("admin cannot demote self", a.post(f"/admin/users/{self_row.group(1)}", data={"action": "plan", "plan": "unpaid"}).status_code == 400)
r = a.post("/admin/users/1", data={"action": "extend", "months": "abc"})
check("extend with non-number months -> 4xx not 500", 400 <= r.status_code < 500 or r.status_code == 303, r.status_code)

# forgot password for unknown email: no leak
r = client().post("/forgot", data={"email": "nobody@test.local"})
check("forgot unknown email does not leak", "байгаа бол" in r.text)

fails = [x for x in results if not x[1]]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
for f in fails:
    print("FAILED:", f[0], f[2])
