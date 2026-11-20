"""Сургалтын дата өгөх ("contribute") болон түүний админ самбар.

Хэрэглэгч (2026-11-12): хүмүүс англи/монгол 2 файл эсвэл хоёр хэлтэй 1 файл оруулна, нэр + имэйлээ
6 оронтой кодоор баталгаажуулна, зөвшөөрөл өгнө. Сервер файлыг тусдаа процессоор (contrib_process.py)
тэгшилж LaBSE >= 0.85 хосуудыг хадгална. Админ /simple/admin дээр хараад зөвшөөрч/татгалзаж,
сургалт эхлүүлж, үр дүнг харьцуулж, загварыг шинэчилж/буцаана.

Өгөгдөл: instance/contrib.db (8000, 8080 сервер хоёулаа энэ нэг файлыг ашиглана),
файлууд: instance/contrib_uploads/<id>/.
"""
import glob
import json
import os
import re
import secrets
import sqlite3
import subprocess
import time
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import auth
import db
import mailer
import otp
import translator
from config import BASE_DIR, ROOT_DIR

DB = os.path.join(BASE_DIR, "instance", "contrib.db")
UP = os.path.join(BASE_DIR, "instance", "contrib_uploads")
JOBS = os.path.join(ROOT_DIR, "benchmark", "contrib_runs")
PY = sys.executable
ALLOWED = {".pdf", ".docx", ".txt", ".doc", ".odt", ".rtf", ".csv", ".xlsx"}
MIN_SIM = 0.85        # анхны (санал болгох) босго
SIM_FLOOR = 0.70      # үүнээс доош таарцтай хосыг огт хадгалахгүй (ихэнх нь буруу орчуулга)
TABLE_MIN = 0.65      # хүснэгтийн мөр (орчуулагч өөрөө зэрэгцүүлсэн): автомат горимд үүнээс дээш бол орно
# Илгээлтийн босго (min_sim): -1 = хуучин 'автомат' (одоо 85%-тай адил), эсвэл админ слайдераар сонгосон нэг босго бүх хосод
# 2026-11-15: хэрэглэгчийн шийдвэр: санал болгох нь нэг л босго (85%), хүснэгтэд тусгай 65% дүрэм байхгүй
INCLUDED = (f"((c.min_sim < 0 AND p.labse_sim >= {MIN_SIM} - 1e-9) "
            f"OR (c.min_sim >= 0 AND p.labse_sim >= c.min_sim - 1e-9))")
MAX_FILES = 100        # олон файлын горим: нэг илгээлтэд
MAX_TOTAL_MB = 95     # Cloudflare нэг хүсэлтэд 100 MB хүртэл зөвшөөрдөг
PROCESS_MAX_MEM = 24 * 1024 ** 3   # боловсруулах процессын санах ойн дээд хэмжээ
CONTRIB_MAX_MB = 50   # дата өгөх хуудсан дээрх нэг файлын дээд хэмжээ (орчуулгын 15 MB-аас тусдаа)
otp.PURPOSES.setdefault("contribute", "Сургалтын дата баталгаажуулах код")

SCHEMA = """
CREATE TABLE IF NOT EXISTS contributions (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL, org TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL, files TEXT NOT NULL, consent_at TEXT NOT NULL, ip TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,           -- unverified / queued / processing / ready / failed / approved / rejected / trained
    message TEXT NOT NULL DEFAULT '', n_pairs INTEGER NOT NULL DEFAULT 0, mean_sim REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, verified_at TEXT, decided_at TEXT, trained_in TEXT
);
CREATE TABLE IF NOT EXISTS simple_admins (      -- дата хариуцсан админууд (үндсэн админ нэмж/хааж чадна)
    id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS simple_sessions (
    token TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS totp (                -- Google Authenticator (2FA): админ бүрийн нууц түлхүүр
    email TEXT PRIMARY KEY, secret TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, last_step INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_2fa (         -- нууц үг зөв, 6 оронтой код хүлээж буй нэвтрэлт (5 минут)
    token TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL, expires_at TEXT NOT NULL,
    tries INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contrib_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, contribution_id TEXT NOT NULL REFERENCES contributions(id),
    english TEXT NOT NULL, mongolian TEXT NOT NULL, labse_sim REAL NOT NULL
);
"""
STATUS_LABELS = {"unverified": "Код хүлээж байна", "queued": "Дараалалд", "processing": "Боловсруулж байна",
                 "ready": "Шалгах", "failed": "Алдаа", "approved": "Зөвшөөрсөн", "rejected": "Татгалзсан",
                 "trained": "Сургалтад орсон"}


@contextmanager
def connect():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB, timeout=20)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(SCHEMA)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(contributions)")}
        for c in ("decided_by", "trained_by"):
            if c not in cols:
                con.execute(f"ALTER TABLE contributions ADD COLUMN {c} TEXT")
        if "min_sim" not in cols:   # илгээлт бүрийн LaBSE босго (админ слайдераар тохируулна)
            con.execute("ALTER TABLE contributions ADD COLUMN min_sim REAL NOT NULL DEFAULT 0.85")
        if "is_table" not in {r["name"] for r in con.execute("PRAGMA table_info(contrib_pairs)")}:
            con.execute("ALTER TABLE contrib_pairs ADD COLUMN is_table INTEGER NOT NULL DEFAULT 0")
        if "setup_only" not in {r["name"] for r in con.execute("PRAGMA table_info(simple_sessions)")}:
            con.execute("ALTER TABLE simple_sessions ADD COLUMN setup_only INTEGER NOT NULL DEFAULT 0")
        yield con
        con.commit()
    finally:
        con.close()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


UNVERIFIED_TTL_H = 24
CLEANUP_FROM = "2026-11-12T12:00:00+00:00"   # үүнээс өмнөх (таны өмнөх туршилтын) илгээлтэд хүрэхгүй


def cleanup_unverified():
    """Имэйлээ баталгаажуулаагүй 24 цагаас өмнөх илгээлтийн файлыг устгана (диск дүүргэх халдлага, хувийн мэдээлэл)."""
    import shutil
    cut = (datetime.now(timezone.utc) - timedelta(hours=UNVERIFIED_TTL_H)).isoformat(timespec="seconds")
    with connect() as con:
        old = [r["id"] for r in con.execute("SELECT id FROM contributions WHERE status='unverified' AND created_at<? AND created_at>=?",
                                            (cut, CLEANUP_FROM))]
        for cid in old:
            shutil.rmtree(os.path.join(UP, cid), ignore_errors=True)
            con.execute("DELETE FROM contributions WHERE id=? AND status='unverified'", (cid,))


def start_processing(cid):
    log = os.path.join(UP, cid, "process.log")
    # Хор хөнөөлтэй файлаас хамгаалах: 2 цагийн хугацаа, 24 GB санах ой, бага давуу эрх (nice). Хэтэрвэл "Алдаа" болно.
    script = os.path.join(BASE_DIR, "contrib_process.py")
    cmd = ['bash', '-c', 'timeout --kill-after=60 7200 nice -n 10 prlimit --as=$4 -- "$1" "$2" "$3" || "$1" "$2" --killed "$3"',
           "contrib", PY, script, cid, str(PROCESS_MAX_MEM)]
    subprocess.Popen(cmd, cwd=ROOT_DIR, stdout=open(log, "w"), stderr=subprocess.STDOUT, start_new_session=True,
                     env={**os.environ, "CUDA_VISIBLE_DEVICES": ""})  # LaBSE-г CPU дээр: орчуулга, сургалтад саад болохгүй


def gpu_busy():
    """Өөр сургалт (MADLAD г.м.) явж байгаа эсэх."""
    try:
        out = subprocess.run(["pgrep", "-f", "train_bench.py|train_madlad.py|train_contrib.py|run_contrib.sh"],
                             capture_output=True, text=True).stdout.split()
        return [p for p in out if p != str(os.getpid())]
    except OSError:
        return []


CUSTOM_TESTS = os.path.join(ROOT_DIR, "benchmark", "custom_tests")


BUILTIN_TESTS = [("test.csv", "Хүний эрх", True), ("test_vision2050.csv", "Vision 2050", False),
                 ("test_laws.csv", "Хууль", False), ("C_unseen_reports.csv", "C тест (шинэ тайлан)", False)]  # True = заавал (загвар солих шийдвэр үүн дээр)
TESTS_OFF = os.path.join(ROOT_DIR, "benchmark", "tests_disabled.json")


def tests_off():
    try:
        return set(json.load(open(TESTS_OFF)))
    except (OSError, ValueError):
        return set()


EXT_TESTS = {"test.csv": ("Хүний эрх", "data/test.csv"), "test_vision2050.csv": ("Vision 2050", "data/test_vision2050.csv"),
             "test_laws.csv": ("Хууль", "data/test_laws.csv"), "C_unseen_reports.csv": ("C тест (шинэ тайлан)", "benchmark/new_test/C_unseen_reports.csv")}


def ext_test_sets():
    """Гадны орчуулагчтай харьцуулах боломжтой шалгалтууд: {файл: (нэр, бүтэн зам)} (админы нэмсэн шалгалтыг оруулаад)."""
    out = {k: (v[0], os.path.join(ROOT_DIR, v[1])) for k, v in EXT_TESTS.items()}
    for ct in custom_tests():
        out[ct["file"]] = (ct["name"], os.path.join(CUSTOM_TESTS, ct["file"] + (".disabled" if ct["off"] else "")))
    return out


def _docx_bytes(paras):
    """python-docx-гүйгээр энгийн .docx үүсгэнэ (дугаарласан мөр бүр нэг догол мөр)."""
    import io, zipfile
    from xml.sax.saxutils import escape
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{escape(p)}</w:t></w:r></w:p>' for p in paras)
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f'<w:body>{body}</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def our_score(test, direction):
    """Манай одоогийн загварын тухайн шалгалт дээрх хамгийн сүүлийн BLEU (сургалтын үр дүнгээс)."""
    cur = os.path.realpath(translator.model_path(direction))
    best = None
    for r in sorted(os.listdir(JOBS)) if os.path.isdir(JOBS) else []:
        rs = run_status(r)
        res = (rs.get("result") or rs.get("kept") or {}).get(direction, {})
        t = (res.get("tests") or {}).get(test)
        if not t:
            continue
        base = os.path.realpath((rs.get("config") or {}).get("base", {}).get(direction, "/x"))
        if res.get("path") and os.path.realpath(res["path"]) == cur:
            best = t["new_bleu"]
        elif base == cur:
            best = t["base_bleu"]
    return best


def ext_rows():
    try:
        with connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS ext_bench (id INTEGER PRIMARY KEY, translator TEXT, test TEXT, direction TEXT, "
                        "bleu REAL, chrf REAL, matched INTEGER, n INTEGER, at TEXT, by TEXT, note TEXT)")
            if not con.execute("SELECT 1 FROM ext_bench LIMIT 1").fetchone():
                con.execute("INSERT INTO ext_bench(translator,test,direction,bleu,chrf,matched,n,at,by,note) VALUES "
                            "('Google Translate','C_unseen_reports.csv','en-mn',30.35,59.27,300,300,'2026-11-10T13:19:06+00:00','','анхны харьцуулалт')")
            rows = [dict(r) for r in con.execute("SELECT * FROM ext_bench ORDER BY test, direction, at DESC")]
    except Exception:  # noqa: BLE001
        return []
    names = {k: v[0] for k, v in EXT_TESTS.items()}
    names.update({ct["file"]: ct["name"] for ct in custom_tests()})
    rows.sort(key=lambda r: (r["at"] or ""), reverse=True)   # шинэ нь дээрээ
    for r in rows:
        r["test_name"] = names.get(r["test"], r["test"])
        r["ours"] = our_score(r["test"], r["direction"])
        # Тухайн орчуулагчийн өмнөх хэмжилт (ижил шалгалт, чиглэл) -> цаг хугацааны өөрчлөлт
        prev = [x for x in rows if x["translator"].lower() == r["translator"].lower() and x["test"] == r["test"]
                and x["direction"] == r["direction"] and (x["at"] or "") < (r["at"] or "")]
        r["prev_bleu"] = prev[0]["bleu"] if prev else None
        r["prev_at"] = prev[0]["at"][:10] if prev else ""
    return rows


def custom_tests():
    """Админы оруулсан шалгалтын материал (сургалтад хэзээ ч орохгүй, зөвхөн шалгалтад)."""
    out = []
    for fn in sorted(os.listdir(CUSTOM_TESTS)) if os.path.isdir(CUSTOM_TESTS) else []:
        if fn.endswith(".csv") or fn.endswith(".csv.disabled"):
            base = fn[:fn.index(".csv")]
            try:
                meta = json.load(open(os.path.join(CUSTOM_TESTS, base + ".json")))
            except (OSError, ValueError):
                meta = {}
            out.append(dict(file=base + ".csv", off=fn.endswith(".disabled"), name=meta.get("name", base), n=meta.get("n", 0),
                            at=meta.get("at", ""), by=meta.get("by", "")))
    return out


def model_versions(direction):
    """Загвар бүрт хувилбарын дугаар: суурь загвар (жишээ нь nllb13_v9_ot -> v9), дараагийн амжилттай сургалт бүр +1.
    {realpath: (дугаар, тайлбар, огноо)}"""
    base = os.path.realpath(translator.MODELS[direction])
    m = re.search(r"_v(\d+)", base)
    n = int(m.group(1)) if m else 1
    out = {base: (f"v{n}", "Суурь загвар (анхнаасаа ашиглаж байсан)", "")}
    for r in sorted(os.listdir(JOBS)) if os.path.isdir(JOBS) else []:
        rs = run_status(r)
        res = (rs.get("result") or rs.get("kept") or {}).get(direction, {})
        p = res.get("path")
        if res.get("ok") and p and os.path.isdir(p):
            n += 1
            st = (json.load(open(os.path.join(JOBS, r, "config.json"))).get("started") or "") if os.path.exists(os.path.join(JOBS, r, "config.json")) else ""
            out[os.path.realpath(p)] = (f"v{n}", f"Сургалт {st[:16].replace('T', ' ') or r}", st[:16].replace("T", " "))
    return out


def model_label(direction, path, vers=None):
    vers = vers if vers is not None else model_versions(direction)
    rp = os.path.realpath(path or "")
    if rp in vers:
        return vers[rp]
    m = re.search(r"_v(\d+)", rp)
    return (f"v{m.group(1)}" if m else os.path.basename(os.path.dirname(rp)) or "?", os.path.relpath(rp, os.path.join(ROOT_DIR, "benchmark")), "")


def model_options(direction, active, current):
    """Загварын түүх: одоогийн, өмнө ашиглаж байсан, сургалтаар гарсан бүх загвар (шинээс хуучин руу)."""
    a = active.get(direction, {}) if active else {}
    cur = os.path.realpath(translator.model_path(direction))
    vers = model_versions(direction)
    used = {os.path.realpath(p) for p in a.get("history", [])} | {os.path.realpath(a.get("previous") or "/x")}
    prev = os.path.realpath(a.get("previous") or "/x")
    out = []
    for p, (v, desc, at) in sorted(vers.items(), key=lambda kv: -int(kv[1][0][1:]) if kv[1][0][1:].isdigit() else 0):
        res = {}
        for r in os.listdir(JOBS) if os.path.isdir(JOBS) else []:
            rs = run_status(r)
            x = (rs.get("result") or rs.get("kept") or {}).get(direction, {})
            if x.get("path") and os.path.realpath(x["path"]) == p:
                res = x
        t = (res.get("tests") or {}).get("test.csv", {})
        notes = []
        if p == cur:
            notes.append("одоо ашиглаж байгаа")
        elif p == prev:
            notes.append("яг өмнө нь ашиглаж байсан")
        elif p in used:
            notes.append("өмнө ашиглаж байсан")
        if t:
            notes.append(f"Хүний эрх BLEU {t['base_bleu']:.1f} → {t['new_bleu']:.1f}")
        if res:
            notes.append("шалгалтад тэнцсэн" if res.get("recommend_deploy") else "шалгалтад тэнцээгүй")
        out.append(dict(path=p, label=v, desc=desc, at=at, note=" · ".join(notes), current=p == cur,
                        recommended=bool(res.get("recommend_deploy")), base=not res))
    return out


def gpu_free_mb():
    """GPU-ийн сул санах ой (MB), мэдэгдэхгүй бол None."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        return int(out.split()[0])
    except Exception:  # noqa: BLE001
        return None


def run_alive(run):
    """Энэ сургалтын процесс (run_contrib.sh <run>) одоо ажиллаж байна уу."""
    try:
        out = subprocess.run(["pgrep", "-f", f"run_contrib.sh {run}"], capture_output=True, text=True, timeout=5).stdout
        return any(pid.strip() and int(pid) != os.getpid() for pid in out.split())
    except Exception:  # noqa: BLE001
        return True   # мэдэхгүй бол "тасарсан" гэж буруу айлгахгүй


def _eta_mn(t):
    """tqdm-ийн "02:06" / "1:02:06" -> "2 мин 6 сек" / "1 цаг 2 мин" (цаг гэж андуурахгүй)."""
    try:
        p = [int(x) for x in t.strip().split(":")]
    except ValueError:
        return ""
    h, m, sec = ([0] * (3 - len(p)) + p)[-3:]
    if h:
        return f"{h} цаг {m} мин"
    return f"{m} мин {sec} сек" if m else f"{sec} сек"


def run_status(run):
    d = os.path.join(JOBS, run)
    st = {"run": run}
    for f in ("config.json", "result.json"):
        p = os.path.join(d, f)
        if os.path.exists(p):
            st[f.split(".")[0]] = json.load(open(p))
    dp = os.path.join(d, "declined.json")   # админ "одоогийнхоо загварыг үлдээх" гэж шийдсэн чиглэлүүд
    if os.path.exists(dp):
        try:
            st["declined"] = json.load(open(dp))
        except ValueError:
            pass
    kp = os.path.join(d, "keep_result.json")
    if "result" not in st and os.path.exists(kp):   # зөвхөн амжилтгүй чиглэлийг дахин сургаж байх үед өмнөх амжилттай үр дүн
        st["kept"] = json.load(open(kp))
    log = os.path.join(d, "run.log")
    if os.path.exists(log):
        with open(log, "rb") as fh:  # сүүлийн 64KB (явцын мөр олон тул), эхний тасархай мөрийг хаяна
            off = max(0, os.path.getsize(log) - 65536); fh.seek(off)
            tail = fh.read().decode("utf-8", "replace").replace("\r", "\n")
            if off:
                tail = tail.split("\n", 1)[-1]
        main = "\n".join(l for l in tail.split("\n") if "\x1b[A" not in l)   # дотоод (nested, ESC[A) явцын мөрийг тооцохгүй
        prog = re.findall(r"(\d+)/(\d+) \[[^\]<]*<([^,\]]*)[^\]]*(?:it/s|s/it)\]", main)
        st["progress"] = f"{prog[-1][0]}/{prog[-1][1]}" if prog else ""
        # Ерөнхий явц: чиглэл бүр = сургалт (85%) + шалгалт (15%)
        dirs = (st.get("config") or {}).get("directions") or ["en-mn"]
        heads = re.findall(r"^=== (en-mn|mn-en): base", open(log, errors="replace").read(), re.M)
        cur = heads[-1] if heads else dirs[0]
        idx = dirs.index(cur) if cur in dirs else 0
        after = main.rsplit(f"=== {cur}: base", 1)[-1]
        cprog = re.findall(r"(\d+)/(\d+) \[[^\]]*(?:it/s|s/it)\]", after)
        evaluating = bool(re.search(r"^== |FLORES|base_bleu", after, re.M)) or (cprog and cprog[-1][0] == cprog[-1][1] and "Map" not in after.splitlines()[-1])
        frac = (int(cprog[-1][0]) / max(1, int(cprog[-1][1]))) * 0.85 if cprog and not evaluating else (0.85 if evaluating else 0)
        st["pct"] = round(100 * (idx + frac) / len(dirs))
        st["stage"] = f"{'Англи → Монгол' if cur == 'en-mn' else 'Монгол → Англи'} ({idx + 1}/{len(dirs)}): " + \
            ("шалгаж байна" if evaluating else f"сургаж байна {round(100 * int(cprog[-1][0]) / max(1, int(cprog[-1][1])))}%" if cprog else "бэлдэж байна")
        st["eta"] = _eta_mn(prog[-1][2]) if prog and not evaluating else ""
        tp = round(100 * int(cprog[-1][0]) / max(1, int(cprog[-1][1]))) if cprog else 0
        # Хугацааны тооцоо (мин): одоогийн сургалтын шат = tqdm-ийн үлдсэн хугацаа; ирэх сургалт = энэ сургалтын
        # нийт хугацаа (өнгөрсөн + үлдсэн), мэдэгдэхгүй бол ~35; шалгалт бүр 10–20
        def _mins(t):
            try:
                p = [int(x) for x in t.strip().split(":")]
            except ValueError:
                return None
            h, m, sec = ([0] * (3 - len(p)) + p)[-3:]
            return h * 60 + m + sec / 60
        bars_t = re.findall(r"\[(\d[\d:]*)<(\d[\d:]*),[^\]]*(?:it/s|s/it)\]", after)
        train_total = 35
        if bars_t:
            e_, r_ = _mins(bars_t[-1][0]), _mins(bars_t[-1][1])
            if e_ is not None and r_ is not None and e_ > 3:
                train_total = e_ + r_
        rem_lo = rem_hi = 0.0
        steps = []
        for i, dname in enumerate(dirs):
            nm = "Англи → Монгол" if dname == "en-mn" else "Монгол → Англи"
            if i < idx:
                tr, ev = "done", "done"
            elif i > idx:
                tr, ev = "todo", "todo"
            else:
                tr, ev = ("done", "run") if evaluating else ("run", "todo")
            if tr == "run":
                r_ = _mins(prog[-1][2]) if prog else None
                rem_lo += r_ if r_ is not None else train_total; rem_hi += r_ if r_ is not None else train_total
            elif tr == "todo":
                rem_lo += train_total; rem_hi += train_total
            if ev in ("run", "todo"):
                rem_lo += 10 if ev == "todo" else 5; rem_hi += 20
            steps.append({"label": f"{nm}: сургалт", "state": tr,
                          "detail": (f"{tp}%" + (f" · ~{st['eta']} үлдсэн" if st["eta"] else "")) if tr == "run" else f"~{round(train_total)} мин" if tr == "todo" else ""})
            steps.append({"label": f"{nm}: шалгалт (одоогийн загвартай харьцуулах)", "state": ev,
                          "detail": "явж байна · 10–20 мин" if ev == "run" else "~10–20 мин" if ev == "todo" else ""})
        st["steps"] = steps
        st["remaining"] = [round(rem_lo), round(rem_hi)]
        st["done"] = "=== contrib run done ===" in tail
        st["failed"] = "FAILED" in tail
        # Асуудлын төрөл (сервер унтрах, санах ой, диск, гацах): админд ойлгомжтой мессеж, арга хэмжээ харуулна
        alive = run_alive(run)
        age = time.time() - os.path.getmtime(log)
        low = tail.lower()
        st["problem"] = ""
        if "out of memory" in low or "outofmemoryerror" in low:
            st["problem"] = "oom"
        elif "no space left" in low:
            st["problem"] = "disk"
        elif st["failed"]:
            st["problem"] = "error"
        elif not st["done"] and "result" not in st and not alive:
            st["problem"] = "interrupted"
        elif alive and age > 1200:
            st["problem"] = "stalled"
        if st["problem"] in ("oom", "disk", "error", "interrupted") and not alive:
            st["failed"] = True
        lines = [l.replace("\x1b[A", "").rstrip() for l in main.splitlines() if l.replace("\x1b[A", "").strip()]
        bars = [l for l in lines if "it/s" in l or "s/it" in l]
        rest = [l for l in lines if l not in bars and not re.fullmatch(r"\s*\d+%\|.*", l)]
        rest = [l for i, l in enumerate(rest)   # "Map: 16%…" гэх мэт давтагдах явцын мөрөөс зөвхөн сүүлийнхийг үлдээнэ
                if not (re.search(r"\d+/\d+ \[", l) and i + 1 < len(rest) and rest[i + 1].split(":")[0] == l.split(":")[0])]
        st["log_tail"] = "\n".join(rest[-10:] + bars[-1:])   # сүүлийн 10 мөр + одоогийн явц
    res = st.get("result") or {}
    if res and not all(r.get("ok") for r in res.values()):
        st["partial_fail"] = True
    # Дахин эхлүүлэхэд хаанаас үргэлжлэх вэ: чиглэл бүрийн сүүлийн бүрэн хадгалсан цэг (checkpoint)
    st["resume"] = {}
    for dname in ((st.get("config") or {}).get("directions_all") or (st.get("config") or {}).get("directions") or []):
        if os.path.isdir(os.path.join(d, dname, "final")):
            continue
        cks = [c for c in glob.glob(os.path.join(d, dname, "checkpoint-*"))
               if os.path.exists(os.path.join(c, "trainer_state.json")) and os.path.exists(os.path.join(c, "optimizer.pt"))]
        if cks:
            st["resume"][dname] = max(int(c.rsplit("-", 1)[-1]) for c in cks)
    return st


SA_COOKIE = "sa"
EMAIL_OK = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")  # хашилт, < > зөвшөөрөхгүй
LOGIN_HITS = {}   # IP -> нэвтрэх оролдлогын цаг (минутад 10)


def is_admin_session(request):
    """/simple, /contribute хуудсанд "Админ самбар" руу буцах товч харуулах эсэх (эрхийг админ хуудас өөрөө шалгана)."""
    tok = request.cookies.get(SA_COOKIE)
    if not tok:
        return False
    try:
        with connect() as con:
            return bool(con.execute("SELECT 1 FROM simple_sessions WHERE token=? AND expires_at>?", (tok, _now())).fetchone())
    except sqlite3.Error:
        return False
ROLE_LABELS = {"owner": "Үндсэн админ", "reviewer": "Дата хариуцсан админ"}
WEIGHTS = {1: "Бага (×1)", 2: "Хэвийн (×2)", 3: "Өндөр (×3)", 5: "Маш өндөр (×5)"}


# ---------------------------------------------------------------- 2FA (TOTP, RFC 6238: Google Authenticator, Microsoft Authenticator г.м.)
TOTP_ISSUER = "HR Translator MN"


def totp_new_secret():
    import base64
    return base64.b32encode(secrets.token_bytes(20)).decode()


def _hotp(secret, step):
    import base64, hashlib, hmac, struct
    h = hmac.new(base64.b32decode(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    o = h[-1] & 15
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000:06d}"


def totp_check(secret, code, last_step=0):
    """Зөв бол ашигласан алхмыг (30 сек) буцаана, буруу бол 0. ±30 сек зөрүүг зөвшөөрнө, нэг кодыг 2 удаа ашиглахгүй."""
    import time
    code = re.sub(r"\s", "", code or "")
    if not re.fullmatch(r"\d{6}", code):
        return 0
    now = int(time.time() // 30)
    for st in (now - 1, now, now + 1):
        if st > last_step and secrets.compare_digest(_hotp(secret, st), code):
            return st
    return 0


def totp_row(email):
    with connect() as con:
        r = con.execute("SELECT * FROM totp WHERE email=?", (email,)).fetchone()
    return dict(r) if r else None


def totp_enabled(email):
    r = totp_row(email)
    return bool(r and r["enabled"])


def setting(key, default=""):
    with connect() as con:
        r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def parse_stats(msg):
    """Боловсруулалтын мессежийг ("хүснэгтийн 337 мөр; тэгшилсэн 381, үлдсэн 313; хассан: duplicate 12, ...") задлах."""
    st = {"aligned": None, "kept": None, "removed": {}, "notes": []}
    for part in (msg or "").split("; "):
        m = re.match(r"тэгшилсэн (\d+), үлдсэн (\d+)", part)
        if m:
            st["aligned"], st["kept"] = int(m.group(1)), int(m.group(2))
        elif part.startswith("хассан: "):
            for kv in part[8:].split(", "):
                k, _, v = kv.rpartition(" ")
                if k and v.isdigit():
                    st["removed"][k] = int(v)
        elif part and not part.startswith("таарц багатай"):
            st["notes"].append(part)
    return st


_TRAIN_DF = {}


def _train_df():
    """Сэдвийн жишээ харуулахад: сургалтын датаг (embed_train.py-тэй ижил дараалал) санах ойд нэг удаа ачаална."""
    p = os.path.join(ROOT_DIR, "data", "train_v9_ot.csv")
    key = os.path.getmtime(p)
    if _TRAIN_DF.get("key") != key:
        import pandas as pd
        _TRAIN_DF["df"] = pd.read_csv(p, keep_default_na=False, usecols=["english", "mongolian"]).drop_duplicates(["english", "mongolian"]).reset_index(drop=True)
        _TRAIN_DF["key"] = key
    return _TRAIN_DF["df"]


POOL_FILES = {"en-mn": "data/train_v9_ot.csv", "mn-en": "data/train_mn_en_v3.csv"}
_pool_cache = {}


def pool_size(direction):
    """Одоогийн загварын сургалтын датаны мөрийн тоо (бүтэн сургалтын хугацаа, шинэ датаны хувийг тооцоход)."""
    p = os.path.join(ROOT_DIR, POOL_FILES[direction])
    try:
        key = (p, os.path.getmtime(p))
    except OSError:
        return 0
    if key not in _pool_cache:
        import pandas as pd
        df = pd.read_csv(p, keep_default_na=False, usecols=["source"])
        n = len(df) if direction == "en-mn" else int((~df.source.astype(str).str.startswith("BT")).sum())
        _pool_cache.clear(); _pool_cache[key] = n
    return _pool_cache[key]


def recommend(n_new, mean_sim, low_share, pools, table_share=0.0):
    """Техникийн бус админд: датаны хэмжээ, чанараас хамаарч сургалтын тохиргоог санал болгоно.
    Буцаах нь dict(mode, weight, directions, pct, reasons[list[str]])."""
    pct = 100.0 * n_new / max(1, pools.get("en-mn", 0))
    ps = f"{pct:.1f}%" if pct >= 0.1 else "0.1%-иас бага"
    reasons = []
    mode = "full" if pct >= 10 else "add"
    if mode == "full":
        reasons.append(f"Шинэ дата одоогийн сургалтын датаны {ps} буюу 10%-иас их байна. Тиймээс бүх датаг хамтад нь дахин үзүүлэх <b>бүтэн сургалт</b> илүү сайн үр дүн өгнө.")
    else:
        reasons.append(f"Шинэ дата одоогийн сургалтын датаны <b>{ps}</b>. 10%-иас бага тул бүтэн сургалт шаардлагагүй, <b>нэмэлт сургалт</b> хангалттай бөгөөд 5–7 дахин хурдан.")
    if n_new < 1000:
        weight = 2
        reasons.append(f"Дата цөөн ({n_new:,} хос). Сургаж болно, гэхдээ сайжрал бараг мэдэгдэхгүй байж магадгүй. "
                       "1,000-аас дээш хос цуглуулаад нэг дор сургахыг зөвлөж байна.")
    elif table_share >= 0.5 and mean_sim >= 0.70:
        weight = 2
        reasons.append(f"Хосуудын {table_share * 100:.0f}% нь хүснэгтийн мөр, өөрөөр хэлбэл орчуулагч өөрөө зэрэгцүүлсэн тул найдвартай. "
                       "Тиймээс таарцын хувь бага харагдсан ч <b>Хэвийн (×2)</b> жин тохиромжтой.")
    elif mean_sim >= 0.88 and low_share < 0.05:
        weight = 2 if mode == "full" else 3
        reasons.append(f"Орчуулгын таарц маш сайн (дунджаар {mean_sim * 100:.0f}%), тиймээс "
                       + ("бүтэн сургалтад ×2 жин хангалттай." if mode == "full" else "шинэ датад <b>Өндөр (×3)</b> жин өгөхийг санал болгож байна."))
    elif mean_sim < 0.85 or low_share >= 0.2:
        weight = 1
        reasons.append(f"Таарц багатай хос нэлээд байна (дундаж {mean_sim * 100:.0f}%, 85%-иас доош {low_share * 100:.0f}%). Алдаа загварт шингэхээс сэргийлж <b>Бага (×1)</b> жинг санал болгож байна.")
    else:
        weight = 2 if mode == "add" else 1
        reasons.append(f"Таарц хэвийн (дундаж {mean_sim * 100:.0f}%), тиймээс <b>{'Хэвийн (×2)' if weight == 2 else 'Бага (×1)'}</b> жин тохиромжтой.")
    directions = "en-mn,mn-en"
    reasons.append("Дата нь хоёр хэлтэй тул <b>хоёр чиглэлийг</b> хоёуланг нь сайжруулж болно. Хугацаа хоёр дахин урт болно.")
    return dict(mode=mode, weight=weight, directions=directions, pct=round(pct, 2), reasons=reasons)


TOPIC_DIR = os.path.join(ROOT_DIR, "benchmark", "topics")


def topics_state():
    try:
        st = json.load(open(os.path.join(TOPIC_DIR, "state.json")))
    except (OSError, ValueError):
        st = None
    running = os.path.exists(os.path.join(TOPIC_DIR, "job.lock"))
    if st is not None:  # шинэ датаг шууд DB-ээс: зөвхөн босгоо давсан (сургалтад орох) хосууд
        new, pend = {}, {}
        with connect() as con:
            has = any(r[1] == "topic" for r in con.execute("PRAGMA table_info(contrib_pairs)"))
            for t, status, n in ([] if not has else con.execute(
                    f"SELECT p.topic, c.status, COUNT(*) FROM contrib_pairs p JOIN contributions c ON c.id=p.contribution_id "
                    f"WHERE c.status IN ('approved','ready') AND p.topic IS NOT NULL AND {INCLUDED} GROUP BY p.topic, c.status")):
                (new if status == "approved" else pend)[t] = (new if status == "approved" else pend).get(t, 0) + n
        st["new"], st["pending"] = new, pend
    return st, running


def start_topics_job():
    """Сэдвийн ангиллыг арын горимд дахин тооцоолно (нэг удаад нэг)."""
    if os.path.exists(os.path.join(TOPIC_DIR, "job.lock")) or not os.path.exists(os.path.join(TOPIC_DIR, "train_emb.npy")):
        return False
    subprocess.Popen([PY, os.path.join(TOPIC_DIR, "topics_job.py")], cwd=ROOT_DIR, start_new_session=True,
                     stdout=open(os.path.join(TOPIC_DIR, "job.log"), "w"), stderr=subprocess.STDOUT)
    return True


def audit(actor, action, detail=""):
    with connect() as con:
        con.execute("INSERT INTO audit(at,actor,action,detail) VALUES (?,?,?,?)", (_now(), actor, action, detail[:500]))


def register(app, page, current_user, require_admin, client_ip, set_session_cookie, templates):
    os.makedirs(UP, exist_ok=True)
    from datetime import timedelta

    def sa_user(request):
        """Энгийн админы сешн: {'email','name','role'} эсвэл None. role: owner (үндсэн) / reviewer (дата хариуцсан)."""
        tok = request.cookies.get(SA_COOKIE)
        if not tok:
            return None
        with connect() as con:
            r = con.execute("SELECT * FROM simple_sessions WHERE token=? AND expires_at>?", (tok, _now())).fetchone()
            if not r:
                return None
            if r["role"] == "reviewer":  # түр хаагдсан бол шууд гарна
                a = con.execute("SELECT active FROM simple_admins WHERE email=?", (r["email"],)).fetchone()
                if not a or not a["active"]:
                    return None
        if r["role"] == "owner":  # үндсэн админы эрх хасагдсан бол сешн хүчингүй
            with db.connect() as con:
                o = con.execute("SELECT plan, disabled FROM users WHERE email=?", (r["email"],)).fetchone()
            if not o or o["disabled"] or o["plan"] != "admin":
                return None
        return dict(email=r["email"], name=r["name"], role=r["role"], role_label=ROLE_LABELS[r["role"]],
                    setup_only=bool(r["setup_only"]), token=tok)

    def need(request, owner=False):
        u = sa_user(request)
        if not u:
            raise HTTPException(403, "Нэвтэрнэ үү.")
        if u["setup_only"]:
            raise HTTPException(403, "Эхлээд 2FA Authenticator-оо холбоно уу.")
        if owner and u["role"] != "owner":
            raise HTTPException(403, "Зөвхөн үндсэн админ.")
        return u

    def back(msg="", err="", at=""):
        """Админ самбар руу буцаана. at: тухайн хэсэг рүү гүйлгэх (#security г.м.), мэдэгдэл дээд талд гарна."""
        from urllib.parse import urlencode
        q = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
        return RedirectResponse("/simple/admin" + ("?" + q if q else "") + (f"#{at}" if at else ""), status_code=303)

    _hits = LOGIN_HITS

    @app.get("/simple/admin/login", response_class=HTMLResponse)
    def sa_login_form(request: Request):
        return templates.TemplateResponse(request, "simple_admin_login.html", {"error": None})

    @app.post("/simple/admin/login")
    def sa_login(request: Request, email: str = Form(...), password: str = Form(...)):
        import time
        ip, now = client_ip(request), time.time()
        q = [t for t in _hits.get(ip, []) if t > now - 60]
        if len(q) >= 10:
            return templates.TemplateResponse(request, "simple_admin_login.html",
                                              {"error": "Хэт олон оролдлого. 1 минутын дараа дахин оролдоно уу."}, status_code=429)
        _hits[ip] = q + [now]
        email = email.strip().lower()
        who = None
        with db.connect() as con:  # үндсэн админ = төлбөртэй сайтын админ бүртгэл
            row = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if row and not row["disabled"] and row["plan"] == "admin" and auth.check_password(password, row["password_hash"]):
                who = (row["email"], row["name"] or row["email"], "owner")
        if not who:
            with connect() as con:
                r = con.execute("SELECT * FROM simple_admins WHERE email=?", (email,)).fetchone()
                if r and r["active"] and auth.check_password(password, r["password_hash"]):
                    who = (r["email"], r["name"] or r["email"], "reviewer")
        if not who:
            return templates.TemplateResponse(request, "simple_admin_login.html",
                                              {"error": "Имэйл эсвэл нууц үг буруу, эсвэл эрх хаагдсан байна."}, status_code=401)
        if totp_enabled(who[0]):  # 2-р шат: Google Authenticator-ийн код
            tok = secrets.token_urlsafe(32)
            with connect() as con:
                con.execute("DELETE FROM pending_2fa WHERE expires_at<?", (_now(),))
                con.execute("INSERT INTO pending_2fa(token,email,name,role,expires_at) VALUES (?,?,?,?,?)",
                            (tok, *who, (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds")))
            resp = RedirectResponse("/simple/admin/login/2fa", status_code=303)
            resp.set_cookie("sa2", tok, httponly=True, samesite="strict", max_age=300,
                            secure=request.headers.get("x-forwarded-proto") == "https")
            return resp
        return start_session(request, who, setup_only=setting("require_2fa") == "1")

    def start_session(request, who, setup_only=False):
        tok = secrets.token_urlsafe(32)
        with connect() as con:
            con.execute("INSERT INTO simple_sessions(token,email,name,role,expires_at,setup_only) VALUES (?,?,?,?,?,?)",
                        (tok, *who, (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds"), int(setup_only)))
        audit(who[0], "login")
        resp = RedirectResponse("/simple/admin/2fa" if setup_only else "/simple/admin", status_code=303)
        resp.set_cookie(SA_COOKIE, tok, httponly=True, samesite="strict", max_age=7 * 86400,
                        secure=request.headers.get("x-forwarded-proto") == "https")
        resp.delete_cookie("sa2")
        return resp

    @app.get("/simple/admin/login/2fa", response_class=HTMLResponse)
    def sa_2fa_form(request: Request):
        if not request.cookies.get("sa2"):
            return RedirectResponse("/simple/admin/login", status_code=303)
        return templates.TemplateResponse(request, "simple_admin_login.html", {"error": None, "step2": True})

    @app.post("/simple/admin/login/2fa")
    def sa_2fa(request: Request, code: str = Form(...)):
        tok = request.cookies.get("sa2", "")
        with connect() as con:
            p = con.execute("SELECT * FROM pending_2fa WHERE token=? AND expires_at>?", (tok, _now())).fetchone()
            if p:
                con.execute("UPDATE pending_2fa SET tries=tries+1 WHERE token=?", (tok,))
        if not p or p["tries"] >= 5:
            if p:
                with connect() as con:
                    con.execute("DELETE FROM pending_2fa WHERE token=?", (tok,))
                audit(p["email"], "2fa_fail", "5 удаа буруу")
            return templates.TemplateResponse(request, "simple_admin_login.html",
                                              {"error": "Хугацаа дууссан эсвэл хэт олон удаа буруу оруулсан. Дахин нэвтэрнэ үү."}, status_code=401)
        t = totp_row(p["email"])
        st = totp_check(t["secret"], code, t["last_step"]) if t and t["enabled"] else 0
        if not st:
            return templates.TemplateResponse(request, "simple_admin_login.html",
                                              {"error": "Код буруу байна. Утсан дээрх хамгийн сүүлийн 6 оронтой кодыг оруулна уу.", "step2": True}, status_code=401)
        with connect() as con:
            con.execute("UPDATE totp SET last_step=? WHERE email=?", (st, p["email"]))
            con.execute("DELETE FROM pending_2fa WHERE token=?", (tok,))
        return start_session(request, (p["email"], p["name"], p["role"]))

    # ------------------------------------------------------------ 2FA тохируулах
    @app.get("/simple/admin/2fa", response_class=HTMLResponse)
    def sa_2fa_setup(request: Request, err: str = ""):
        u = sa_user(request)
        if not u:
            return RedirectResponse("/simple/admin/login", status_code=303)
        t = totp_row(u["email"])
        if t and t["enabled"]:
            return RedirectResponse("/simple/admin#security", status_code=303)
        if not t:
            with connect() as con:
                con.execute("INSERT OR REPLACE INTO totp(email,secret,enabled,created_at) VALUES (?,?,0,?)",
                            (u["email"], totp_new_secret(), _now()))
            t = totp_row(u["email"])
        from urllib.parse import quote
        import segno
        uri = f"otpauth://totp/{quote(TOTP_ISSUER)}:{quote(u['email'])}?secret={t['secret']}&issuer={quote(TOTP_ISSUER)}"
        # PNG зураг: SVG нь сайтын ерөнхий загвар (icon-ы stroke) -аас болж эвдэрч харагддаг байсан
        svg = f'<img src="{segno.make(uri, error="m").png_data_uri(scale=8, border=4)}" alt="QR код" width="220" height="220" style="image-rendering:pixelated;display:block">'
        key = " ".join(t["secret"][i:i + 4] for i in range(0, len(t["secret"]), 4))
        return templates.TemplateResponse(request, "simple_admin_2fa.html", dict(user=u, qr=svg, key=key, err=err))

    @app.post("/simple/admin/2fa/enable")
    def sa_2fa_enable(request: Request, code: str = Form(...)):
        u = sa_user(request)
        if not u:
            raise HTTPException(403, "Нэвтэрнэ үү.")
        t = totp_row(u["email"])
        st = totp_check(t["secret"], code, t["last_step"]) if t else 0
        if not st:
            from urllib.parse import quote
            return RedirectResponse("/simple/admin/2fa?err=" + quote("Код буруу байна. Апп дээрх одоогийн 6 оронтой кодыг оруулна уу."), status_code=303)
        with connect() as con:
            con.execute("UPDATE totp SET enabled=1, last_step=? WHERE email=?", (st, u["email"]))
            con.execute("UPDATE simple_sessions SET setup_only=0 WHERE email=?", (u["email"],))
        audit(u["email"], "2fa_on")
        return back(msg="2FA Authenticator холбогдлоо. Дараагийн нэвтрэлтээс эхлэн 6 оронтой код асууна.", at="security")

    @app.post("/simple/admin/2fa/disable")
    def sa_2fa_disable(request: Request, code: str = Form(...)):
        u = need(request)
        if setting("require_2fa") == "1":
            return back(err="2FA заавал байх тохиргоотой тул унтрааж болохгүй.", at="security")
        t = totp_row(u["email"])
        if not t or not t["enabled"] or not totp_check(t["secret"], code, t["last_step"]):
            return back(err="Код буруу байна.", at="security")
        with connect() as con:
            con.execute("DELETE FROM totp WHERE email=?", (u["email"],))
        audit(u["email"], "2fa_off")
        return back(msg="2FA Authenticator салгагдлаа.", at="security")

    @app.post("/simple/admin/admins/reset2fa")
    def sa_reset_2fa(request: Request, email: str = Form(...)):
        u = need(request, owner=True)
        with connect() as con:
            if not con.execute("SELECT 1 FROM simple_admins WHERE email=?", (email,)).fetchone():
                return back(err="Олдсонгүй.", at="admins")
            con.execute("DELETE FROM totp WHERE email=?", (email,))
            con.execute("DELETE FROM simple_sessions WHERE email=? AND role='reviewer'", (email,))
        audit(u["email"], "2fa_reset", email)
        return back(msg=f"{email}: 2FA цуцлагдлаа. Дараагийн нэвтрэлтэд дахин холбоно.", at="admins")

    @app.post("/simple/admin/settings/require2fa")
    def sa_require_2fa(request: Request, value: str = Form(...)):
        u = need(request, owner=True)
        on = value == "1"
        if on and not totp_enabled(u["email"]):
            return back(err="Эхлээд өөрийн бүртгэлд 2FA Authenticator холбоно уу.", at="security")
        with connect() as con:
            con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('require_2fa',?)", ("1" if on else "0",))
        audit(u["email"], "require_2fa_on" if on else "require_2fa_off")
        return back(msg="Бүх админд 2FA заавал боллоо. Холбоогүй админ дараагийн нэвтрэлтдээ эхлээд холбоно." if on else "2FA заавал биш боллоо.", at="security")

    @app.post("/simple/admin/logout")
    def sa_logout(request: Request):
        with connect() as con:
            con.execute("DELETE FROM simple_sessions WHERE token=?", (request.cookies.get(SA_COOKIE, ""),))
        resp = RedirectResponse("/simple", status_code=303)
        resp.delete_cookie(SA_COOKIE)
        return resp

    @app.post("/simple/admin/password")
    def sa_password(request: Request, old: str = Form(...), new: str = Form(...), new2: str = Form(...)):
        u = need(request)
        if u["role"] != "reviewer":
            return back(err="Үндсэн админы нууц үгийг төлбөртэй сайтын 'Миний бүртгэл' хэсгээс солино.", at="security")
        if new != new2 or len(new) < 8:
            return back(err="Шинэ нууц үг 8-аас дээш тэмдэгттэй бөгөөд хоёр удаа ижил байх ёстой.", at="security")
        with connect() as con:
            r = con.execute("SELECT * FROM simple_admins WHERE email=?", (u["email"],)).fetchone()
            if not auth.check_password(old, r["password_hash"]):
                return back(err="Одоогийн нууц үг буруу байна.", at="security")
            con.execute("UPDATE simple_admins SET password_hash=? WHERE email=?", (auth.hash_password(new), u["email"]))
        audit(u["email"], "password_change")
        return back(msg="Нууц үг солигдлоо.", at="security")

    # ------------------------------------------------------------ үндсэн админ: дата админуудыг удирдах
    @app.post("/simple/admin/admins/add")
    def sa_add(request: Request, email: str = Form(...), name: str = Form(""), password: str = Form(...)):
        u = need(request, owner=True)
        email = email.strip().lower()
        if not EMAIL_OK.fullmatch(email):
            return back(err="Зөв имэйл хаяг оруулна уу.", at="admins")
        if len(password) < 8:
            return back(err="Түр нууц үг 8-аас дээш тэмдэгттэй байх ёстой.", at="admins")
        with connect() as con:
            if con.execute("SELECT 1 FROM simple_admins WHERE email=?", (email,)).fetchone():
                return back(err="Энэ имэйлтэй дата админ аль хэдийн байна.", at="admins")
            con.execute("INSERT INTO simple_admins(email,name,password_hash,active,created_at,created_by) VALUES (?,?,?,?,?,?)",
                        (email, name.strip()[:100], auth.hash_password(password), 1, _now(), u["email"]))
        audit(u["email"], "admin_add", email)
        return back(msg=f"{email} дата админ нэмэгдлээ. Түр нууц үгийг тухайн хүнд өөрөө дамжуулна уу.", at="admins")

    @app.post("/simple/admin/admins/toggle")
    def sa_toggle(request: Request, email: str = Form(...)):
        u = need(request, owner=True)
        with connect() as con:
            r = con.execute("SELECT active FROM simple_admins WHERE email=?", (email,)).fetchone()
            if not r:
                return back(err="Олдсонгүй.", at="admins")
            con.execute("UPDATE simple_admins SET active=? WHERE email=?", (0 if r["active"] else 1, email))
            if r["active"]:
                con.execute("DELETE FROM simple_sessions WHERE email=? AND role='reviewer'", (email,))
        audit(u["email"], "admin_disable" if r["active"] else "admin_enable", email)
        return back(msg=f"{email}: {'эрх түр хаагдлаа' if r['active'] else 'эрх сэргээгдлээ'}.", at="admins")

    @app.post("/simple/admin/admins/reset")
    def sa_reset(request: Request, email: str = Form(...), password: str = Form(...)):
        u = need(request, owner=True)
        if len(password) < 8:
            return back(err="Түр нууц үг 8-аас дээш тэмдэгттэй байх ёстой.", at="admins")
        with connect() as con:
            if not con.execute("SELECT 1 FROM simple_admins WHERE email=?", (email,)).fetchone():
                return back(err="Олдсонгүй.", at="admins")
            con.execute("UPDATE simple_admins SET password_hash=? WHERE email=?", (auth.hash_password(password), email))
            con.execute("DELETE FROM simple_sessions WHERE email=? AND role='reviewer'", (email,))
        audit(u["email"], "admin_reset_password", email)
        return back(msg=f"{email}: нууц үг шинэчлэгдлээ.", at="admins")

    # ------------------------------------------------------------ хувь нэмэр оруулагч
    @app.get("/contribute", response_class=HTMLResponse)
    def contribute_page(request: Request):
        return page(request, "contribute.html", is_admin=is_admin_session(request), min_sim=MIN_SIM, max_mb=CONTRIB_MAX_MB, max_files=MAX_FILES, max_total=MAX_TOTAL_MB)

    @app.post("/api/contribute/start")
    async def contribute_start(request: Request, name: str = Form(...), email: str = Form(...), org: str = Form(""),
                               mode: str = Form(...), consent: str = Form(""),
                               file_en: UploadFile = File(None), file_mn: UploadFile = File(None),
                               file_both: UploadFile = File(None), files_many: list[UploadFile] = File(None)):
        name, email, org = name.strip()[:100], email.strip().lower()[:200], org.strip()[:150]
        if not name or not EMAIL_OK.fullmatch(email):
            raise HTTPException(400, "Нэр, зөв имэйл хаягаа оруулна уу.")
        if consent != "yes":
            raise HTTPException(400, "Зөвшөөрлийн хэсгийг тэмдэглэнэ үү.")
        cleanup_unverified()
        with connect() as con:  # нэг имэйл рүү код илгээж "бөмбөгдөх"-өөс хамгаална
            hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
            if con.execute("SELECT COUNT(*) FROM contributions WHERE email=? AND created_at>?", (email, hour_ago)).fetchone()[0] >= 5:
                raise HTTPException(429, "Энэ имэйлээр нэг цагт 5 удаа хүртэл илгээнэ. Түр хүлээнэ үү.")
        if mode == "many":
            files = [(f"f{i}", f) for i, f in enumerate(files_many or []) if f is not None and f.filename]
        else:
            files = {"two": [("en", file_en), ("mn", file_mn)], "one": [("both", file_both)]}.get(mode)
        if not files or any(f is None or not f.filename for _, f in files):
            raise HTTPException(400, "Файлаа сонгоно уу.")
        # Олон файл: ZIP-ийн доторх файлуудыг задлана (зам ашиглахгүй, зөвхөн нэр), нийт хязгаартай
        blobs, total = [], 0
        if len(files) > MAX_FILES:
            raise HTTPException(400, f"Нэг удаад {MAX_FILES} файл хүртэл.")
        for role, f in files:
            data = await f.read(MAX_TOTAL_MB * 1024 * 1024 + 1)   # ZIP нь 95 MB хүртэл; бусад файлыг доор 50 MB-аар шалгана
            fname = os.path.basename(f.filename.replace("\\", "/"))[:200]
            if mode == "many" and fname.lower().endswith(".zip"):
                import io
                import zipfile
                import zlib
                try:
                    z = zipfile.ZipFile(io.BytesIO(data))
                except zipfile.BadZipFile:
                    raise HTTPException(400, f"ZIP файл эвдэрсэн байна: {fname}")
                # "zip bomb" хамгаалалт: толгой дээрх хэмжээнд итгэхгүй, задлах явцад нийт хэмжээ, файлын тоог шалгана
                for zi in z.infolist():
                    zn = os.path.basename(zi.filename)
                    if zi.is_dir() or zn.startswith(".") or os.path.splitext(zn)[1].lower() not in ALLOWED:
                        continue
                    if len(blobs) >= MAX_FILES:
                        raise HTTPException(400, f"Нэг удаад {MAX_FILES} файл хүртэл.")
                    room = min(CONTRIB_MAX_MB * 1024 * 1024, MAX_TOTAL_MB * 1024 * 1024 - total)
                    if zi.file_size > room:
                        raise HTTPException(400, f"ZIP доторх файл хэт том, эсвэл нийт хэмжээ {MAX_TOTAL_MB} MB-аас хэтэрлээ: {zn}")
                    try:
                        with z.open(zi) as fz:
                            b = fz.read(room + 1)
                    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error):
                        raise HTTPException(400, f"ZIP доторх файлыг уншиж чадсангүй: {zn}")
                    if len(b) > room:
                        raise HTTPException(400, f"ZIP доторх файл хэт том, эсвэл нийт хэмжээ {MAX_TOTAL_MB} MB-аас хэтэрлээ: {zn}")
                    total += len(b)
                    blobs.append((zn, b))
                continue
            if os.path.splitext(fname)[1].lower() not in ALLOWED:
                if mode == "many" and fname.startswith("."):
                    continue  # хавтас дотрох далд файл (.DS_Store г.м.)
                raise HTTPException(400, f"Зөвхөн PDF, DOCX, DOC, ODT, RTF, TXT, CSV, XLSX{', ZIP' if mode == 'many' else ''} файл: {fname}")
            if len(data) > CONTRIB_MAX_MB * 1024 * 1024:
                raise HTTPException(400, f"Файл {CONTRIB_MAX_MB} MB-аас ихгүй байх ёстой: {fname}")
            total += len(data)
            if total > MAX_TOTAL_MB * 1024 * 1024:
                raise HTTPException(400, f"Нийт хэмжээ {MAX_TOTAL_MB} MB-аас ихгүй байх ёстой.")
            blobs.append((fname, data))
        if mode == "many" and not 2 <= len(blobs) <= MAX_FILES:
            raise HTTPException(400, f"Олон файлын горимд 2–{MAX_FILES} файл оруулна уу (одоо {len(blobs)}).")
        if sum(len(b) for _, b in blobs) > MAX_TOTAL_MB * 1024 * 1024:
            raise HTTPException(400, f"Нийт хэмжээ {MAX_TOTAL_MB} MB-аас ихгүй байх ёстой.")
        cid = secrets.token_urlsafe(9)
        d = os.path.join(UP, cid)
        os.makedirs(d)
        saved = []
        roles = [r for r, _ in files] if mode != "many" else [f"f{i}" for i in range(len(blobs))]
        for role, (fname, data) in zip(roles, blobs):
            fn = f"{role}{os.path.splitext(fname)[1].lower()}"
            open(os.path.join(d, fn), "wb").write(data)
            saved.append({"role": role, "file": fn, "name": fname})
        with connect() as con:
            con.execute("INSERT INTO contributions(id,name,email,org,mode,files,consent_at,ip,status,created_at,min_sim) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,0.85)", (cid, name, email, org, mode, json.dumps(saved, ensure_ascii=False),
                                                         _now(), client_ip(request), "unverified", _now()))
        with db.connect() as con:
            ok, msg = otp.send_code(con, email, "contribute", cid)
            dev = con.execute("SELECT code FROM otp_dev WHERE purpose='contribute' AND data=?", (cid,)).fetchone() \
                if ok and not mailer.configured() else None
        if not ok:
            raise HTTPException(429, msg)
        if dev:  # туршилтын горим: 8080 (нийтийн) сервер өөр app.db-тэй тул кодыг хуваалцсан contrib.db-д хуулна
            with connect() as con:
                con.execute("CREATE TABLE IF NOT EXISTS dev_codes (cid TEXT PRIMARY KEY, code TEXT)")
                con.execute("INSERT OR REPLACE INTO dev_codes VALUES (?,?)", (cid, dev["code"]))
        return {"id": cid}

    @app.post("/api/contribute/verify")
    async def contribute_verify(request: Request):
        try:
            data = await request.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise HTTPException(400, "Буруу хүсэлт.")
        cid, code = str(data.get("id", ""))[:40], str(data.get("code", ""))[:10]
        with connect() as con:
            c = con.execute("SELECT * FROM contributions WHERE id=?", (cid,)).fetchone()
        if not c or c["status"] != "unverified":
            raise HTTPException(404, "Илгээлт олдсонгүй.")
        with db.connect() as con:
            ok, got = otp.check_code(con, c["email"], "contribute", code)
        if not ok or got != cid:
            raise HTTPException(400, got if not ok else "Код буруу байна.")
        with connect() as con:
            con.execute("UPDATE contributions SET status='queued', verified_at=? WHERE id=?", (_now(), cid))
        start_processing(cid)
        return {"ok": True}

    # ------------------------------------------------------------ админ самбар
    @app.get("/simple/admin", response_class=HTMLResponse)
    def contrib_admin(request: Request, view: str = "", s: str = "", msg: str = "", err: str = ""):
        # s: "Өөр жишээ" товч URL-ийг өөрчилж хуудсыг дахин ачаалуулна
        user = sa_user(request)
        if not user:
            return RedirectResponse("/simple/admin/login", status_code=303)
        if user["setup_only"]:
            return RedirectResponse("/simple/admin/2fa", status_code=303)
        with connect() as con:
            rows = [dict(r) for r in con.execute("SELECT * FROM contributions WHERE status!='unverified' ORDER BY created_at DESC")]
            tfa = {r["email"] for r in con.execute("SELECT email FROM totp WHERE enabled=1")}
            sample = [dict(r) for r in con.execute(
                "SELECT * FROM contrib_pairs WHERE contribution_id=? ORDER BY RANDOM() LIMIT 12", (view,))] if view else []
            approved = con.execute("SELECT COUNT(*) FROM contrib_pairs p JOIN contributions c ON c.id=p.contribution_id "
                                   f"WHERE c.status='approved' AND {INCLUDED}").fetchone()[0]
            pending = [dict(r) for r in con.execute(
                "SELECT id,name,email,created_at FROM contributions WHERE status='unverified' ORDER BY created_at DESC LIMIT 20")]
            admins = [dict(r) for r in con.execute("SELECT email,name,active,created_at,created_by FROM simple_admins ORDER BY created_at")] \
                if user["role"] == "owner" else []
            codes = {}
            if not mailer.configured():  # туршилтын горим: имэйл явахгүй тул кодыг энд харуулна (8000 ба 8080-ийн аль алинаас)
                try:
                    codes = {r["cid"]: r["code"] for r in con.execute("SELECT cid, code FROM dev_codes")}
                except sqlite3.OperationalError:
                    pass
        for r in rows:
            r["files"] = json.loads(r["files"])
        for p in pending:
            p["code"] = codes.get(p["id"], "")
        runs = sorted(os.listdir(JOBS), reverse=True)[:8] if os.path.isdir(JOBS) else []
        try:
            active = json.load(open(translator.ACTIVE_FILE))
        except (OSError, ValueError):
            active = {}
        bench = os.path.join(ROOT_DIR, "benchmark")
        current = {d: os.path.relpath(translator.model_path(d), bench) for d in ("en-mn", "mn-en")}
        updated = {}
        for d in ("en-mn", "mn-en"):  # загвар хамгийн сүүлд хэзээ шинэчлэгдсэн: сайтаас шилжүүлсэн огноо, эсвэл загварын хавтасны огноо
            at = (active.get(d) or {}).get("at")
            if not at:
                try:
                    at = datetime.fromtimestamp(os.path.getmtime(os.path.join(translator.model_path(d), "config.json")), timezone.utc).isoformat(timespec="seconds")
                except OSError:
                    at = ""
            updated[d] = at[:10]
        with connect() as con:
            q = con.execute("SELECT AVG(p.labse_sim), AVG(CASE WHEN p.labse_sim < 0.85 AND p.is_table=0 THEN 1.0 ELSE 0 END), AVG(p.is_table) FROM contrib_pairs p "
                            f"JOIN contributions c ON c.id=p.contribution_id WHERE c.status='approved' AND {INCLUDED}").fetchone()
            prev_n = con.execute("SELECT COUNT(*) FROM contrib_pairs p JOIN contributions c ON c.id=p.contribution_id "
                                 f"WHERE c.status='trained' AND {INCLUDED}").fetchone()[0]
        pools = {d: pool_size(d) for d in ("en-mn", "mn-en")}
        # Одоогийн нийт сургалтын дата = үндсэн файл + өмнө нь сургалтад орсон бүх илгээлт (дата нэмэгдэх тусам өснө)
        totals = {d: n + prev_n for d, n in pools.items()}
        rec = recommend(approved, q[0] or 0, q[1] or 0, totals, q[2] or 0) if approved else None
        tstate, trunning = topics_state()
        import topics as TP
        with connect() as con:
            TP.init(con)
            tlist = TP.active_topics(con)
        if rec:
            rec["pct_str"] = f"{rec['pct']:.1f}%" if rec["pct"] >= 0.1 else "0.1%-иас бага"
        # Бодит хурд: v8/v9 сургалтын лог дээр секундэд ~19.2 мөр (явцын шалгалтыг оруулаад) + эцсийн 5 тест ~20 мин
        plan = dict(new=approved, replay_min=20000, replay_factor=5, replay_max=max(pools.values()), rows_per_sec=19.2, sec_per_step=0.85, eval_min=20, pools=pools, prev=prev_n, totals=totals,
                    replay_src=POOL_FILES)
        return templates.TemplateResponse(request, "contrib_admin.html", dict(
            custom_tests=custom_tests(), builtin_tests=BUILTIN_TESTS, tests_off=tests_off(), ext=ext_rows(), ext_sets=ext_test_sets(),
            mlabel={d: model_label(d, translator.model_path(d)) for d in ("en-mn", "mn-en")},
            mprev={d: model_label(d, (active or {}).get(d, {}).get("previous")) for d in ("en-mn", "mn-en") if (active or {}).get(d, {}).get("previous")},
            model_missing=[d for d in ("en-mn", "mn-en") if (active or {}).get(d, {}).get("path") and not os.path.isdir((active or {})[d]["path"])], model_hist={d: model_options(d, active, current) for d in ("en-mn", "mn-en")},
            user=user, pending=pending, test_mode=False, plan=plan, weights=WEIGHTS,  # TODO: revert to `not mailer.configured()` — "Баталгаажуулах кодууд" хэсгийг түр нуусан
            rows=rows, view=view, sample=sample, approved=approved, admins=admins, msg=msg, err=err,
            runs=[run_status(r) for r in runs], status_labels=STATUS_LABELS, current=current, updated=updated,
            tfa=tfa, my_2fa=user["email"] in tfa, require_2fa=setting("require_2fa") == "1",
            active=active, busy=gpu_busy(), min_sim=MIN_SIM, rec=rec, tstate=tstate, trunning=trunning, tlist=tlist))

    @app.get("/simple/admin/run_status")
    def contrib_run_status(request: Request, run: str):
        """Явж буй сургалтын явц, лог (админ хуудас хэдэн секунд тутам асууна)."""
        u = sa_user(request)
        if not u or u.get("setup_only"):
            raise HTTPException(403, "Нэвтрэх шаардлагатай.")
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,80}", run) or not os.path.isdir(os.path.join(JOBS, run)):
            raise HTTPException(404, "Олдсонгүй.")
        st = run_status(run)
        return {"progress": st.get("progress", ""), "pct": st.get("pct", 0), "stage": st.get("stage", ""), "eta": st.get("eta", ""), "steps": st.get("steps", []), "remaining": st.get("remaining", [0, 0]), "problem": st.get("problem", ""), "log_tail": st.get("log_tail", ""), "failed": st.get("failed", False),
                "done": bool(st.get("result")) or st.get("done", False)}

    @app.get("/simple/admin/sample")
    def contrib_sample(request: Request, cid: str, lo: float = 0, hi: float = 2):
        """Жишээ хосуудыг цонхонд харуулах. lo/hi: таарцын завсар (слайдераар шинээр нэмэгдэх хосыг харах)."""
        u = sa_user(request)
        if not u or u["setup_only"]:
            raise HTTPException(403, "Нэвтэрнэ үү.")
        with connect() as con:
            r = con.execute("SELECT name, message, n_pairs, min_sim, status, mode, files FROM contributions WHERE id=?", (cid,)).fetchone()
            if not r:
                raise HTTPException(404, "Олдсонгүй.")
            if lo > 0 or hi < 2:
                q = "SELECT * FROM contrib_pairs WHERE contribution_id=? AND labse_sim>=? AND labse_sim<? ORDER BY RANDOM() LIMIT 12"
                rows = con.execute(q, (cid, lo - 1e-9, hi - 1e-9)).fetchall()
            else:
                rows = con.execute(f"SELECT p.* FROM contrib_pairs p JOIN contributions c ON c.id=p.contribution_id "
                                   f"WHERE p.contribution_id=? AND {INCLUDED} ORDER BY RANDOM() LIMIT 12", (cid,)).fetchall()
            sims = [[round(x[0], 4), x[1]] for x in con.execute("SELECT labse_sim, is_table FROM contrib_pairs WHERE contribution_id=?", (cid,))]
        pairs = [dict(english=p["english"], mongolian=p["mongolian"], sim=round(p["labse_sim"] * 100), table=bool(p["is_table"])) for p in rows]
        try:
            fnames = [f.get("name") or f.get("file") for f in json.loads(r["files"] or "[]")]
        except ValueError:
            fnames = []
        return {"files": fnames, "mode": r["mode"], "name": r["name"], "message": r["message"], "stats": parse_stats(r["message"]), "n_pairs": r["n_pairs"], "min_sim": r["min_sim"], "status": r["status"],
                "default": MIN_SIM, "table_min": TABLE_MIN, "sims": sims, "pairs": pairs}

    @app.get("/simple/admin/audit")
    def contrib_audit(request: Request, scope: str = "reviewer", before: int = 0, limit: int = 50,
                      q: str = "", act: str = "", dfrom: str = "", dto: str = ""):
        """Үйлдлийн түүх (доош гүйлгэхэд цааш нь ачаална). scope=owner: үндсэн админы түүх (зөвхөн үндсэн админ харна),
        scope=reviewer: дата хариуцсан админуудын түүх (бүх админ харна)."""
        u = sa_user(request)
        if not u or u["setup_only"]:
            raise HTTPException(403, "Нэвтэрнэ үү.")
        if scope not in ("all", "owner", "reviewer") or (scope != "reviewer" and u["role"] != "owner"):
            raise HTTPException(403, "Энэ түүхийг зөвхөн үндсэн админ харна.")
        limit = max(1, min(limit, 200))
        cond = {"all": "1=1", "reviewer": "actor IN (SELECT email FROM simple_admins)",
                "owner": "actor NOT IN (SELECT email FROM simple_admins)"}[scope]
        # Шүүлтүүр: хайлт (хэн / дэлгэрэнгүй), үйлдлийн төрөл, огноо (UTC, YYYY-MM-DD)
        fsql, fargs = "", []
        for w in q.lower().split()[:6]:
            fsql += " AND (pylower(actor) LIKE ? OR pylower(detail) LIKE ?)"; fargs += [f"%{w}%", f"%{w}%"]   # SQLite lower() кирилл үсгийг жижигрүүлдэггүй
        if act and re.fullmatch(r"[a-z0-9_]{1,40}", act):
            fsql += " AND action=?"; fargs.append(act)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", dfrom or ""):
            fsql += " AND substr(at,1,10)>=?"; fargs.append(dfrom)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", dto or ""):
            fsql += " AND substr(at,1,10)<=?"; fargs.append(dto)
        sql = (f"SELECT id, at, actor, action, detail FROM audit WHERE {cond}{fsql}"
               + (" AND id<?" if before else "") + " ORDER BY id DESC LIMIT ?")
        with connect() as con:
            con.create_function("pylower", 1, lambda x: (x or "").lower(), deterministic=True)
            rows = [dict(r) for r in con.execute(sql, tuple(fargs) + ((before,) if before else ()) + (limit + 1,))]
            total = con.execute(f"SELECT COUNT(*) FROM audit WHERE {cond}{fsql}", tuple(fargs)).fetchone()[0]
            acts = [r[0] for r in con.execute(f"SELECT DISTINCT action FROM audit WHERE {cond} ORDER BY action")]
            admins = {r[0] for r in con.execute("SELECT email FROM simple_admins")}
        for r in rows:
            r["role"] = "reviewer" if r["actor"] in admins else "owner"
        return {"rows": rows[:limit], "more": len(rows) > limit, "total": total, "actions": acts}

    @app.post("/simple/admin/threshold")
    def contrib_threshold(request: Request, cid: str = Form(...), min_sim: float = Form(...)):
        """Илгээлтийн LaBSE босгыг өөрчилнө (слайдер, -1 = автомат). Сургалтад орох хосын тоо шууд шинэчлэгдэнэ."""
        u = need(request)
        if min_sim != -1 and not TABLE_MIN - 1e-9 <= min_sim <= 0.99:
            raise HTTPException(400, f"Босго {int(TABLE_MIN * 100)}–99% хооронд байна.")
        min_sim = MIN_SIM if min_sim == -1 else round(min_sim, 2)  # хуучин "автомат" = санал болгох 85%
        with connect() as con:
            r = con.execute("SELECT status FROM contributions WHERE id=?", (cid,)).fetchone()
            if not r:
                raise HTTPException(404, "Олдсонгүй.")
            if r["status"] in ("trained", "processing", "queued", "unverified"):
                raise HTTPException(409, "Энэ төлөвт босгыг өөрчлөх боломжгүй.")
            con.execute("UPDATE contributions SET min_sim=? WHERE id=?", (min_sim, cid))
            n = con.execute(f"SELECT COUNT(*) FROM contrib_pairs p JOIN contributions c ON c.id=p.contribution_id "
                            f"WHERE p.contribution_id=? AND {INCLUDED}", (cid,)).fetchone()[0]
            con.execute("UPDATE contributions SET n_pairs=? WHERE id=?", (n, cid))
        audit(u["email"], "threshold", f"{cid}: {'автомат' if min_sim < 0 else str(int(round(min_sim * 100))) + '%'} → {n} хос")
        return {"ok": True, "n_pairs": n, "min_sim": min_sim}

    @app.post("/simple/admin/decide")
    def contrib_decide(request: Request, cid: str = Form(...), action: str = Form(...)):
        u = need(request)
        new = {"approve": "approved", "reject": "rejected", "retry": "queued", "restore": "trained"}.get(action)
        if not new:
            raise HTTPException(400, "Буруу үйлдэл.")
        with connect() as con:
            r = con.execute("SELECT name, trained_in FROM contributions WHERE id=?", (cid,)).fetchone()
            if not r:
                raise HTTPException(404, "Олдсонгүй.")
            if action == "restore" and not r["trained_in"]:   # зөвхөн өмнө нь сургалтад орсон илгээлтийг буцаана
                return back(err="Энэ илгээлт өмнө нь сургалтад ороогүй тул буцаах төлөв алга.")
            con.execute("UPDATE contributions SET status=?, decided_at=?, decided_by=? WHERE id=?", (new, _now(), u["email"], cid))
        audit(u["email"], action, f"{cid} ({r['name']})" + (f" → {r['trained_in']}" if action == "restore" else ""))
        if action == "restore":
            return back(msg=f"\"{r['name']}\" илгээлт \"Сургалтад орсон\" төлөвт буцлаа.")
        if new == "queued":
            start_processing(cid)
        return back()

    @app.post("/simple/admin/train")
    def contrib_train(request: Request, directions: str = Form("en-mn"), weight: int = Form(2), mode: str = Form("add")):
        u = need(request)
        if mode not in ("add", "full"):
            raise HTTPException(400, "Сургалтын төрөл буруу.")
        if gpu_busy():
            raise HTTPException(409, "Өөр сургалт явж байна. Дууссаны дараа дахин оролдоно уу.")
        if weight not in WEIGHTS:
            raise HTTPException(400, "Жин буруу байна.")
        dirs = [d for d in directions.split(",") if d in ("en-mn", "mn-en")]
        if not dirs:
            raise HTTPException(400, "Чиглэлээ сонгоно уу.")
        with connect() as con:
            rows = con.execute("SELECT p.english, p.mongolian, p.labse_sim, c.id AS cid, c.org, c.name FROM contrib_pairs p "
                               f"JOIN contributions c ON c.id=p.contribution_id WHERE c.status='approved' AND {INCLUDED}").fetchall()
        if not rows:
            raise HTTPException(400, "Зөвшөөрсөн шинэ дата алга.")
        seen_pairs, uniq = set(), []   # өөр өөр илгээлтэд ижил хос байвал (жишээ нь ижил датаг DOCX ба CSV-ээр) нэг л удаа
        for r in rows:
            k = (r["english"].strip(), r["mongolian"].strip())
            if k not in seen_pairs:
                seen_pairs.add(k); uniq.append(r)
        rows = uniq
        run = datetime.now().strftime("contrib_%Y%m%d_%H%M%S")
        while os.path.exists(os.path.join(JOBS, run)):  # нэг секундэд 2 удаа дарсан ч давхцахгүй
            run += "b"
        d = os.path.join(JOBS, run)
        os.makedirs(d)
        import csv
        with open(os.path.join(d, "new_pairs.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["english", "mongolian", "source", "labse_sim"])
            for r in rows:
                w.writerow([r["english"], r["mongolian"], f"Contrib: {(r['org'] or r['name'])[:40]}", f"{r['labse_sim']:.4f}"])
        if True:  # өмнө нь сургалтад орсон илгээлтүүдийн хос: бүтэн сургалтад бүгдийг, нэмэлт сургалтад давтлагын дээжид оруулна
            with connect() as con:
                prev = con.execute("SELECT p.english, p.mongolian, p.labse_sim, c.org, c.name FROM contrib_pairs p JOIN contributions c "
                                   f"ON c.id=p.contribution_id WHERE c.status='trained' AND {INCLUDED}").fetchall()
            with open(os.path.join(d, "prev_pairs.csv"), "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["english", "mongolian", "source", "labse_sim"])
                for r in prev:
                    if (r["english"].strip(), r["mongolian"].strip()) not in seen_pairs:
                        w.writerow([r["english"], r["mongolian"], f"Contrib: {(r['org'] or r['name'])[:40]}", f"{r['labse_sim']:.4f}"])
        cfg = {"run": run, "directions": dirs, "n_new": len(rows), "weight": weight, "mode": mode, "by": u["email"],
               "contributions": sorted({r["cid"] for r in rows}),
               "base": {k: translator.model_path(k) for k in dirs}, "started": _now()}
        json.dump(cfg, open(os.path.join(d, "config.json"), "w"), ensure_ascii=False, indent=1)
        subprocess.Popen(["bash", os.path.join(ROOT_DIR, "benchmark", "run_contrib.sh"), run], cwd=ROOT_DIR,
                         stdout=open(os.path.join(d, "run.log"), "w"), stderr=subprocess.STDOUT, start_new_session=True)
        with connect() as con:
            for cid in cfg["contributions"]:
                con.execute("UPDATE contributions SET status='trained', trained_in=?, trained_by=? WHERE id=?", (run, u["email"], cid))
        audit(u["email"], "train", f"{run}: {'бүтэн' if mode == 'full' else 'нэмэлт'} сургалт, {', '.join(dirs)}, {len(rows)} хос, жин ×{weight}")
        return back(msg="Сургалт эхэллээ. Явцыг доорх 'Сургалтын түүх' хэсгээс харна уу.")

    # ------------------------------------------------------------ сэдвүүд (агуулгаар бүлэглэх)
    import topics as TP

    @app.post("/simple/admin/topics/add")
    def topic_add(request: Request, name: str = Form(...), examples: str = Form(...)):
        u = need(request)
        name = name.strip()[:60]
        ex = _kwlist(examples)
        if not name or len(ex) < 2:
            return back(err="Сэдвийн нэр болон дор хаяж 2 түлхүүр үг (таслалаар эсвэл мөр бүрт нэг) оруулна уу.", at="topics")
        tid = "t" + secrets.token_hex(4)
        with connect() as con:
            TP.init(con)
            con.execute("INSERT INTO topics(id,name,examples,created_at,created_by) VALUES (?,?,?,?,?)",
                        (tid, name, json.dumps(ex, ensure_ascii=False), _now(), u["email"]))
        audit(u["email"], "topic_add", name)
        start_topics_job()
        return back(msg=f"\"{name}\" сэдэв нэмэгдлээ. Датаг дахин ангилж байна (хэдэн минут).", at="topics")

    def _kwlist(text):
        return list(dict.fromkeys(w.strip().lower()[:40] for w in re.split(r"[,;\n]", text) if len(w.strip()) >= 2))[:80]

    @app.post("/simple/admin/topics/keywords")
    def topic_keywords(request: Request, tid: str = Form(...), keywords: str = Form(...)):
        u = need(request)
        kw = _kwlist(keywords)
        if len(kw) < 2:
            return back(err="Дор хаяж 2 түлхүүр үг оруулна уу.", at="topics")
        with connect() as con:
            TP.init(con)
            old = con.execute("SELECT name, examples FROM topics WHERE id=? AND active=1", (tid,)).fetchone()
            if not old:
                return back(err="Сэдэв олдсонгүй.", at="topics")
            try:
                same = sorted(json.loads(old["examples"] or "[]")) == sorted(kw)
            except ValueError:
                same = False
            if same:  # юу ч өөрчлөөгүй: дахин ангилах, түүхэнд бичих шаардлагагүй
                return back(msg="Өөрчлөлт алга, түлхүүр үгс хэвээр.", at="topics")
            con.execute("UPDATE topics SET examples=? WHERE id=?", (json.dumps(kw, ensure_ascii=False), tid))
        audit(u["email"], "topic_keywords", f"{old['name']}: {len(kw)} үг")
        start_topics_job()
        return back(msg=f"\"{old['name']}\" сэдвийн түлхүүр үгс шинэчлэгдлээ. Датаг дахин ангилж байна.", at="topics")

    @app.post("/simple/admin/topics/rename")
    def topic_rename(request: Request, tid: str = Form(...), name: str = Form(...)):
        u = need(request)
        name = name.strip()[:60]
        if not name:
            return back(err="Нэр хоосон байж болохгүй.", at="topics")
        with connect() as con:
            TP.init(con)
            old = con.execute("SELECT name FROM topics WHERE id=? AND active=1", (tid,)).fetchone()
            if not old:
                return back(err="Сэдэв олдсонгүй.", at="topics")
            con.execute("UPDATE topics SET name=? WHERE id=?", (name, tid))
        st, _ = topics_state()
        if st and tid in st.get("names", {}):   # нэр солиход дата хэвээр, зөвхөн харагдах нэр шинэчлэгдэнэ
            st["names"][tid] = name
            json.dump(st, open(os.path.join(TOPIC_DIR, "state.json"), "w"), ensure_ascii=False)
        audit(u["email"], "topic_rename", f"{old['name']} → {name}")
        return back(msg=f"Нэр солигдлоо: {old['name']} → {name}. Дата нь хэвээр.", at="topics")

    @app.post("/simple/admin/topics/delete")
    def topic_delete(request: Request, tid: str = Form(...)):
        u = need(request)
        with connect() as con:
            TP.init(con)
            old = con.execute("SELECT name FROM topics WHERE id=? AND active=1", (tid,)).fetchone()
            if not old:
                return back(err="Сэдэв олдсонгүй.", at="topics")
            con.execute("UPDATE topics SET active=0 WHERE id=?", (tid,))   # түүх хадгалагдана, дахин сэргээж болно
        audit(u["email"], "topic_delete", old["name"])
        start_topics_job()
        return back(msg=f"\"{old['name']}\" сэдэв устгагдлаа. Түүний дата хамгийн ойр сэдэв рүү эсвэл \"Бусад\" руу шилжиж байна.", at="topics")

    @app.post("/simple/admin/topics/suggest")
    def topic_suggest(request: Request, sid: str = Form(...), action: str = Form(...), name: str = Form("")):
        u = need(request)
        st, _ = topics_state()
        sg = next((x for x in (st or {}).get("suggestions", []) if x["sid"] == sid), None)
        if not sg:
            return back(err="Санал олдсонгүй (аль хэдийн шинэчлэгдсэн байж магадгүй).", at="topics")
        with connect() as con:
            TP.init(con)
            if action == "accept":
                name = name.strip()[:60]
                if not name:
                    return back(err="Шинэ сэдвийн нэрийг оруулна уу.", at="topics")
                con.execute("INSERT INTO topics(id,name,examples,created_at,created_by) VALUES (?,?,?,?,?)",
                            ("t" + secrets.token_hex(4), name, json.dumps(sg["seeds"], ensure_ascii=False), _now(), u["email"]))
            elif action == "dismiss":
                con.execute("INSERT OR REPLACE INTO topic_suggest_dismissed(id,centroid,at,by) VALUES (?,?,?,?)",
                            (sid, json.dumps(sg["centroid"]), _now(), u["email"]))
            else:
                raise HTTPException(400, "Буруу үйлдэл.")
        st["suggestions"] = [x for x in st["suggestions"] if x["sid"] != sid]
        json.dump(st, open(os.path.join(TOPIC_DIR, "state.json"), "w"), ensure_ascii=False)
        audit(u["email"], "topic_suggest_" + action, name or ", ".join(sg["keywords"][:4]))
        if action == "accept":
            start_topics_job()
            return back(msg=f"\"{name}\" шинэ сэдэв нэмэгдлээ. Датаг дахин ангилж байна.", at="topics")
        return back(msg="Санал хаагдлаа. Энэ бүлэг \"Бусад\"-д үлдэнэ.", at="topics")

    @app.post("/simple/admin/topics/refresh")
    def topic_refresh(request: Request):
        u = need(request)
        ok = start_topics_job()
        return back(msg="Датаг дахин ангилж байна (хэдэн минут)." if ok else "", err="" if ok else "Ангилал аль хэдийн явж байна.", at="topics")

    @app.get("/simple/admin/topics/sample")
    def topic_sample(request: Request, tid: str):
        u = sa_user(request)
        if not u or u["setup_only"]:
            raise HTTPException(403, "Нэвтэрнэ үү.")
        st, _ = topics_state()
        if not st or tid not in st["order"]:
            raise HTTPException(404, "Олдсонгүй.")
        import numpy as np
        idx = np.load(os.path.join(TOPIC_DIR, "train_topics.npy"))
        rows = np.where(idx == st["order"].index(tid))[0]
        pick = sorted(np.random.default_rng().choice(rows, min(10, len(rows)), replace=False).tolist()) if len(rows) else []
        df = _train_df()
        out = [dict(english=df.english[r], mongolian=df.mongolian[r]) for r in pick]
        with connect() as con:
            new = [dict(r) for r in con.execute("SELECT p.english, p.mongolian FROM contrib_pairs p JOIN contributions c ON c.id=p.contribution_id "
                                                "WHERE p.topic=? AND c.status IN ('ready','approved') ORDER BY RANDOM() LIMIT 5", (tid,))]
        return {"name": st["names"].get(tid, tid), "train": out, "new": new}

    @app.post("/simple/admin/tests/add")
    async def tests_add(request: Request, name: str = Form(...), file: UploadFile = File(...)):
        """Үндсэн админ шалгалтын материал (2 хэлтэй CSV/XLSX) нэмнэ. Сургалтаас үргэлж хасагдана."""
        u = need(request, owner=True)
        ext = os.path.splitext(file.filename or "")[1].lower()
        if ext not in (".csv", ".xlsx"):
            return back(err="Зөвхөн CSV эсвэл XLSX файл (англи, монгол 2 багана).", at="tests")
        data = await file.read(20 * 1024 * 1024 + 1)
        if len(data) > 20 * 1024 * 1024:
            return back(err="Файл 20 MB-аас их байна.", at="tests")
        import tempfile, contrib_process as CP, pandas as pd
        with tempfile.NamedTemporaryFile(suffix=ext) as tf:
            tf.write(data); tf.flush()
            try:
                rows = CP.sheet_rows(tf.name)
            except Exception:  # noqa: BLE001
                rows = []
        rows = list(dict.fromkeys((e.strip(), m.strip()) for e, m in rows if e.strip() and m.strip()))[:5000]
        if len(rows) < 20:
            return back(err=f"Хос хэт цөөн ({len(rows)}). Шалгалтад дор хаяж 20 хос хэрэгтэй (100+ бол найдвартай).", at="tests")
        os.makedirs(CUSTOM_TESTS, exist_ok=True)
        slug = "custom_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        pd.DataFrame(rows, columns=["english", "mongolian"]).to_csv(os.path.join(CUSTOM_TESTS, slug + ".csv"), index=False)
        nm = (name.strip() or file.filename or slug)[:80]
        json.dump(dict(name=nm, n=len(rows), at=_now(), by=u["email"], src=file.filename), open(os.path.join(CUSTOM_TESTS, slug + ".json"), "w"), ensure_ascii=False)
        audit(u["email"], "test_add", f"{nm}: {len(rows)} хос")
        return back(msg=f"Шалгалтын материал \"{nm}\" нэмэгдлээ ({len(rows)} хос). Дараагийн сургалтаас эхлэн шалгана, сургалтад хэзээ ч орохгүй.", at="tests")

    @app.post("/simple/admin/train/retry")
    def train_retry(request: Request, run: str = Form(...)):
        """Тасарсан/амжилтгүй сургалтыг ижил тохиргоо, ижил датаар дахин эхлүүлнэ (хуучин лог хадгалагдана)."""
        u = need(request)
        d = os.path.join(JOBS, os.path.basename(run))
        if not re.fullmatch(r"contrib_\d{8}_\d{6}", run) or not os.path.isdir(d):
            return back(err="Сургалт олдсонгүй.")
        if gpu_busy():
            return back(err="Өөр сургалт эсвэл GPU ажил явж байна. Дууссаны дараа дахин оролдоно уу.")
        st = run_status(run)
        if not (st.get("failed") or st.get("partial_fail")):
            return back(err="Энэ сургалтыг дахин эхлүүлэх шаардлагагүй.")
        free = gpu_free_mb()
        if free is not None and free < 14000:
            return back(err=f"GPU-д сул санах ой хүрэлцэхгүй байна ({free // 1024} GB сул, ~14 GB хэрэгтэй). GPU ашиглаж буй бусад програмыг хаагаад дахин оролдоно уу.")
        n = 1
        while os.path.exists(os.path.join(d, f"run.log.{n}")):
            n += 1
        old = (st.get("result") or {})
        keep = {k: v for k, v in old.items() if v.get("ok")}
        if keep:   # амжилттай чиглэлийг дахин сургахгүй, зөвхөн амжилтгүйг нь
            cfg = json.load(open(os.path.join(d, "config.json")))
            cfg.setdefault("directions_all", cfg["directions"])
            cfg["directions"] = [x for x in cfg["directions_all"] if x not in keep]
            json.dump(cfg, open(os.path.join(d, "config.json"), "w"), ensure_ascii=False, indent=1)
            json.dump(keep, open(os.path.join(d, "keep_result.json"), "w"), ensure_ascii=False, indent=1)
        os.rename(os.path.join(d, "run.log"), os.path.join(d, f"run.log.{n}"))
        if os.path.exists(os.path.join(d, "result.json")):
            os.rename(os.path.join(d, "result.json"), os.path.join(d, f"result.json.{n}"))
        subprocess.Popen(["bash", os.path.join(ROOT_DIR, "benchmark", "run_contrib.sh"), run], cwd=ROOT_DIR,
                         stdout=open(os.path.join(d, "run.log"), "w"), stderr=subprocess.STDOUT, start_new_session=True)
        audit(u["email"], "train_retry", f"{run} ({st.get('problem') or 'алдаа'})")
        return back(msg="Сургалтыг дахин эхлүүллээ. Явцыг доорх 'Сургалтын түүх' хэсгээс харна уу.")

    def _ext_init(con):
        con.execute("CREATE TABLE IF NOT EXISTS ext_bench (id INTEGER PRIMARY KEY, translator TEXT, test TEXT, direction TEXT, "
                    "bleu REAL, chrf REAL, matched INTEGER, n INTEGER, at TEXT, by TEXT, note TEXT)")
        if not con.execute("SELECT 1 FROM ext_bench LIMIT 1").fetchone():   # өмнө хэмжсэн Google (C тест, 2026-11-10)
            con.execute("INSERT INTO ext_bench(translator,test,direction,bleu,chrf,matched,n,at,by,note) VALUES "
                        "('Google Translate','C_unseen_reports.csv','en-mn',30.35,59.27,300,300,'2026-11-10T13:19:06+00:00','','анхны харьцуулалт')")

    @app.get("/simple/admin/extbench/source")
    def ext_source(request: Request, test: str, direction: str = "en-mn"):
        """Гадны орчуулагчид оруулах эх текст: дугаарласан өгүүлбэрүүд (.docx)."""
        need(request, owner=True)
        sets = ext_test_sets()
        if test not in sets or direction not in ("en-mn", "mn-en"):
            raise HTTPException(404, "Олдсонгүй.")
        import pandas as pd
        df = pd.read_csv(sets[test][1], keep_default_na=False)
        src = df.english if direction == "en-mn" else df.mongolian
        data = _docx_bytes([f"{i + 1}. {x}" for i, x in enumerate(src)])
        from fastapi.responses import Response
        fn = f"{test.rsplit('.', 1)[0]}_{'EN' if direction == 'en-mn' else 'MN'}_source.docx"
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        headers={"Content-Disposition": f'attachment; filename="{fn}"'})

    @app.post("/simple/admin/extbench/upload")
    async def ext_upload(request: Request, translator_name: str = Form(...), test: str = Form(...), direction: str = Form("en-mn"),
                         file: UploadFile = File(...)):
        """Гадны орчуулагчийн (Google, Amnesty Translator г.м.) орчуулгыг оноолж, огноотой нь хадгална."""
        u = need(request, owner=True)
        sets = ext_test_sets()
        name = translator_name.strip()[:60]
        if not name or test not in sets or direction not in ("en-mn", "mn-en"):
            return back(err="Орчуулагчийн нэр, шалгалтаа сонгоно уу.", at="extbench")
        ext = os.path.splitext(file.filename or "")[1].lower()
        if ext not in (".docx", ".txt"):
            return back(err="Орчуулсан файлаа .docx эсвэл .txt хэлбэрээр оруулна уу.", at="extbench")
        data = await file.read(20 * 1024 * 1024 + 1)
        if len(data) > 20 * 1024 * 1024:
            return back(err="Файл 20 MB-аас их байна.", at="extbench")
        import tempfile, pandas as pd
        import documents
        from sacrebleu.metrics import BLEU, CHRF
        df = pd.read_csv(sets[test][1], keep_default_na=False)
        ref = list(df.mongolian if direction == "en-mn" else df.english)
        with tempfile.NamedTemporaryFile(suffix=ext) as tf:
            tf.write(data); tf.flush()
            try:
                paras = [p for p in documents.read_document(tf.name, ext)[0] if p.strip()]
            except Exception:  # noqa: BLE001
                return back(err="Файлыг уншиж чадсангүй.", at="extbench")
        num = {}
        for p in paras:
            m = re.match(r"\s*(\d+)\s*[.)]\s*(.*)", p)
            if m:
                num.setdefault(int(m.group(1)), m.group(2).strip())
        # Шалгалтыг АГУУЛГААР нь таних: орчуулгыг шалгалт бүрийн (2 чиглэл) лавлах орчуулгатай chrF-ээр харьцуулж,
        # хамгийн их таарсныг сонгоно. Файлын нэр, сонголт хамаагүй (нэрийг өөрчилсөн, хуудас шинэчлэгдсэн ч зөв).
        picked = None
        if num:
            best = (-1, None, None)
            for k, (_, pth) in sets.items():
                try:
                    tdf = pd.read_csv(pth, keep_default_na=False)
                except Exception:  # noqa: BLE001
                    continue
                for dname, col in (("en-mn", "mongolian"), ("mn-en", "english")):
                    rr = list(tdf[col])
                    hh = [num.get(i + 1, "") for i in range(len(rr))]
                    if sum(bool(h) for h in hh) < 0.5 * len(rr):
                        continue
                    sc = CHRF().corpus_score(hh, [rr]).score
                    if sc > best[0]:
                        best = (sc, k, dname)
            if best[1] and best[0] >= 20:
                picked = best
        if picked and (picked[1] != test or picked[2] != direction):
            test, direction = picked[1], picked[2]
            df = pd.read_csv(sets[test][1], keep_default_na=False)
            ref = list(df.mongolian if direction == "en-mn" else df.english)
        hyp = [num.get(i + 1, "") for i in range(len(ref))]
        matched = sum(bool(h) for h in hyp)
        if matched < 0.8 * len(ref):
            return back(err=f"Өгүүлбэрийн дугаар таарсангүй ({matched}/{len(ref)}). Татаж авсан эх файлыг бүхэлд нь, дугаартай нь орчуулна уу.", at="extbench")
        b, c = BLEU().corpus_score(hyp, [ref]).score, CHRF().corpus_score(hyp, [ref]).score
        with connect() as con:
            _ext_init(con)
            con.execute("INSERT INTO ext_bench(translator,test,direction,bleu,chrf,matched,n,at,by,note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (name, test, direction, round(b, 2), round(c, 2), matched, len(ref), _now(), u["email"], ""))
        audit(u["email"], "ext_bench", f"{name} · {sets[test][0]} · {direction}: BLEU {b:.1f}")
        return back(msg=f"{name}: {sets[test][0]} дээр BLEU {b:.1f} ({matched}/{len(ref)} өгүүлбэр). Харьцуулалтын хүснэгтэд нэмэгдлээ.", at="extbench")

    @app.post("/simple/admin/extbench/delete")
    def ext_delete(request: Request, id: int = Form(...)):
        u = need(request, owner=True)
        with connect() as con:
            _ext_init(con)
            con.execute("DELETE FROM ext_bench WHERE id=?", (id,))
        audit(u["email"], "ext_bench_delete", str(id))
        return back(msg="Харьцуулалтын мөрийг хаслаа.", at="extbench")

    @app.post("/simple/admin/run/decline")
    def run_decline(request: Request, run: str = Form(...), direction: str = Form(...)):
        """Сургалтын шинэ загвар руу шилжихгүй, одоогийнхоо загварыг үлдээх шийдвэр (дараа нь шилжиж болно)."""
        u = need(request)
        d = os.path.join(JOBS, os.path.basename(run))
        if not re.fullmatch(r"contrib_\d{8}_\d{6}", run) or not os.path.isdir(d) or direction not in ("en-mn", "mn-en"):
            return back(err="Олдсонгүй.")
        p = os.path.join(d, "declined.json")
        try:
            dec = json.load(open(p))
        except (OSError, ValueError):
            dec = {}
        dec[direction] = {"at": _now(), "by": u["email"]}
        json.dump(dec, open(p, "w"), ensure_ascii=False)
        audit(u["email"], "deploy_decline", f"{run} {direction}: одоогийн загвараа үлдээсэн")
        return back(msg="Ойлголоо. Сайт одоогийн загвараараа үргэлжлүүлнэ. Хүсвэл дараа нь шилжиж болно.")

    @app.post("/simple/admin/tests/toggle")
    def tests_toggle(request: Request, name: str = Form(...)):
        """Үндсэн шалгалтыг түр унтраах/асаах. Хүний эрхийн шалгалт заавал (загвар солих шийдвэр үүн дээр)."""
        u = need(request, owner=True)
        meta = {n: (label, req) for n, label, req in BUILTIN_TESTS}
        if name not in meta or meta[name][1]:
            return back(err="Энэ шалгалтыг унтрааж болохгүй.", at="tests")
        off = tests_off()
        off ^= {name}
        json.dump(sorted(off), open(TESTS_OFF, "w"))
        audit(u["email"], "test_toggle", f"{meta[name][0]}: {'унтраасан' if name in off else 'асаасан'}")
        return back(msg=f"\"{meta[name][0]}\" шалгалтыг {'унтраалаа. Дараагийн сургалтаас шалгахгүй' if name in off else 'дахин асаалаа'}.", at="tests")

    @app.post("/simple/admin/tests/remove")
    def tests_remove(request: Request, file: str = Form(...), action: str = Form("off")):
        """Админы нэмсэн шалгалт: off = унтраах/асаах (файл хэвээр), delete = бүр мөсөн устгах."""
        u = need(request, owner=True)
        fn = os.path.basename(file)
        if not fn.endswith(".csv"):
            return back(err="Олдсонгүй.", at="tests")
        p = os.path.join(CUSTOM_TESTS, fn)
        cur = p if os.path.exists(p) else p + ".disabled" if os.path.exists(p + ".disabled") else None
        if not cur:
            return back(err="Олдсонгүй.", at="tests")
        try:
            name = json.load(open(p[:-4] + ".json")).get("name", fn)
        except (OSError, ValueError):
            name = fn
        if action == "delete":
            os.remove(cur)
            if os.path.exists(p[:-4] + ".json"):
                os.remove(p[:-4] + ".json")
            audit(u["email"], "test_delete", name)
            return back(msg=f"\"{name}\" шалгалтыг устгалаа.", at="tests")
        if cur == p:
            os.rename(p, p + ".disabled")
            audit(u["email"], "test_toggle", f"{name}: унтраасан")
            return back(msg=f"\"{name}\" шалгалтыг унтраалаа (файл хадгалагдсан).", at="tests")
        os.rename(cur, p)
        audit(u["email"], "test_toggle", f"{name}: асаасан")
        return back(msg=f"\"{name}\" шалгалтыг дахин асаалаа.", at="tests")

    @app.post("/simple/admin/deploy")
    def contrib_deploy(request: Request, direction: str = Form(...), path: str = Form(...)):
        u = need(request)
        full = os.path.realpath(path)
        bench = os.path.join(ROOT_DIR, "benchmark")
        if direction not in ("en-mn", "mn-en") or not full.startswith(bench + os.sep) or not os.path.isdir(full):
            raise HTTPException(400, "Загварын зам буруу.")
        try:
            active = json.load(open(translator.ACTIVE_FILE))
        except (OSError, ValueError):
            active = {}
        prev = translator.model_path(direction)
        is_rollback = full == os.path.realpath(active.get(direction, {}).get("previous", "") or "/nonexistent")
        if u["role"] != "owner":
            # Дата админ: зөвхөн автомат шалгалтаар "муудаагүй" гэж гарсан сургалтын загварыг байршуулж болно
            ok = False
            for r in (os.listdir(JOBS) if os.path.isdir(JOBS) else []):
                res = run_status(r).get("result", {}).get(direction, {})
                if res.get("recommend_deploy") and os.path.realpath(res.get("path", "")) == full:
                    ok = True
            if not ok:
                raise HTTPException(403, "Энэ загварыг зөвхөн үндсэн админ байршуулна (автомат шалгалтад тэнцээгүй эсвэл буцаах үйлдэл).")
        if os.path.realpath(prev) == full:   # аль хэдийн энэ загвар (давхар дарсан) -> түүхийг эвдэхгүй
            return back(msg="Сайт аль хэдийн энэ загвараар ажиллаж байна.")
        hist = active.get(direction, {}).get("history", [])
        active[direction] = {"path": full, "previous": prev, "history": (hist + [prev])[-10:], "at": _now(), "by": u["email"]}
        tmp = translator.ACTIVE_FILE + ".tmp"   # шилжилтийн үед тасарвал хагас бичигдсэн файл үлдэхгүй (атомар)
        json.dump(active, open(tmp, "w"), ensure_ascii=False, indent=1)
        os.replace(tmp, translator.ACTIVE_FILE)
        audit(u["email"], "rollback" if is_rollback else "deploy", f"{direction}: {os.path.relpath(full, bench)}")
        return back(msg=f"{'Англи → Монгол' if direction == 'en-mn' else 'Монгол → Англи'} загвар шинэчлэгдлээ.")
