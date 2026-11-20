"""QA for the public /simple mode. Usage: python qa_public.py http://127.0.0.1:8001 <dir> <db>"""
import io, itertools, json, os, sqlite3, sys, time, zipfile
import requests

B, D, DBP = sys.argv[1], sys.argv[2], sys.argv[3]
_ip = itertools.count(1)
results, samples = [], []


def ip():
    return {"CF-Connecting-IP": f"10.9.{next(_ip) // 250}.{next(_ip) % 250}"}


def check(name, cond, info=""):
    results.append((name, bool(cond), info))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info and not cond else ""))


def tr(text, d="en-mn", h=None, raw=None):
    h = h or ip()
    if raw is not None:
        return requests.post(B + "/api/simple/translate", data=raw, headers={**h, "content-type": "application/json"})
    return requests.post(B + "/api/simple/translate", json={"text": text, "direction": d}, headers=h)


def docx_text(b):
    import re
    z = zipfile.ZipFile(io.BytesIO(b))
    x = z.read("word/document.xml").decode()
    return " ".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", x))


def is_cyr(s):
    letters = [c for c in s if c.isalpha()]
    return letters and sum("Ѐ" <= c <= "ӿ" for c in letters) / len(letters) > 0.7


def is_lat(s):
    letters = [c for c in s if c.isalpha()]
    return letters and sum(c.isascii() for c in letters) / len(letters) > 0.9


# ---------------- 1. pages & blocked routes
for p in ["/", "/simple", "/static/app.js", "/static/style.css", "/robots.txt"]:
    r = requests.get(B + p)
    check(f"GET {p} -> 200", r.status_code == 200, r.status_code)
r = requests.get(B + "/")
check("home page = translator (has textarea)", "<textarea" in r.text)
check("home page has no login/register/pricing links", not any(x in r.text for x in ['href="/login', 'href="/register', 'href="/pricing', 'href="/admin']))
for p in ["/login", "/register", "/admin", "/account", "/pricing", "/buy", "/forgot", "/docs", "/openapi.json", "/redoc", "/static/../config.py", "/instance/app.db"]:
    r = requests.get(B + p, allow_redirects=False)
    check(f"blocked GET {p}", r.status_code in (302, 404) and "config" not in r.text[:200], r.status_code)
for p in ["/api/translate", "/api/translate-file", "/login", "/register", "/admin/users", "/logout"]:
    r = requests.post(B + p, data={"x": 1}, headers=ip(), allow_redirects=False)
    check(f"blocked POST {p}", r.status_code in (302, 404), r.status_code)
r = requests.get(B + "/")
check("security headers", r.headers.get("X-Frame-Options") == "DENY" and r.headers.get("X-Content-Type-Options") == "nosniff")

# ---------------- 2. text translation
EN = {
    "UDHR art.1": "All human beings are born free and equal in dignity and rights.",
    "torture": "No one shall be subjected to torture or to cruel, inhuman or degrading treatment or punishment.",
    "numbers/dates": "On 10 December 1948, the General Assembly adopted the Declaration by 48 votes to none.",
    "quote": 'The Special Rapporteur said: "Detainees must have access to a lawyer."',
    "multi-paragraph": "Freedom of expression is protected.\n\nThe press must be free.\n\nCensorship is prohibited.",
    "heading+list": "Recommendations\n1. Ratify the Optional Protocol.\n2. End child labour in mining.",
    "single word": "Justice",
}
MN = {
    "Constitution": "Монгол Улсын иргэн хууль ёсны дагуу эрх, эрх чөлөөгөө эдэлж, үүргээ биелүүлнэ.",
    "life": "Хүн бүр амьд явах, эрүүл, аюулгүй орчинд амьдрах эрхтэй.",
    "NHRC": "Хүний эрхийн Үндэсний Комисс 2001 онд байгуулагдсан.",
    "multi-paragraph": "Хүүхэд бүр боловсрол эзэмших эрхтэй.\n\nЭмэгтэйчүүдийн эсрэг хүчирхийллийг таслан зогсоох шаардлагатай.",
    "single word": "Шударга ёс",
}
for d, cases, ok_lang in (("en-mn", EN, is_cyr), ("mn-en", MN, is_lat)):
    for name, text in cases.items():
        t0 = time.time()
        r = tr(text, d)
        dt = time.time() - t0
        j = r.json() if r.ok else {}
        out = j.get("translation", "")
        good = r.ok and out.strip() and ok_lang(out)
        if "paragraph" in name:
            good = good and out.count("\n\n") == text.count("\n\n")
        if "numbers" in name or "NHRC" in name:
            good = good and all(n in out for n in ("1948", "48") if n in text) and ("2001" in out if "2001" in text else True)
        check(f"text {d}: {name}", good, f"{r.status_code} {out[:80]!r}")
        samples.append((d, text, out, round(dt, 1)))

r = tr("x" * 5000)
check("exactly 5000 chars accepted", r.status_code == 200, r.status_code)
r = tr("x" * 5001)
check("5001 chars rejected (400)", r.status_code == 400, r.status_code)
long_en = (EN["torture"] + " ") * 40
t0 = time.time(); r = tr(long_en[:5000]); dt = time.time() - t0
check(f"long text ~4000 chars translated ({dt:.1f}s)", r.ok and len(r.json()["translation"]) > 1000, r.status_code)
samples.append(("en-mn", f"[{len(long_en)} chars repeated text]", f"[{len(r.json().get('translation',''))} chars out]", round(dt, 1)))
for name, kw in [("empty", dict(text="")), ("spaces", dict(text="   \n ")), ("number type", dict(text=123)),
                 ("null", dict(text=None)), ("bad direction", dict(text="Hello", d="en-fr")), ("direction list", dict(text="Hi", d=["en-mn"]))]:
    r = requests.post(B + "/api/simple/translate", json={"text": kw["text"], "direction": kw.get("d", "en-mn")}, headers=ip())
    check(f"invalid input '{name}' -> 4xx", 400 <= r.status_code < 500, f"{r.status_code} {r.text[:80]}")
for name, raw in [("broken JSON", "{text:"), ("JSON list", "[1,2]"), ("empty body", "")]:
    r = tr(None, raw=raw)
    check(f"invalid body '{name}' -> 4xx", 400 <= r.status_code < 500, f"{r.status_code} {r.text[:80]}")
r = tr("Emoji test 😀 and email info@example.org, URL https://ohchr.org.")
check("emoji/email/url does not crash", r.ok, r.status_code)
if r.ok:
    samples.append(("en-mn", "Emoji test 😀 and email info@example.org, URL https://ohchr.org.", r.json()["translation"], 0))
r = tr("<script>alert(1)</script> Human rights")
check("HTML input does not crash", r.ok, r.status_code)

# ---------------- 3. rate limits
h = ip()
codes = [tr("Hello.", h=h).status_code for _ in range(11)]
check("text limit: 10/min ok, 11th -> 429", codes[:10] == [200] * 10 and codes[10] == 429, codes)
check("other IP not affected by limit", tr("Hello.").status_code == 200)


# ---------------- 4. files
def upload(fn, d="en-mn", s=None, h=None):
    s = s or requests.Session()
    with open(os.path.join(D, fn), "rb") as f:
        r = s.post(B + "/api/simple/translate-file", files={"file": (fn, f)}, data={"direction": d}, headers=h or ip())
    return s, r


def wait(s, jid, t=180):
    for _ in range(t):
        j = s.get(B + f"/api/jobs/{jid}").json()
        if j.get("status") in ("done", "failed"):
            return j
        time.sleep(1)
    return j


file_cases = [("en.txt", "en-mn", is_cyr), ("en.docx", "en-mn", is_cyr), ("en.pdf", "en-mn", is_cyr),
              ("mn.txt", "mn-en", is_lat), ("mn.docx", "mn-en", is_lat), ("mn.pdf", "mn-en", is_lat)]
sess, jobs = requests.Session(), []
for fn, d, lang in file_cases:
    t0 = time.time()
    _, r = upload(fn, d, s=sess)
    if not r.ok:
        check(f"file {fn} upload", False, f"{r.status_code} {r.text[:100]}"); continue
    jid = r.json()["job_id"]
    j = wait(sess, jid)
    dl = sess.get(B + f"/download/{jid}")
    txt = docx_text(dl.content) if dl.ok else ""
    good = j["status"] == "done" and dl.ok and lang(txt)
    if fn.startswith("en.") and fn != "en.txt":
        good = good and "1948" in txt
    check(f"file {fn} ({d}) -> docx ({time.time()-t0:.0f}s)", good, f"{j.get('status')} {j.get('error')} {txt[:80]!r}")
    samples.append((d, f"[file {fn}]", txt[:600], round(time.time() - t0, 1)))
    jobs.append(jid)
    # privacy: someone else cannot read/download it
    other = requests.Session()
    check(f"file {fn}: other visitor cannot see job/download", other.get(B + f"/api/jobs/{jid}").status_code == 404
          and other.get(B + f"/download/{jid}").status_code == 404)
r = sess.get(B + "/download-zip?ids=" + ",".join(jobs))
check("ZIP of all files", r.ok and len(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == len(jobs), r.status_code)

for fn, why in [("empty.txt", "empty file"), ("fake.pdf", "corrupt PDF"), ("bad.exe", "wrong type"),
                ("pages25.pdf", ">20 pages"), ("big.txt", ">15 MB")]:
    _, r = upload(fn)
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.ok and fn == "empty.txt":  # accepted but must not crash the worker
        jj = wait(_, j["job_id"], 60)
        check(f"bad file '{why}' handled", jj["status"] in ("done", "failed"), jj)
    else:
        check(f"bad file '{why}' rejected with message", 400 <= r.status_code < 500 and j.get("error"), f"{r.status_code} {r.text[:100]}")
        print("     message:", j.get("error"))

h = ip()
codes = [upload("en.txt", h=h)[1].status_code for _ in range(6)]
check("file limit: 5/hour ok, 6th -> 429", codes[:5] == [200] * 5 and codes[5] == 429, codes)

# ---------------- 5. privacy: nothing translated stored
time.sleep(20)
con = sqlite3.connect(DBP)
dump = "\n".join(con.iterdump())
check("DB has no source text", "born free and equal" not in dump and "амьд явах" not in dump)
check("DB has no user accounts touched", con.execute("SELECT count(*) FROM users WHERE email!='guest@system.local'").fetchone()[0] == 0 if "users" in dump else True)
up = os.path.join(os.path.dirname(DBP), "uploads")
left = os.listdir(up) if os.path.isdir(up) else []
check("uploaded originals deleted after translation", not left, left)
log = open(os.path.join(D, "server.log")).read()
check("server log has no source text", "born free" not in log and "амьд явах" not in log)

print("\nSUMMARY", sum(r[1] for r in results), "/", len(results))
json.dump({"results": results, "samples": samples}, open(os.path.join(D, "qa_results.json"), "w"), ensure_ascii=False, indent=1)
