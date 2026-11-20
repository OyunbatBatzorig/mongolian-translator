"""Аюулгүй байдлын QA (2026-11-12): хор хөнөөлтэй файл, бусдын датад хандах, XSS, толгой мэдээлэл.
    cd webapp && ../venv/bin/python qa_security.py <халдлагын файлуудын хавтас>
Түр өгөгдлийн сан ашиглана; жинхэнэ датад хүрэхгүй."""
import io, json, os, resource, shutil, sys, tempfile, zipfile
ATK = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else None
TMP = tempfile.mkdtemp(prefix="qa_sec_")
os.environ["APP_DB"] = os.path.join(TMP, "app.db"); os.environ.pop("SMTP_HOST", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db, translator, contrib, documents  # noqa: E402
contrib.DB = os.path.join(TMP, "contrib.db"); contrib.UP = os.path.join(TMP, "uploads"); contrib.JOBS = os.path.join(TMP, "runs")
translator.ACTIVE_FILE = os.path.join(TMP, "a.json")
contrib.start_processing = lambda cid: None
translator.available_directions = lambda: {"en-mn": True, "mn-en": True}
import main  # noqa: E402
main.UPLOAD_DIR = os.path.join(TMP, "up_tr")
from fastapi.testclient import TestClient  # noqa: E402
db.init()
R = []
def check(n, c, info=""):
    R.append((n, bool(c))); print(("PASS " if c else "FAIL ") + n + (f"  [{info}]" if info and not c else ""), flush=True)
rss = lambda: resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
c = TestClient(main.app)
base = {"name": "Attacker", "email": "atk@example.com", "org": "", "consent": "yes"}
TXT_EN = ("file_en", ("en.txt", b"Everyone has the right to life, liberty and security of person.", "text/plain"))
TXT_MN = ("file_mn", ("mn.txt", "Хүн бүр амьд явах эрхтэй.".encode(), "text/plain"))

# 1. ZIP bomb (олон файлын горим)
if ATK:
    m0 = rss()
    r = c.post("/api/contribute/start", data={**base, "mode": "many"}, files=[("files_many", ("bomb.zip", open(f"{ATK}/many_bomb.zip", "rb").read(), "application/zip")), ("files_many", ("x.txt", b"hello world text", "text/plain"))])
    check("zip bomb rejected (400)", r.status_code == 400, r.text[:150])
    check("zip bomb: memory stayed low (<200 MB growth)", rss() - m0 < 200, f"{rss() - m0} MB")
    # 2. DOCX bomb
    try:
        documents.zip_read(f"{ATK}/bomb.docx", "word/document.xml"); ok = False
    except ValueError:
        ok = True
    check("docx bomb: capped reader refuses", ok)
    m0 = rss()
    r = c.post("/api/simple/translate-file", data={"direction": "en-mn"}, files={"file": ("bomb.docx", open(f"{ATK}/bomb.docx", "rb").read(), "application/octet-stream")})
    check("docx bomb: translate upload rejected", r.status_code == 400, r.text[:150])
    check("docx bomb: server memory stayed low", rss() - m0 < 200, f"{rss() - m0} MB")
    # 3. ZIP path traversal
    r = c.post("/api/contribute/start", data={**base, "email": "trav@example.com", "mode": "many"}, files=[("files_many", ("t.zip", open(f"{ATK}/trav.zip", "rb").read(), "application/zip"))])
    check("zip traversal: nothing written outside", not os.path.exists("/tmp/pwned_by_zip.txt") and not os.path.exists("/etc/evil.txt"))
    if r.status_code == 200:
        files = os.listdir(os.path.join(contrib.UP, r.json()["id"]))
        check("zip traversal: files stored under safe names", all(f.startswith("f") for f in files), files)
r = c.post("/api/simple/translate-file", data={"direction": "en-mn"}, files={"file": ("fake.pdf", os.urandom(5000), "application/pdf")})
check("garbage PDF rejected (400, not 500)", r.status_code == 400, r.status_code)
r = c.post("/api/simple/translate-file", data={"direction": "en-mn"}, files={"file": ("evil.html", b"<script>alert(1)</script>", "text/html")})
check("HTML upload rejected", r.status_code == 400)
# 4. XSS / injection in email & name
r = c.post("/api/contribute/start", data={**base, "email": "x');alert(1);//@a.bc", "mode": "two"}, files=[TXT_EN, TXT_MN])
check("XSS email rejected", r.status_code == 400)
r = c.post("/api/contribute/start", data={**base, "email": '"><svg onload=alert(1)>@a.bc', "mode": "two"}, files=[TXT_EN, TXT_MN])
check("XSS email (svg) rejected", r.status_code == 400)
r = c.post("/api/register", data={"email": "a'b@c.de", "password": "abcdefgh1"}) if any(getattr(rt, "path", "") == "/api/register" for rt in main.app.routes) else None
# 5. verify with junk JSON
check("verify: non-object JSON -> 400", c.post("/api/contribute/verify", content=b"[1,2]", headers={"Content-Type": "application/json"}).status_code == 400)
check("verify: invalid JSON -> 400", c.post("/api/contribute/verify", content=b"{bad", headers={"Content-Type": "application/json"}).status_code == 400)
# 6. headers
h = c.get("/simple").headers
check("headers: CSP frame-ancestors none", "frame-ancestors 'none'" in h.get("content-security-policy", ""))
check("headers: X-Frame-Options DENY + nosniff", h.get("x-frame-options") == "DENY" and h.get("x-content-type-options") == "nosniff")
check("headers: contribute page has CSP", "default-src 'self'" in c.get("/contribute").headers.get("content-security-policy", ""))
# 7. per-email cap
with contrib.connect() as con:
    for i in range(5):
        con.execute("INSERT INTO contributions(id,name,email,org,mode,files,consent_at,ip,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (f"cap{i}", "x", "victim@example.com", "", "two", "[]", contrib._now(), "1.1.1.1", "unverified", contrib._now()))
r = c.post("/api/contribute/start", data={**base, "email": "victim@example.com", "mode": "two"}, files=[TXT_EN, TXT_MN])
check("email bombing: 6th per hour -> 429", r.status_code == 429, r.status_code)
# 8. unverified cleanup
os.makedirs(os.path.join(contrib.UP, "oldone"), exist_ok=True); os.makedirs(os.path.join(contrib.UP, "legacy"), exist_ok=True)
with contrib.connect() as con:
    con.execute("UPDATE contributions SET created_at='2099-01-01T00:00:00+00:00' WHERE id LIKE 'cap%'")
    con.execute("INSERT INTO contributions(id,name,email,org,mode,files,consent_at,ip,status,created_at) VALUES ('oldone','x','o@example.com','','two','[]','x','x','unverified',?)",
                ((__import__('datetime').datetime.now(__import__('datetime').timezone.utc) - __import__('datetime').timedelta(hours=30)).isoformat(timespec="seconds"),))
    con.execute("INSERT INTO contributions(id,name,email,org,mode,files,consent_at,ip,status,created_at) VALUES ('legacy','x','o@example.com','','two','[]','x','x','unverified',?)",
                ((__import__('datetime').datetime.now(__import__('datetime').timezone.utc) - __import__('datetime').timedelta(hours=60)).isoformat(timespec="seconds"),))
_cf = contrib.CLEANUP_FROM
contrib.CLEANUP_FROM = (__import__('datetime').datetime.now(__import__('datetime').timezone.utc) - __import__('datetime').timedelta(hours=40)).isoformat(timespec="seconds")
contrib.cleanup_unverified()
contrib.CLEANUP_FROM = _cf
with contrib.connect() as con:
    ids = {r[0] for r in con.execute("SELECT id FROM contributions")}
check("cleanup: stale unverified upload deleted", "oldone" not in ids and not os.path.exists(os.path.join(contrib.UP, "oldone")))
check("cleanup: your earlier test upload kept", "legacy" in ids and os.path.exists(os.path.join(contrib.UP, "legacy")))
# 9. other people's translation jobs
a, b = TestClient(main.app), TestClient(main.app)
r = a.post("/api/simple/translate-file", data={"direction": "en-mn"}, files={"file": ("my.txt", b"My private letter about my case.", "text/plain")})
jid = r.json().get("job_id") if r.status_code == 200 else None
check("job: owner can see own job", jid and a.get(f"/api/jobs/{jid}").status_code == 200, r.text[:100])
check("job: stranger can't see it", jid and b.get(f"/api/jobs/{jid}").status_code == 404)
check("job: stranger can't download it", jid and b.get(f"/download/{jid}").status_code == 404)
check("job: stranger can't cancel it", jid and b.post(f"/api/jobs/{jid}/cancel").status_code == 404)
check("job: stranger can't zip it", jid and b.get(f"/download-zip?ids={jid}").status_code == 404)
# 10. admin API without login
for u, d in [("/simple/admin/decide", {"cid": "x", "action": "approve"}), ("/simple/admin/train", {"directions": "en-mn"}),
             ("/simple/admin/deploy", {"direction": "en-mn", "path": "x"}), ("/simple/admin/admins/add", {"email": "e@e.ee", "password": "12345678"}),
             ("/simple/admin/settings/require2fa", {"value": "0"}), ("/simple/admin/admins/reset2fa", {"email": "e@e.ee"})]:
    check(f"no login: POST {u} -> 403", c.post(u, data=d).status_code == 403)
check("no login: sample API -> 403", c.get("/simple/admin/sample?cid=x").status_code == 403)
c.cookies.set("sa", "forged-token")
check("forged admin cookie rejected", c.get("/simple/admin", follow_redirects=False).status_code == 303)
# 11. PII filter
import re
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "contrib_process.py")).read()
ns = {"re": re}; exec(src[src.index("PII = "):src.index("def too_short")], ns)
check("PII: email/phone/register caught", all(ns["PII"].search(t) for t in ["info@amnesty.org", "9911 2233", "+976 11 312345", "УБ12345678"]))
check("PII: years/amounts kept", not any(ns["PII"].search(t) for t in ["In 2016-2020 it grew.", "3,500,000 people", "Article 25.1", "95 000 000 MNT"]))
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{sum(ok for _, ok in R)}/{len(R)} passed")
sys.exit(0 if all(ok for _, ok in R) else 1)
