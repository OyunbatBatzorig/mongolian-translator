"""QA: /simple, /contribute, /simple/admin (2026-11-12).
    cd webapp && ../venv/bin/python qa_contrib.py
Түр өгөгдлийн сан, түр contrib.db ашиглана (жинхэнэ датад хүрэхгүй). Орчуулгын загварыг хуурамчаар солино
(GPU ашиглахгүй); бодит загварын шалгалтыг ажиллаж буй серверээр (localhost:8000) хийнэ.
"""
import io, json, os, re, shutil, subprocess, sys, tempfile, time, zipfile
TMP = tempfile.mkdtemp(prefix="qa_contrib_")
os.environ["APP_DB"] = os.path.join(TMP, "app.db")
for k in ("SMTP_HOST",):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db, auth, translator  # noqa: E402
import contrib  # noqa: E402
contrib.DB = os.path.join(TMP, "contrib.db"); contrib.UP = os.path.join(TMP, "uploads"); contrib.JOBS = os.path.join(TMP, "runs")
translator.ACTIVE_FILE = os.path.join(TMP, "active_models.json")
contrib.start_processing = lambda cid: None          # тэгшлэлийг доор тусад нь шалгана
translator.translate_segments = lambda text, d: [{"start": 0, "end": len(text), "para": 0, "translation": "ОРЧУУЛГА"}]
translator.translate_paragraphs = lambda paras, d, *a, **k: ["ОРЧУУЛГА" for _ in paras]
translator.available_directions = lambda: {"en-mn": True, "mn-en": True}
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

db.init()
results = []


def check(name, cond, info=""):
    results.append((name, bool(cond), info))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info and not cond else ""), flush=True)


def code_for(cid):
    with db.connect() as con:
        r = con.execute("SELECT code FROM otp_dev WHERE purpose='contribute' AND data=?", (cid,)).fetchone()
    return r["code"] if r else None


with db.connect() as con:
    con.execute("INSERT INTO users(email,name,password_hash,plan,disabled,created_at) VALUES (?,?,?,?,0,?)",
                ("qa-admin@example.com", "QA admin", auth.hash_password("qa-admin-pass-1"), "admin", db.now_iso()))
    con.execute("INSERT INTO users(email,name,password_hash,plan,disabled,created_at) VALUES (?,?,?,?,0,?)",
                ("qa-user@example.com", "QA user", auth.hash_password("qa-user-pass-1"), "paid", db.now_iso()))

c = TestClient(main.app)
TXT_EN = b"Everyone has the right to life, liberty and security of person.\nNo one shall be held in slavery or servitude."
TXT_MN = "Хүн бүр амьд явах, эрх чөлөөтэй байх, халдашгүй байх эрхтэй.\nХэнийг ч боолчлох буюу хүлцэнгүй байдалд байлгаж болохгүй.".encode()

# ---------------------------------------------------------------- SIMPLE
r = c.get("/simple"); check("simple: page 200", r.status_code == 200)
check("simple: contribute button", "/contribute" in r.text and "contrib-btn" in r.text)
check("simple: 5000 char limit", 'maxlength="5000"' in r.text)
check("simple: no paid nav", "/pricing" not in r.text and "/login" not in r.text)
check("simple: translate ok", c.post("/api/simple/translate", json={"text": "Hello.", "direction": "en-mn"}).json().get("translation") == "ОРЧУУЛГА")
check("simple: empty -> 400", c.post("/api/simple/translate", json={"text": "  ", "direction": "en-mn"}).status_code == 400)
check("simple: >5000 -> 400", c.post("/api/simple/translate", json={"text": "a" * 5001, "direction": "en-mn"}).status_code == 400)
check("simple: bad direction -> 400", c.post("/api/simple/translate", json={"text": "Hi", "direction": "xx-yy"}).status_code == 400)
check("simple: non-string -> 400", c.post("/api/simple/translate", json={"text": 5, "direction": "en-mn"}).status_code in (400, 422))
r = c.post("/api/simple/translate-file", files={"file": ("a.exe", b"MZ", "application/octet-stream")}, data={"direction": "en-mn"})
check("simple: bad file type -> 400", r.status_code == 400)
r = c.post("/api/simple/translate-file", files={"file": ("a.txt", TXT_EN, "text/plain")}, data={"direction": "en-mn"})
check("simple: file job created", r.status_code == 200 and "job_id" in r.json(), r.text[:120])

# ---------------------------------------------------------------- CONTRIBUTE
r = c.get("/contribute"); check("contribute: page 200", r.status_code == 200)
check("contribute: plain-language 85% text", "85%" in r.text and "LaBSE" not in r.text.split("<main")[1].split("</p>")[0])
check("contribute: consent required attr", 'name="consent" value="yes" required' in r.text)
base = {"name": "QA Tester", "email": "qa1@example.com", "org": "QA", "mode": "two", "consent": "yes"}
two = {"file_en": ("en.txt", TXT_EN, "text/plain"), "file_mn": ("mn.txt", TXT_MN, "text/plain")}
check("contribute: no consent -> 400", c.post("/api/contribute/start", data={**base, "consent": ""}, files=two).status_code == 400)
check("contribute: bad email -> 400", c.post("/api/contribute/start", data={**base, "email": "nope"}, files=two).status_code == 400)
check("contribute: empty name -> 400", c.post("/api/contribute/start", data={**base, "name": "  "}, files=two).status_code == 400)
check("contribute: missing file -> 400", c.post("/api/contribute/start", data=base, files={"file_en": two["file_en"]}).status_code == 400)
check("contribute: bad mode -> 400", c.post("/api/contribute/start", data={**base, "mode": "zzz"}, files=two).status_code == 400)
r = c.post("/api/contribute/start", data=base, files={"file_en": ("x.exe", b"MZ", "application/x"), "file_mn": two["file_mn"]})
check("contribute: bad extension -> 400", r.status_code == 400)
r = c.post("/api/contribute/start", data={**base, "email": "big@example.com"},
           files={"file_en": ("big.txt", b"a" * (51 * 1024 * 1024), "text/plain"), "file_mn": two["file_mn"]})
check("contribute: >50MB -> 400", r.status_code == 400)
check("contribute: hint lists ZIP+folder, 50 MB", "ZIP болон бүхэл хавтас" in c.get("/contribute").text and "50 MB" in c.get("/contribute").text)
r = c.post("/api/contribute/start", data=base, files=two)
check("contribute: start ok", r.status_code == 200 and "id" in r.json(), r.text[:120])
cid = r.json().get("id")
check("contribute: resend <1 min -> 429", c.post("/api/contribute/start", data=base, files=two).status_code == 429)
check("contribute: verify unknown id -> 404", c.post("/api/contribute/verify", json={"id": "nope", "code": "000000"}).status_code == 404)
check("contribute: verify wrong code -> 400", c.post("/api/contribute/verify", json={"id": cid, "code": "000000"}).status_code == 400)
code = code_for(cid)
check("contribute: code stored for admin (test mode)", bool(code and re.fullmatch(r"\d{6}", code)))
r = c.post("/api/contribute/verify", json={"id": cid, "code": code})
check("contribute: verify ok", r.status_code == 200 and r.json().get("ok"), r.text[:120])
check("contribute: double verify -> 404", c.post("/api/contribute/verify", json={"id": cid, "code": code}).status_code == 404)
# 5 удаа буруу -> түгжигдэх
r = c.post("/api/contribute/start", data={**base, "email": "lock@example.com"}, files=two); lid = r.json()["id"]
for _ in range(5):
    c.post("/api/contribute/verify", json={"id": lid, "code": "111111"})
r = c.post("/api/contribute/verify", json={"id": lid, "code": code_for(lid)})
check("contribute: locked after 5 wrong codes", r.status_code == 400)
# XSS нэр
r = c.post("/api/contribute/start", data={**base, "email": "xss@example.com", "name": "<script>alert(1)</script>"}, files=two)
xid = r.json()["id"]; c.post("/api/contribute/verify", json={"id": xid, "code": code_for(xid)})
with contrib.connect() as con:
    con.execute("UPDATE contributions SET status='ready', n_pairs=1, mean_sim=0.9 WHERE id IN (?,?)", (cid, xid))
    con.execute("INSERT INTO contrib_pairs(contribution_id,english,mongolian,labse_sim) VALUES (?,?,?,?)",
                (cid, "No one shall be held in slavery.", "Хэнийг ч боолчлохгүй.", 0.9))

# ---------------------------------------------------------------- ADMIN
a = TestClient(main.app)
r = a.get("/simple/admin", follow_redirects=False)
check("admin: no login -> redirect to login", r.status_code == 303 and r.headers["location"] == "/simple/admin/login")
check("admin: login page 200", a.get("/simple/admin/login").status_code == 200)
check("admin: wrong password -> 401", a.post("/simple/admin/login", data={"email": "qa-admin@example.com", "password": "x"}).status_code == 401)
check("admin: non-admin user -> 401", a.post("/simple/admin/login", data={"email": "qa-user@example.com", "password": "qa-user-pass-1"}).status_code == 401)
check("admin: decide w/o login -> 403", a.post("/simple/admin/decide", data={"cid": cid, "action": "approve"}).status_code == 403)
r = a.post("/simple/admin/login", data={"email": "qa-admin@example.com", "password": "qa-admin-pass-1"}, follow_redirects=False)
check("admin: login ok", r.status_code == 303)
check("nav: back-to-admin on /simple + /contribute when logged in", "← Админ самбар" in a.get("/simple").text and "← Админ самбар" in a.get("/contribute").text)
check("nav: no admin button for visitors", "← Админ самбар" not in c.get("/simple").text and "← Админ самбар" not in c.get("/contribute").text)
r = a.get("/simple/admin")
check("admin: dashboard 200", r.status_code == 200)
check("admin: separate from paid admin (no orders tab)", "Захиалга" not in r.text and "/pricing" not in r.text)
check("admin: pending code table (test mode)", "туршилтын горим" in r.text)
check("admin: lists contribution", "QA Tester" in r.text)
check("admin: XSS escaped", "<script>alert(1)</script>" not in r.text and "&lt;script&gt;" in r.text)
r = a.get(f"/simple/admin?view={cid}")
check("admin: sample view shows pairs", "Хэнийг ч боолчлохгүй." in r.text)
j = a.get(f"/simple/admin/sample?cid={cid}").json()
check("popup: sample json has pairs", j["pairs"] and j["pairs"][0]["mongolian"] == "Хэнийг ч боолчлохгүй.", j)
check("popup: sample needs login", TestClient(main.app).get(f"/simple/admin/sample?cid={cid}").status_code == 403)
check("popup: unknown id -> 404", a.get("/simple/admin/sample?cid=nope").status_code == 404)
check("popup: buttons open dialog (no page jump)", 'data-sample="' in a.get("/simple/admin").text and '<dialog class="pop"' in a.get("/simple/admin").text and "#sample" not in a.get("/simple/admin").text)
check("admin: sample view has close/another", "✕ Хаах" in r.text and "Өөр жишээ" in r.text)
check("admin: bad action -> 400", a.post("/simple/admin/decide", data={"cid": cid, "action": "boom"}).status_code == 400)
r = a.post("/simple/admin/decide", data={"cid": cid, "action": "approve"}, follow_redirects=False)
check("admin: approve", r.status_code == 303)
with contrib.connect() as con:
    check("admin: status approved", con.execute("SELECT status FROM contributions WHERE id=?", (cid,)).fetchone()[0] == "approved")
r = a.post("/simple/admin/decide", data={"cid": xid, "action": "reject"}, follow_redirects=False)
check("admin: reject", r.status_code == 303)
check("admin: deploy outside benchmark -> 400", a.post("/simple/admin/deploy", data={"direction": "en-mn", "path": "/etc"}).status_code == 400)
check("admin: deploy traversal -> 400", a.post("/simple/admin/deploy", data={"direction": "en-mn", "path": "../../etc"}).status_code == 400)
check("admin: deploy bad direction -> 400", a.post("/simple/admin/deploy", data={"direction": "xx", "path": "benchmark"}).status_code == 400)
contrib.gpu_busy = lambda: ["123"]
check("admin: train while GPU busy -> 409", a.post("/simple/admin/train", data={"directions": "en-mn"}).status_code == 409)
contrib.gpu_busy = lambda: []
with contrib.connect() as con:
    con.execute("UPDATE contributions SET status='rejected'")
check("admin: train with no approved -> 400", a.post("/simple/admin/train", data={"directions": "en-mn"}).status_code == 400)
# ---- сургалт (subprocess-ийг хуурамчаар солино)
launched = []
class _P:
    def __init__(self, *a, **k): launched.append(a[0])
_realPopen = subprocess.Popen; contrib.subprocess.Popen = _P
with contrib.connect() as con:
    con.execute("UPDATE contributions SET status='approved' WHERE id=?", (cid,))
r = a.get("/simple/admin")
check("ui: owner badge", "Үндсэн админ" in r.text)
check("ui: direction cards (both recommended + preselected)", 'value="en-mn"' in r.text and 'value="mn-en"' in r.text and 'value="en-mn,mn-en" checked' in r.text)
check("ui: weight choices", all(f'name="weight" value="{w}"' in r.text for w in (1, 2, 3, 5)))
check("ui: plain summary box", 'id="summary"' in r.text and "Техникийн дэлгэрэнгүй" in r.text)
check("ui: recommendation card + percentage shown", "Санал болголт" in a.get("/simple/admin").text and "санал болгосон" in a.get("/simple/admin").text)
check("ui: training type cards (add/full)", 'name="mode" value="add"' in a.get("/simple/admin").text and 'name="mode" value="full"' in a.get("/simple/admin").text)
check("ui: admin management visible to owner", "/simple/admin/admins/add" in r.text)
check("train: bad weight -> 400", a.post("/simple/admin/train", data={"directions": "en-mn", "weight": "7"}).status_code == 400)
check("train: no direction -> 400", a.post("/simple/admin/train", data={"directions": "zz", "weight": "2"}).status_code == 400)
r = a.post("/simple/admin/train", data={"directions": "mn-en", "weight": "3"}, follow_redirects=False)
check("train: ok -> redirect with msg", r.status_code == 303 and "msg=" in r.headers["location"], r.status_code)
run = sorted(os.listdir(contrib.JOBS))[-1]
cfg = json.load(open(os.path.join(contrib.JOBS, run, "config.json")))
check("train: config weight/direction/by", cfg["weight"] == 3 and cfg["directions"] == ["mn-en"] and cfg["by"] == "qa-admin@example.com", cfg)
check("train: launched run_contrib.sh", launched and launched[-1][-1] == run)
check("train: pairs deduped across contributions", len(open(os.path.join(contrib.JOBS, run, "new_pairs.csv"), encoding="utf-8").read().strip().splitlines()) - 1 == cfg["n_new"])
with contrib.connect() as con:
    check("train: contribution marked trained", con.execute("SELECT status, trained_by FROM contributions WHERE id=?", (cid,)).fetchone()[:] == ("trained", "qa-admin@example.com"))
check("ui: trained row can be re-included", "Дахин сургалтад оруулах" in a.get("/simple/admin").text)
# санал болголт
R = contrib.recommend
r = R(6643, 0.86, 0.03, {"en-mn": 370773, "mn-en": 290000})
check("recommend: 1.8% -> add-on training, x2", r["mode"] == "add" and r["weight"] == 2 and 1.7 < r["pct"] < 1.9, r)
r = R(50000, 0.90, 0.0, {"en-mn": 370773})
check("recommend: 13% -> full training", r["mode"] == "full" and r["weight"] == 2, r)
r = R(3000, 0.91, 0.01, {"en-mn": 370773})
check("recommend: excellent match -> x3", r["weight"] == 3, r)
r = R(3000, 0.80, 0.4, {"en-mn": 370773})
check("recommend: many weak pairs -> x1", r["weight"] == 1, r)
r = R(3000, 0.80, 0.0, {"en-mn": 370773}, table_share=0.95)
check("recommend: mostly table rows (human-aligned) -> x2, not penalised", r["weight"] == 2 and any("хүснэгтийн мөр" in x for x in r["reasons"]), r)
r = R(325, 0.9, 0.0, {"en-mn": 370773})
check("recommend: tiny data -> advice to collect more", any("цөөн" in x for x in r["reasons"]), r)
check("train: bad mode -> 400", a.post("/simple/admin/train", data={"directions": "en-mn", "weight": "2", "mode": "zzz"}).status_code == 400)
# run_contrib.sh-ийн жингийн логик
sh = open(os.path.join(contrib.ROOT_DIR, "benchmark", "run_contrib.sh")).read()
check("train: run_contrib.sh uses weight", '.get("weight", 2)' in sh and "[new] * w" in sh)

# ---- дата хариуцсан админ (reviewer)
r = a.post("/simple/admin/admins/add", data={"email": "bad", "name": "x", "password": "longenough"}, follow_redirects=False)
check("owner: add bad email -> err", "err=" in r.headers["location"])
r = a.post("/simple/admin/admins/add", data={"email": "rev@example.com", "name": "Rev", "password": "short"}, follow_redirects=False)
check("owner: add short password -> err", "err=" in r.headers["location"])
r = a.post("/simple/admin/admins/add", data={"email": "Rev@Example.com", "name": "Reviewer One", "password": "rev-pass-123"}, follow_redirects=False)
check("owner: add reviewer ok", "msg=" in r.headers["location"])
r = a.post("/simple/admin/admins/add", data={"email": "rev@example.com", "name": "dup", "password": "rev-pass-123"}, follow_redirects=False)
check("owner: duplicate reviewer -> err", "err=" in r.headers["location"])
check("ui: reviewer listed", "rev@example.com" in a.get("/simple/admin").text)

rv = TestClient(main.app)
check("reviewer: wrong password -> 401", rv.post("/simple/admin/login", data={"email": "rev@example.com", "password": "nope-nope"}).status_code == 401)
r = rv.post("/simple/admin/login", data={"email": "rev@example.com", "password": "rev-pass-123"}, follow_redirects=False)
check("reviewer: login ok", r.status_code == 303)
r = rv.get("/simple/admin")
check("reviewer: dashboard 200 + badge", r.status_code == 200 and "Дата хариуцсан админ" in r.text)
check("reviewer: no admin management", "/simple/admin/admins/add" not in r.text and "/simple/admin/admins/toggle" not in r.text)
check("reviewer: password form shown", "/simple/admin/password" in r.text)
check("reviewer: cannot add admin -> 403", rv.post("/simple/admin/admins/add", data={"email": "x@example.com", "password": "12345678"}).status_code == 403)
check("reviewer: cannot toggle -> 403", rv.post("/simple/admin/admins/toggle", data={"email": "rev@example.com"}).status_code == 403)
check("reviewer: cannot reset -> 403", rv.post("/simple/admin/admins/reset", data={"email": "rev@example.com", "password": "12345678"}).status_code == 403)
with contrib.connect() as con:
    con.execute("UPDATE contributions SET status='ready' WHERE id=?", (xid,))
    con.execute("INSERT INTO contrib_pairs(contribution_id,english,mongolian,labse_sim) VALUES (?,?,?,?)",
                (xid, "Everyone has the right to education.", "Хүн бүр боловсрол эзэмших эрхтэй.", 0.9))
r = rv.post("/simple/admin/decide", data={"cid": xid, "action": "approve"}, follow_redirects=False)
with contrib.connect() as con:
    row = con.execute("SELECT status, decided_by FROM contributions WHERE id=?", (xid,)).fetchone()
check("reviewer: can approve", r.status_code == 303 and tuple(row) == ("approved", "rev@example.com"), tuple(row))
r = rv.post("/simple/admin/train", data={"directions": "en-mn", "weight": "2"}, follow_redirects=False)
check("reviewer: can start training", r.status_code == 303 and "msg=" in r.headers.get("location", ""), (r.status_code, r.text[:200]))
# deploy: шалгалтад тэнцсэн / тэнцээгүй загвар
bench = os.path.join(contrib.ROOT_DIR, "benchmark")
good_m = tempfile.mkdtemp(prefix="qa_good_", dir=bench); bad_m = tempfile.mkdtemp(prefix="qa_bad_", dir=bench)
try:
    fr = os.path.join(contrib.JOBS, "contrib_qa_fake"); os.makedirs(fr, exist_ok=True)
    json.dump({"run": "contrib_qa_fake", "directions": ["en-mn"], "n_new": 1, "weight": 2, "started": "2026-11-12T10:00:00"}, open(os.path.join(fr, "config.json"), "w"))
    json.dump({"en-mn": {"ok": True, "path": good_m, "recommend_deploy": True, "tests": {"test.csv": {"base_bleu": 26.0, "new_bleu": 26.5}}, "flores": ""},
               "mn-en": {"ok": True, "path": bad_m, "recommend_deploy": False, "tests": {"test.csv": {"base_bleu": 30.0, "new_bleu": 29.0}}, "flores": ""}},
              open(os.path.join(fr, "result.json"), "w"))
    check("reviewer: deploy not-recommended -> 403", rv.post("/simple/admin/deploy", data={"direction": "mn-en", "path": bad_m}).status_code == 403)
    check("reviewer: deploy arbitrary model -> 403", rv.post("/simple/admin/deploy", data={"direction": "en-mn", "path": os.path.join(bench, "nllb13_v9_ot", "final")}).status_code == 403)
    r = rv.post("/simple/admin/deploy", data={"direction": "en-mn", "path": good_m}, follow_redirects=False)
    check("reviewer: deploy recommended ok", r.status_code == 303 and translator.model_path("en-mn") == os.path.realpath(good_m), translator.model_path("en-mn"))
    act = json.load(open(translator.ACTIVE_FILE))
    check("deploy: records by + previous", act["en-mn"]["by"] == "rev@example.com" and act["en-mn"]["previous"])
    prev = act["en-mn"]["previous"]
    rp = rv.get("/simple/admin").text
    check("reviewer: no rollback button", "руу буцаах</button>" not in rp)
    check("reviewer: rollback -> 403", rv.post("/simple/admin/deploy", data={"direction": "en-mn", "path": prev}).status_code == 403)
    ap = a.get("/simple/admin").text
    check("owner: rollback button shown", "руу буцаах</button>" in ap)
    check("owner: deploy not-recommended allowed", a.post("/simple/admin/deploy", data={"direction": "mn-en", "path": bad_m}, follow_redirects=False).status_code == 303)
    r = a.post("/simple/admin/deploy", data={"direction": "en-mn", "path": prev}, follow_redirects=False)
    check("owner: rollback ok", r.status_code == 303 and translator.model_path("en-mn") == os.path.realpath(prev))
finally:
    shutil.rmtree(good_m, ignore_errors=True); shutil.rmtree(bad_m, ignore_errors=True)
subprocess.Popen = _realPopen
# reviewer password change
r = rv.post("/simple/admin/password", data={"old": "wrong-old", "new": "newpass-123", "new2": "newpass-123"}, follow_redirects=False)
check("reviewer: password wrong old -> err", "err=" in r.headers["location"])
r = rv.post("/simple/admin/password", data={"old": "rev-pass-123", "new": "newpass-123", "new2": "different-1"}, follow_redirects=False)
check("reviewer: password mismatch -> err", "err=" in r.headers["location"])
r = rv.post("/simple/admin/password", data={"old": "rev-pass-123", "new": "newpass-123", "new2": "newpass-123"}, follow_redirects=False)
check("reviewer: password change ok", "msg=" in r.headers["location"])
check("owner: password form -> err (use paid site)", "err=" in a.post("/simple/admin/password", data={"old": "x", "new": "12345678", "new2": "12345678"}, follow_redirects=False).headers["location"])
# owner disables reviewer -> session dies immediately, login blocked
r = a.post("/simple/admin/admins/toggle", data={"email": "rev@example.com"}, follow_redirects=False)
check("owner: disable reviewer", "msg=" in r.headers["location"])
check("reviewer: kicked out after disable", rv.get("/simple/admin", follow_redirects=False).status_code == 303)
check("reviewer: login blocked when disabled", TestClient(main.app).post("/simple/admin/login", data={"email": "rev@example.com", "password": "newpass-123"}).status_code == 401)
a.post("/simple/admin/admins/toggle", data={"email": "rev@example.com"})
a.post("/simple/admin/admins/reset", data={"email": "rev@example.com", "password": "reset-pass-9"})
check("owner: reset -> old password fails", TestClient(main.app).post("/simple/admin/login", data={"email": "rev@example.com", "password": "newpass-123"}).status_code == 401)
check("owner: reset -> new password works", TestClient(main.app).post("/simple/admin/login", data={"email": "rev@example.com", "password": "reset-pass-9"}, follow_redirects=False).status_code == 303)
# audit
with contrib.connect() as con:
    acts = {r[0] for r in con.execute("SELECT action FROM audit")}
check("audit: all actions logged", {"login", "approve", "reject", "train", "deploy", "rollback", "admin_add", "admin_disable", "admin_enable", "admin_reset_password", "password_change"} <= acts, acts)
check("ui: audit log shown in Mongolian", "Үйлдлийн түүх" in a.get("/simple/admin").text and "Админ нэмсэн" in a.get("/simple/admin").text)
# ---- LaBSE босго (слайдер)
with contrib.connect() as con:
    con.execute("INSERT INTO contributions(id,name,email,org,mode,files,consent_at,ip,status,created_at,n_pairs) VALUES ('thr1','Thr','t@example.com','','two','[]','x','x','ready',?,2)", (contrib._now(),))
    for e, m, sim, tb in [("Pair at 72 percent here.", "Хос 72", 0.72, 0), ("Pair at 80 percent here.", "Хос 80", 0.80, 0),
                          ("Pair at 90 percent here.", "Хос 90", 0.90, 0), ("Table row pair here.", "Хүснэгт", 0.66, 1)]:
        con.execute("INSERT INTO contrib_pairs(contribution_id,english,mongolian,labse_sim,is_table) VALUES (?,?,?,?,?)", ("thr1", e, m, sim, tb))
j = a.get("/simple/admin/sample?cid=thr1").json()
check("threshold: default = manual 85%+ (tables too)", j["min_sim"] == 0.85 and sorted(p["sim"] for p in j["pairs"]) == [90] and len(j["sims"]) == 4, j)
with contrib.connect() as con:
    con.execute("UPDATE contributions SET min_sim=-1 WHERE id='thr1'")
j = a.get("/simple/admin/sample?cid=thr1").json()
check("threshold: legacy auto (-1) = same 85% for all (no 65% table rule)", j["min_sim"] < 0 and sorted(p["sim"] for p in j["pairs"]) == [90] and len(j["sims"]) == 4, j)
check("ui: one slider, no auto/manual split", (lambda h: 'data-mode="auto"' not in h and 'id="thrReset"' in h)(open("templates/contrib_admin.html").read()))
j = a.get("/simple/admin/sample?cid=thr1&lo=0.75&hi=0.85").json()
check("threshold: edge view shows 75-85% band", [p["sim"] for p in j["pairs"]] == [80], j["pairs"])
r = a.post("/simple/admin/threshold", data={"cid": "thr1", "min_sim": "0.80"})
check("threshold: uniform 80% -> 2 pairs (table 66% now excluded)", r.status_code == 200 and r.json()["n_pairs"] == 2, r.text)
r = a.post("/simple/admin/threshold", data={"cid": "thr1", "min_sim": "0.70"})
check("threshold: uniform 70% -> 3 pairs", r.json()["n_pairs"] == 3, r.text)
check("threshold: below 65% -> 400", a.post("/simple/admin/threshold", data={"cid": "thr1", "min_sim": "0.6"}).status_code == 400)
check("threshold: unknown id -> 404", a.post("/simple/admin/threshold", data={"cid": "nope", "min_sim": "0.8"}).status_code == 404)
check("threshold: no login -> 403", TestClient(main.app).post("/simple/admin/threshold", data={"cid": "thr1", "min_sim": "0.8"}).status_code == 403)
a.post("/simple/admin/decide", data={"cid": "thr1", "action": "approve"})
with contrib.connect() as con:
    n = con.execute(f"SELECT COUNT(*) FROM contrib_pairs p JOIN contributions c ON c.id=p.contribution_id WHERE c.id='thr1' AND c.status='approved' AND {contrib.INCLUDED}").fetchone()[0]
check("threshold: training takes pairs >= chosen threshold", n == 3, n)
_rp = subprocess.Popen; contrib.subprocess.Popen = _P; contrib.gpu_busy = lambda: []
r = a.post("/simple/admin/train", data={"directions": "en-mn", "weight": "1", "mode": "full"}, follow_redirects=False)
contrib.subprocess.Popen = _rp
frun = sorted(x for x in os.listdir(contrib.JOBS) if x.startswith("contrib_2"))[-1]; fcfg = json.load(open(os.path.join(contrib.JOBS, frun, "config.json")))
check("train full: config mode=full + previous contributions file", r.status_code == 303 and fcfg.get("mode") == "full" and os.path.exists(os.path.join(contrib.JOBS, frun, "prev_pairs.csv")), (r.status_code, fcfg))
with contrib.connect() as con:
    con.execute("UPDATE contributions SET status='approved' WHERE id='thr1'")
check("ui: table shows threshold", "босго 70%" in a.get("/simple/admin").text)
r = a.post("/simple/admin/threshold", data={"cid": "thr1", "min_sim": "-1"})
check("threshold: legacy -1 -> saved as 85%, 1 pair", r.json()["n_pairs"] == 1 and "босго 85%" in a.get("/simple/admin").text, r.text)
with contrib.connect() as con:
    con.execute("UPDATE contributions SET status='trained' WHERE id='thr1'")
check("threshold: locked after training -> 409", a.post("/simple/admin/threshold", data={"cid": "thr1", "min_sim": "0.9"}).status_code == 409)
with contrib.connect() as con:
    con.execute("UPDATE contributions SET status='rejected' WHERE id='thr1'")

# ---- 2FA (Google Authenticator)
contrib.LOGIN_HITS.clear()
import time as _t
step = lambda k=0: int(_t.time() // 30) + k
r = a.get("/simple/admin/2fa")
check("2fa: setup page shows QR + key", r.status_code == 200 and "data:image/png;base64" in r.text and "Google Authenticator" in r.text)
sec = contrib.totp_row("qa-admin@example.com")["secret"]
check("2fa: secret not enabled before confirm", not contrib.totp_enabled("qa-admin@example.com"))
r = a.post("/simple/admin/2fa/enable", data={"code": "000000"}, follow_redirects=False)
check("2fa: enable wrong code -> err", "err=" in r.headers["location"])
r = a.post("/simple/admin/2fa/enable", data={"code": contrib._hotp(sec, step())}, follow_redirects=False)
check("2fa: enable ok", "msg=" in r.headers["location"] and contrib.totp_enabled("qa-admin@example.com"))
check("ui: security card shows linked", "✓ Холбосон" in a.get("/simple/admin").text)
o = TestClient(main.app)
r = o.post("/simple/admin/login", data={"email": "qa-admin@example.com", "password": "qa-admin-pass-1"}, follow_redirects=False)
check("2fa: password ok -> asks code", r.status_code == 303 and r.headers["location"] == "/simple/admin/login/2fa")
check("2fa: no session before code", o.get("/simple/admin", follow_redirects=False).status_code == 303 and o.get("/simple/admin", follow_redirects=False).headers["location"] == "/simple/admin/login")
check("2fa: code page 200", "Баталгаажуулах код" in o.get("/simple/admin/login/2fa").text)
check("2fa: wrong code -> 401", o.post("/simple/admin/login/2fa", data={"code": "123456" if contrib._hotp(sec, step()) != "123456" else "654321"}).status_code == 401)
check("2fa: replayed code -> 401", o.post("/simple/admin/login/2fa", data={"code": contrib._hotp(sec, step())}).status_code == 401)
r = o.post("/simple/admin/login/2fa", data={"code": contrib._hotp(sec, step(1))}, follow_redirects=False)
check("2fa: correct code -> logged in", r.status_code == 303 and r.headers["location"] == "/simple/admin" and o.get("/simple/admin").status_code == 200)
x = TestClient(main.app)
x.post("/simple/admin/login", data={"email": "qa-admin@example.com", "password": "qa-admin-pass-1"}, follow_redirects=False)
for _ in range(5):
    x.post("/simple/admin/login/2fa", data={"code": "999999" if contrib._hotp(sec, step()) != "999999" else "888888"})
check("2fa: locked after 5 wrong", "хэт олон" in x.post("/simple/admin/login/2fa", data={"code": contrib._hotp(sec, step(1))}).text)
check("2fa: no cookie -> back to login", TestClient(main.app).get("/simple/admin/login/2fa", follow_redirects=False).status_code == 303)
# require for everyone
rr = TestClient(main.app); rr.post("/simple/admin/login", data={"email": "rev@example.com", "password": "reset-pass-9"})
check("2fa: reviewer can't toggle require -> 403", rr.post("/simple/admin/settings/require2fa", data={"value": "1"}).status_code == 403)
r = a.post("/simple/admin/settings/require2fa", data={"value": "1"}, follow_redirects=False)
check("2fa: owner turns on require", "msg=" in r.headers["location"] and contrib.setting("require_2fa") == "1")
contrib.LOGIN_HITS.clear()
rv2 = TestClient(main.app)
r = rv2.post("/simple/admin/login", data={"email": "rev@example.com", "password": "reset-pass-9"}, follow_redirects=False)
check("2fa: required -> reviewer forced to setup", r.headers["location"] == "/simple/admin/2fa")
check("2fa: forced user can't open dashboard", rv2.get("/simple/admin", follow_redirects=False).headers.get("location") == "/simple/admin/2fa")
check("2fa: forced user can't act -> 403", rv2.post("/simple/admin/decide", data={"cid": cid, "action": "approve"}).status_code == 403)
check("2fa: forced setup page shows warning", "шаардлагатай" in rv2.get("/simple/admin/2fa").text)
rsec = contrib.totp_row("rev@example.com")["secret"]
rv2.post("/simple/admin/2fa/enable", data={"code": contrib._hotp(rsec, step())})
check("2fa: after linking dashboard opens", rv2.get("/simple/admin").status_code == 200)
check("ui: owner sees reviewer 2FA linked + reset", "2FA цуцлах" in a.get("/simple/admin").text)
with contrib.connect() as con:
    con.execute("UPDATE totp SET last_step=0 WHERE email='qa-admin@example.com'")
check("2fa: can't disable while required", "err=" in a.post("/simple/admin/2fa/disable", data={"code": contrib._hotp(sec, step())}, follow_redirects=False).headers["location"])
r = a.post("/simple/admin/admins/reset2fa", data={"email": "rev@example.com"}, follow_redirects=False)
check("2fa: owner resets reviewer 2FA", "msg=" in r.headers["location"] and not contrib.totp_row("rev@example.com"))
check("2fa: reset logs reviewer out", rv2.get("/simple/admin", follow_redirects=False).status_code == 303)
a.post("/simple/admin/settings/require2fa", data={"value": "0"})
r = a.post("/simple/admin/2fa/disable", data={"code": contrib._hotp(sec, step())}, follow_redirects=False)
check("2fa: owner disables own 2FA", "msg=" in r.headers["location"] and not contrib.totp_enabled("qa-admin@example.com"))
check("2fa: login without 2FA again works", TestClient(main.app).post("/simple/admin/login", data={"email": "qa-admin@example.com", "password": "qa-admin-pass-1"}, follow_redirects=False).headers["location"] == "/simple/admin")
# RFC 6238 test vector (SHA1, secret "12345678901234567890", T=59 -> 94287082 -> 6 digits 287082)
import base64 as _b
check("2fa: RFC 6238 test vector", contrib._hotp(_b.b32encode(b"12345678901234567890").decode(), 59 // 30) == "287082")

with contrib.connect() as con:
    acts = {r[0] for r in con.execute("SELECT action FROM audit")}
check("audit: 2FA actions logged", {"2fa_on", "2fa_off", "2fa_reset", "2fa_fail", "require_2fa_on", "require_2fa_off"} <= acts, acts)
# ---- үйлдлийн түүх: 2 тусдаа (үндсэн / дата админ), гүйлгэж ачаалах
contrib.LOGIN_HITS.clear()
rl = TestClient(main.app); rl.post("/simple/admin/login", data={"email": "rev@example.com", "password": "reset-pass-9"})
check("audit: reviewer can't read owner log -> 403", rl.get("/simple/admin/audit?scope=owner").status_code == 403)
jr = rl.get("/simple/admin/audit?scope=reviewer&limit=200").json()
check("audit: reviewer log only has data admins", jr["rows"] and all(r["actor"] == "rev@example.com" for r in jr["rows"]), {r["actor"] for r in jr["rows"]})
jo = a.get("/simple/admin/audit?scope=owner&limit=200").json()
check("audit: owner log has no data-admin actions", jo["rows"] and all(r["actor"] != "rev@example.com" for r in jo["rows"]))
check("audit: owner can read data-admin log too", a.get("/simple/admin/audit?scope=reviewer").status_code == 200)
p1 = a.get("/simple/admin/audit?scope=owner&limit=5").json()
p2 = a.get(f"/simple/admin/audit?scope=owner&limit=5&before={p1['rows'][-1]['id']}").json()
check("audit: paging (scroll) returns older rows, no overlap", p1["more"] and p2["rows"] and p2["rows"][0]["id"] < p1["rows"][-1]["id"])
check("audit: no login -> 403", TestClient(main.app).get("/simple/admin/audit").status_code == 403)
ja = a.get("/simple/admin/audit?scope=all&limit=200").json()
check("audit: owner 'all' tab = owner + data admins", {r["role"] for r in ja["rows"]} == {"owner", "reviewer"} and ja["total"] >= len(jo["rows"]) + len(jr["rows"]))
check("audit: reviewer can't read 'all' -> 403", rl.get("/simple/admin/audit?scope=all").status_code == 403)
check("ui: reviewer page has no owner-log tab", 'data-log="owner"' not in rl.get("/simple/admin").text and 'data-log="owner"' in a.get("/simple/admin").text)

# ---- хуудас хооронд шилжих (хэрэглэгчийн өнцгөөс): хуудас бүрээс буцах/болих зам байгаа эсэх
import re as _re
def links(html):
    return set(_re.findall(r'href="([^"#?]+)', html))
v = TestClient(main.app)
pg = v.get("/contribute").text
check("nav: contribute -> back button left of the title", "/simple" in links(pg) and pg.index('class="backbtn"') < pg.index("Орчуулагчийг сайжруулахад туслах</h1>"))
check("ui: send button disabled until consent ticked", 'id="send" type="submit" disabled' in pg and "syncSend" in pg)
check("nav: contribute has 'Болих' (leave) + upload cancel", 'id="leave"' in pg and 'id="cancelUp"' in pg)
check("nav: contribute code step has 'back to edit' + browser Back support", 'id="backEdit"' in pg and "popstate" in pg)
check("nav: contribute done step links back + send another", pg.count('href="/simple"') >= 2 and 'href="/contribute"' in pg)
check("nav: simple -> contribute link", "/contribute" in links(v.get("/simple").text))
dp = v.get("/donate")
check("donate: page with account, recipient, description rule", dp.status_code == 200 and "MN82 000500 5041292715" in dp.text and "МОНГОЛЫН ЭМНЕСТИ ИНТЕРНЭШНЛ" in dp.text and "нэр, утасны дугаар" in dp.text)
check("nav: donate -> back to translator; side tab -> /donate", "/simple" in links(dp.text) and 'href="/donate"' in v.get("/simple").text)
lg = v.get("/simple/admin/login").text
check("nav: admin login -> back to translator", "/simple" in links(lg))
x2 = TestClient(main.app)
with contrib.connect() as con:
    con.execute("INSERT OR REPLACE INTO totp(email,secret,enabled,created_at) VALUES (?,?,1,?)", ("qa-admin@example.com", contrib.totp_new_secret(), contrib._now()))
contrib.LOGIN_HITS.clear()
x2.post("/simple/admin/login", data={"email": "qa-admin@example.com", "password": "qa-admin-pass-1"}, follow_redirects=False)
check("nav: 2FA code page -> back to login", "/simple/admin/login" in links(x2.get("/simple/admin/login/2fa").text))
with contrib.connect() as con:
    con.execute("DELETE FROM totp WHERE email='qa-admin@example.com'")
ap = a.get("/simple/admin").text
check("nav: admin -> translator, contribute, logout", {"/simple", "/contribute"} <= links(ap) and "/simple/admin/logout" in ap)
check("nav: 2FA setup -> back to admin + logout", "/simple/admin" in links(a.get("/simple/admin/2fa").text) and "/simple/admin/logout" in a.get("/simple/admin/2fa").text)
check("nav: logged-in admin sees back-to-admin on simple/contribute", "/simple/admin" in links(a.get("/simple").text) and "/simple/admin" in links(a.get("/contribute").text))
check("nav: unknown page (404) still has a way home", 'href="/"' in v.get("/no-such-page").text)
r = a.post("/simple/admin/logout", follow_redirects=False)
check("nav: logout lands on translator", r.headers["location"] == "/simple")
a.post("/simple/admin/login", data={"email": "qa-admin@example.com", "password": "qa-admin-pass-1"})

# ---- сэдвээр бүлэглэх
import topics as TP
comp = [(t, TP.compile_kw(k)) for t, _, k in TP.DEFAULT_TOPICS]
check("topics: human-rights text -> hr", TP.pick(TP.score_text("The detainee was subjected to torture. Хүний эрхийн комисс шалгасан.", comp))[0] == "hr")
check("topics: unrelated general text -> other", TP.pick(TP.score_text("I like to cook dinner with my family on Sundays and watch films.", comp))[0] == TP.OTHER)
comp2 = [(t, rx) for t, rx in comp if t != "health"]
check("topics: deleting a topic moves its text to the nearest remaining topic or other",
      TP.pick(TP.score_text("Hospital patients and disease. Эмнэлэг, өвчтөн.", comp))[0] == "health" and TP.pick(TP.score_text("Hospital patients and disease. Эмнэлэг, өвчтөн.", comp2))[0] in ("other", "gender", "social", "hr"))
jobs_started = []
contrib.start_topics_job = lambda: jobs_started.append(1) or True
os.makedirs(contrib.TOPIC_DIR if False else os.path.join(TMP, "topics"), exist_ok=True)
contrib.TOPIC_DIR = os.path.join(TMP, "topics")
json.dump({"updated": "2026-11-15T00:00:00+00:00", "total": 100, "order": ["hr", "other"], "names": {"hr": "Хүний эрх", "other": "Бусад"},
           "keywords": {"hr": ["torture"]}, "train": {"hr": 60, "other": 40}, "new": {"hr": 1, "other": 0}, "docs": {"hr": 3, "other": 2},
           "suggestions": [{"sid": "abc", "n": 400, "share": 4.0, "keywords": ["хөгжлийн", "бэрхшээл"], "docs": ["d1"], "examples": [{"en": "x", "mn": "y"}], "seeds": ["хөгжлийн бэрхшээл"], "centroid": []}]},
          open(os.path.join(contrib.TOPIC_DIR, "state.json"), "w"), ensure_ascii=False)
tp = a.get("/simple/admin").text
check("topics ui: chart, suggestion, add form", 'id="topics"' in tp and "Шинэ сэдэв санал болгож байна" in tp and "/simple/admin/topics/add" in tp)
check("topics: add needs >=2 keywords", "err=" in a.post("/simple/admin/topics/add", data={"name": "X", "examples": "a"}, follow_redirects=False).headers["location"])
r = a.post("/simple/admin/topics/add", data={"name": "Хөгжлийн бэрхшээл", "examples": "хөгжлийн бэрхшээл, тэргэнцэр, disabilit"}, follow_redirects=False)
check("topics: add ok -> reclassify started", "msg=" in r.headers["location"] and jobs_started)
check("topics: rename keeps data (state counts unchanged)", "msg=" in a.post("/simple/admin/topics/rename", data={"tid": "hr", "name": "Хүний эрх (шинэ нэр)"}, follow_redirects=False).headers["location"]
      and json.load(open(os.path.join(contrib.TOPIC_DIR, "state.json")))["train"]["hr"] == 60 and json.load(open(os.path.join(contrib.TOPIC_DIR, "state.json")))["names"]["hr"] == "Хүний эрх (шинэ нэр)")
n0 = len(jobs_started)
r = a.post("/simple/admin/topics/delete", data={"tid": "health"}, follow_redirects=False)
with contrib.connect() as con:
    act = con.execute("SELECT active FROM topics WHERE id='health'").fetchone()[0]
check("topics: delete = hidden (history kept) + reclassify", "msg=" in r.headers["location"] and act == 0 and len(jobs_started) > n0)
check("topics: keywords edit", "msg=" in a.post("/simple/admin/topics/keywords", data={"tid": "hr", "keywords": "torture, эрүүдэн, detention"}, follow_redirects=False).headers["location"])
r = a.post("/simple/admin/topics/suggest", data={"sid": "abc", "action": "dismiss"}, follow_redirects=False)
with contrib.connect() as con:
    dis = con.execute("SELECT COUNT(*) FROM topic_suggest_dismissed WHERE id='abc'").fetchone()[0]
check("topics: dismissed suggestion remembered (won't come back)", "msg=" in r.headers["location"] and dis == 1)
check("topics: sample needs login", TestClient(main.app).get("/simple/admin/topics/sample?tid=hr").status_code == 403)

# owner demoted in paid site -> session invalid
with db.connect() as con:
    con.execute("UPDATE users SET plan='paid' WHERE email='qa-admin@example.com'")
check("owner: demoted -> session invalid", a.get("/simple/admin", follow_redirects=False).status_code == 303)
with db.connect() as con:
    con.execute("UPDATE users SET plan='admin' WHERE email='qa-admin@example.com'")

r = a.post("/simple/admin/logout", follow_redirects=False)
check("admin: logout", r.status_code == 303 and a.get("/simple/admin", follow_redirects=False).status_code == 303)

# ---------------------------------------------------------------- ТЭГШЛЭЛ (жинхэнэ процесс, CPU)
d = os.path.join(TMP, "real"); os.makedirs(d)
cid2 = "qa-real"
for name, data in (("en.txt", TXT_EN), ("mn.txt", TXT_MN)):
    open(os.path.join(contrib.UP, cid2, name) if os.path.isdir(os.path.join(contrib.UP, cid2)) else os.path.join(d, name), "wb").write(data)
os.makedirs(os.path.join(contrib.UP, cid2), exist_ok=True)
for name, data in (("en.txt", TXT_EN), ("mn.txt", TXT_MN)):
    open(os.path.join(contrib.UP, cid2, name), "wb").write(data)
with contrib.connect() as con:
    con.execute("INSERT INTO contributions(id,name,email,mode,files,consent_at,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (cid2, "QA", "qa@example.com", "two", json.dumps([{"role": "en", "file": "en.txt", "name": "en.txt"},
                                                                  {"role": "mn", "file": "mn.txt", "name": "mn.txt"}]),
                 db.now_iso(), "queued", db.now_iso()))
env = {**os.environ, "CUDA_VISIBLE_DEVICES": ""}
code_ = (f"import sys; sys.path.insert(0, {os.path.dirname(os.path.abspath(__file__))!r}); import contrib; "
         f"contrib.DB={contrib.DB!r}; contrib.UP={contrib.UP!r}; import contrib_process as cp; cp.contrib=contrib; cp.main({cid2!r})")
p = subprocess.run([sys.executable, "-c", code_], cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   capture_output=True, text=True, env=env, timeout=900)
with contrib.connect() as con:
    row = con.execute("SELECT status, n_pairs, message FROM contributions WHERE id=?", (cid2,)).fetchone()
check("align: process finished", row and row["status"] in ("ready", "failed"), p.stderr[-300:])
check("align: found >=1 good pair", row and row["n_pairs"] >= 1, row and row["message"])

# ---------------------------------------------------------------- PUBLIC MODE (8080 горим)
pub = subprocess.run([sys.executable, "-c", f"""
import os, sys; os.environ['PUBLIC_SIMPLE']='1'; os.environ['APP_DB']={os.path.join(TMP, 'pub.db')!r}
sys.path.insert(0, {os.path.dirname(os.path.abspath(__file__))!r})
import translator; translator.available_directions=lambda: {{'en-mn': True, 'mn-en': True}}
import db; db.init(); import main, contrib
contrib.DB={os.path.join(TMP, 'pubc.db')!r}; contrib.UP={os.path.join(TMP, 'pubup')!r}; contrib.start_processing=lambda c: None
from fastapi.testclient import TestClient; c=TestClient(main.app)
g=lambda u: c.get(u, follow_redirects=False).status_code
print('donate', g('/donate')); print('simple', g('/simple')); print('contribute', g('/contribute')); print('admin', g('/simple/admin')); print('paidadmin', g('/admin'))
codes=[c.post('/api/contribute/verify', json={{'id':'x','code':'1'}}).status_code for _ in range(12)]
print('rate', codes[-1])
"""], capture_output=True, text=True, timeout=300)
out = pub.stdout
check("public: /simple 200", "simple 200" in out, out[-300:] + pub.stderr[-300:])
check("public: /contribute 200", "contribute 200" in out)
check("public: /donate 200", "donate 200" in out)
check("public: /simple/admin hidden", "admin 302" in out)
check("public: /admin hidden", "paidadmin 302" in out)
check("public: contribute rate limit 429", "rate 429" in out)

# ---------------------------------------------------------------- LIVE SERVER (жинхэнэ загвар)
try:
    import requests
    r = requests.post("http://localhost:8000/api/simple/translate", json={"text": "The minister called the law “a step backwards”.", "direction": "en-mn"}, timeout=120).json()
    check("live: en-mn translation + curly quotes kept", "“" in r["translation"] and len(r["translation"]) > 10, r)
    r = requests.post("http://localhost:8000/api/simple/translate", json={"text": "Хүн бүр боловсрол эзэмших эрхтэй.", "direction": "mn-en"}, timeout=120).json()
    check("live: mn-en translation", "education" in r["translation"].lower(), r)
    for u in ("/simple", "/contribute", "/simple/admin/login"):
        check(f"live: {u} 200", requests.get("http://localhost:8000" + u, timeout=30).status_code == 200)
except Exception as ex:  # noqa: BLE001
    check("live server reachable", False, str(ex))

shutil.rmtree(TMP, ignore_errors=True)
ok = sum(1 for _, p, _ in results if p)
print(f"\n=== {ok}/{len(results)} passed ===")
for n, p, i in results:
    if not p:
        print("FAILED:", n, i)
