"""Нэг "contribution"-ий файлуудыг тэгшилж, LaBSE >= 0.85 хосуудыг contrib.db-д бичнэ.
    python webapp/contrib_process.py <contribution id>      (contrib.py үүнийг тусдаа процессоор ажиллуулна)

Оролтын 2 хэлбэр:
  two  - англи файл + монгол файл (тайлан, хууль): өгүүлбэрт хувааж LaBSE зангуу + завсар нөхөх (report_align2)
  one  - хоёр хэлтэй 1 файл: DOCX хүснэгт (EN | MN баганатай, жишээ нь Silicon Shadows) бол мөр бүрийг хос болгоно;
         хүснэгтгүй бол догол мөрүүдийг хэлээр нь ялгаж, хоёр жагсаалтыг тэгшилнэ.
Шүүлтүүр: тасархай хэсэг, хүснэгтийн тоо, тоо таарахгүй, урт харьцаа, LaBSE < 0.85, сургалтын/тестийн датанд байгаа хос.
"""
import html
import json
import os
import re
import sqlite3
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, os.path.join(ROOT, "legal_pipeline")]
import documents  # noqa: E402
import contrib  # noqa: E402

MIN_SIM = contrib.MIN_SIM
TABLE_MIN = 0.65  # хүснэгтийн мөр: зэрэгцүүлэлт нь баримтын бүтцээр баталгаатай, LaBSE зөвхөн илт буруугаа шүүнэ
SPLIT = re.compile(r"(?<=[.!?;])\s+(?=[\"“«(]?[A-ZА-ЯЁӨҮ0-9])")


def cyr(s):
    letters = [c for c in s if c.isalpha()]
    return sum("Ѐ" <= c <= "ӿ" for c in letters) / max(1, len(letters))


def lang(s):
    c = cyr(s)
    return "mn" if c > 0.6 else "en" if c < 0.15 else "mix"


def sentences(paras):
    out = []
    for p in paras:
        for s in SPLIT.split(re.sub(r"\s+", " ", p).strip()):
            s = s.strip(" •▪-–")
            if len(s.split()) >= 3 and len(s) <= 500:
                out.append(s)
    return list(dict.fromkeys(out))


def text_of(path, role="mn"):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".xlsx"):
        rows = sheet_rows(path)
        return [e for e, _ in rows] + [m for _, m in rows], None
    if ext == ".pdf":
        import report_align2  # толгой/хөл, агуулгын жагсаалт, үг тасалгааг цэвэрлэнэ
        bl = report_align2.blocks(path, "EN" if role == "en" else "MN")
        if not bl and documents.scanned_pages(path) and documents.ocr_available():  # скан PDF -> OCR
            return [p for p in documents.read_document(path, ".pdf", ocr=True)[0] if p.strip()], None
        return None, bl
    paras, _ = documents.read_document(path, ext)
    return paras, None


def sheet_rows(path):
    """CSV / XLSX: англи ба монгол баганыг агуулгаар нь олж (толгой мөр, бусад баганыг үл тооно) хос буцаана."""
    if path.endswith(".xlsx"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        grid = [[("" if v is None else str(v)).strip() for v in r] for ws in wb.worksheets for r in ws.iter_rows(values_only=True)]
    else:
        import csv, io
        text = documents.read_text(path)
        try:
            dialect = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        grid = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect)]
    grid = [r for r in grid if any(r)][:200000]
    if not grid:
        return []
    ncol = max(len(r) for r in grid)
    score = [[0, 0] for _ in range(ncol)]   # багана бүрт: англи мөр, монгол мөрийн тоо
    for r in grid[:2000]:
        for i, c in enumerate(r):
            if len(c) >= 3:
                k = lang(c)
                if k == "en": score[i][0] += 1
                elif k == "mn": score[i][1] += 1
    ce = max(range(ncol), key=lambda i: score[i][0]); cm = max(range(ncol), key=lambda i: score[i][1])
    if ce == cm or not score[ce][0] or not score[cm][1]:
        return []
    return [(r[ce], r[cm]) for r in grid if len(r) > max(ce, cm) and lang(r[ce]) == "en" and lang(r[cm]) == "mn"]


def docx_table_rows(path):
    x = documents.zip_read(path, "word/document.xml").decode("utf-8", errors="replace")
    x = re.sub(r"<w:del\b[^>]*>.*?</w:del>", "", x, flags=re.S)
    rows = []
    for r in re.findall(r"<w:tr[ >].*?</w:tr>", x, flags=re.S):
        cells = []
        for c in re.findall(r"<w:tc[ >].*?</w:tc>", r, flags=re.S):
            ps = [html.unescape(re.sub(r"<[^>]+>", "", p)).strip() for p in re.findall(r"<w:p[ >].*?</w:p>", c, flags=re.S)]
            cells.append(" ".join(p for p in ps if p))
        en = [c for c in cells if c and lang(c) == "en" and len(c.split()) >= 3]
        mn = [c for c in cells if c and lang(c) == "mn" and len(c.split()) >= 3]
        if len(en) == 1 and len(mn) == 1:
            rows.append((en[0], mn[0]))
    return rows


MIN_EN_LETTERS, MIN_MN_LETTERS, MIN_WORDS = 10, 6, 2  # "A." / "1.2" гэх мэт жагсаалтын тэмдэг, нэг үгтэй мөр сургалтад хэрэггүй


# Хувийн мэдээлэл (имэйл, утасны дугаар, регистрийн дугаар) агуулсан мөрийг сургалтад оруулахгүй:
# загвар түүнийг цээжилж, бусдад орчуулгаар "гаргаж" өгөх эрсдэлтэй.
PII = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"                                  # имэйл
                 r"|\+\d[\d\s()-]{7,}\d"                                     # олон улсын утас
                 r"|(?<![\d.,])(?:976[\s-]?)?[5-9]\d{3}\s?\d{4}(?![\d.,])"     # Монгол утас 9911 2233
                 r"|(?<![А-ЯӨҮЁа-яөүё])[А-ЯӨҮЁ]{2}\d{8}(?!\d)")                 # регистрийн дугаар УБ12345678


def too_short(e, m):
    letters = lambda x: sum(ch.isalpha() for ch in x)  # noqa: E731
    return letters(e) < MIN_EN_LETTERS or letters(m) < MIN_MN_LETTERS or len(e.split()) < MIN_WORDS


def pdf_table_rows(path):
    """Хүснэгтийг PDF болгож хадгалсан хоёр хэлтэй баримт: нэг хуудсан дээр зэрэгцээ (нэг өндөрт) байрлах
    англи блок + монгол блок = нэг мөр. Word хүснэгтийн бүтэц PDF-д алга болдог тул байрлалаар нь сэргээнэ."""
    import pymupdf
    rows = []
    for page in pymupdf.open(path):
        bl = []
        for b in page.get_text("blocks"):
            if b[6] != 0:
                continue
            t = re.sub(r"\s+", " ", b[4]).strip()
            if len(t.split()) >= 2 and lang(t) in ("en", "mn"):
                bl.append((b[0], b[1], b[2], b[3], t, lang(t)))
        used = set()
        for i, e in enumerate(bl):
            if e[5] != "en":
                continue
            best, bo = None, 0.0
            for j, m in enumerate(bl):
                if m[5] != "mn" or j in used or not (m[0] >= e[2] - 5 or m[2] <= e[0] + 5):
                    continue  # өөр баганад (зүүн/баруун) байх ёстой
                ov = min(e[3], m[3]) - max(e[1], m[1])
                frac = ov / max(1.0, min(e[3] - e[1], m[3] - m[1]))
                if frac > bo:
                    best, bo = j, frac
            if best is not None and bo >= 0.5:
                used.add(best)
                rows.append((e[4], bl[best][4]))
    return rows


def main(cid):
    with contrib.connect() as con:
        c = dict(con.execute("SELECT * FROM contributions WHERE id=?", (cid,)).fetchone())
        con.execute("UPDATE contributions SET status='processing', message='' WHERE id=?", (cid,))
    d = os.path.join(contrib.UP, cid)
    files = {f["role"]: os.path.join(d, f["file"]) for f in json.loads(c["files"])}
    names = {f["role"]: f.get("name") or f["file"] for f in json.loads(c["files"])}
    for role, pth in list(files.items()):  # .doc/.odt/.rtf -> .docx (энэ процесс өөрөө хугацаа/санах ойн хязгаартай)
        if os.path.splitext(pth)[1].lower() in documents.LEGACY:
            try:
                files[role] = documents.convert_legacy(pth)
            except ValueError:
                print("convert failed", pth, flush=True)
    from sentence_transformers import SentenceTransformer
    import numpy as np
    import report_align2 as ra
    model = SentenceTransformer("sentence-transformers/LaBSE", device="cpu")
    # БҮХ ҮСЭГ НЬ ТОМ гарчгийг LaBSE муу үнэлдэг (зөв орчуулга 0.3–0.6 авдаг) тул жижиг үсгээр харьцуулна
    caps = lambda x: x.lower() if sum(ch.isupper() for ch in x) > 0.6 * max(1, sum(ch.isalpha() for ch in x)) else x  # noqa: E731
    enc = lambda xs: model.encode([caps(x) for x in xs], batch_size=32, normalize_embeddings=True) if xs else np.zeros((0, 768))  # noqa: E731
    table_keys = set()  # хүснэгтийн мөрөөр (орчуулагч өөрөө зэрэгцүүлсэн) ирсэн хосууд -> зөөлөн босго
    pairs = []

    def align_lists(es, ms):
        if not es or not ms:
            return []
        got, _ = ra.align_doc(es, ms, enc(es), enc(ms), model)
        return [(e, m, s) for e, m, s, _ in got]

    def file_sents(path, role="mn"):
        paras, sents = text_of(path, role)
        return sents if sents is not None else sentences(paras)

    def do_two(en_path, mn_path):
        a, b = file_sents(en_path, "en"), file_sents(mn_path, "mn")
        es = [x for x in a if lang(x) == "en"]
        ms = [x for x in b if lang(x) == "mn"]
        note = ""
        if (len(es) < 0.3 * max(1, len(a)) or len(ms) < 0.3 * max(1, len(b))) and (a and b):
            # QA 2026-11-13: англи/монгол файлыг сольж оруулбал 0 хос гардаг байсан -> хэлээр нь өөрсдөө ялгана
            es = [x for x in a + b if lang(x) == "en"]; ms = [x for x in a + b if lang(x) == "mn"]
            note = " (файлуудын хэлийг автоматаар тодорхойлсон)"
        empty = [n for n, x in (("англи", a), ("монгол", b)) if not x]
        if empty:
            note += f"; {' ба '.join(empty)} файлаас текст олдсонгүй (скан хийсэн зураг PDF эсвэл хоосон файл байж магадгүй)"
        return align_lists(es, ms), f"англи {len(es)} / монгол {len(ms)} өгүүлбэр{note}"

    def do_one(path):
        out = []
        rows = docx_table_rows(path) if path.endswith(".docx") else pdf_table_rows(path) if path.endswith(".pdf") \
            else sheet_rows(path) if path.endswith((".csv", ".xlsx")) else []
        kind = "хүснэгтийн"
        if len(rows) < 5:  # хүснэгтгүй 2 хэлтэй файл: англи догол мөрийн дараа монгол орчуулга нь ээлжилдэг бол шууд хосолно
            paras = [p.strip() for p in (text_of(path)[0] or []) if p.strip()]
            adj = [(paras[i], paras[i + 1]) for i in range(len(paras) - 1)
                   if lang(paras[i]) == "en" and lang(paras[i + 1]) == "mn"]
            n_min = min(sum(lang(p) == "en" for p in paras), sum(lang(p) == "mn" for p in paras))
            if len(adj) >= 5 and len(adj) >= 0.6 * max(1, n_min):
                rows, kind = adj, "ээлжилсэн догол"
        if len(rows) >= 5:
            e = enc([a for a, _ in rows]); m = enc([b for _, b in rows])
            for (a, b), s in zip(rows, (e * m).sum(1)):
                ea, mb = SPLIT.split(a), SPLIT.split(b)
                if len(ea) == len(mb) > 1:  # догол мөрийг өгүүлбэрээр нь хуваах боломжтой бол
                    ss = (enc(ea) * enc(mb)).sum(1)
                    if ss.min() >= 0.7:
                        out += list(zip(ea, mb, map(float, ss)))
                        continue
                out.append((a, b, float(s)))
            table_keys.update((x, y) for x, y, _ in out)
            return out, f"{kind} {len(rows)} мөр"
        s = file_sents(path)
        es, ms = [x for x in s if lang(x) == "en"], [x for x in s if lang(x) == "mn"]
        return align_lists(es, ms), f"англи {len(es)} / монгол {len(ms)} өгүүлбэр"

    if c["mode"] == "two":
        pairs, note = do_two(files["en"], files["mn"])
    elif c["mode"] == "one":
        pairs, note = do_one(files["both"])
    else:  # "many": олон файл / хавтас / ZIP -> файл бүрийн хэлийг тодорхойлж, агуулгаар нь хос болгоно
        info = []
        for role, path in files.items():
            try:
                s = file_sents(path)
            except Exception:  # noqa: BLE001
                continue
            n_en = sum(lang(x) == "en" for x in s); n_mn = sum(lang(x) == "mn" for x in s)
            tot = max(1, n_en + n_mn)
            kind = "both" if min(n_en, n_mn) / tot >= 0.2 else "en" if n_en > n_mn else "mn"
            if kind == "both" or (path.endswith(".docx") and len(docx_table_rows(path)) >= 5) \
                    or (path.endswith(".pdf") and len(pdf_table_rows(path)) >= 5):
                kind = "both"
            sample = [x for x in s if lang(x) == kind][:300] if kind != "both" else []
            info.append(dict(path=path, name=names.get(role, os.path.basename(path)), kind=kind, emb=enc(sample) if sample else None))
        pairs, notes = [], []
        for f in [f for f in info if f["kind"] == "both"]:
            got, _n = do_one(f["path"]); pairs += got; notes.append(f"«{f.get('name') or os.path.basename(f['path'])}» (2 хэлтэй): {len(got)} хос")
        en = [f for f in info if f["kind"] == "en" and f["emb"] is not None]
        mn = [f for f in info if f["kind"] == "mn" and f["emb"] is not None]
        if en and mn:
            S = np.zeros((len(en), len(mn)))
            for i, a in enumerate(en):
                for j, b in enumerate(mn):
                    x = a["emb"] @ b["emb"].T
                    S[i, j] = max(np.median(x.max(1)), np.median(x.max(0)))
            matched = 0
            for i in range(len(en)):
                j = int(S[i].argmax())
                if S[i, j] >= 0.65 and int(S[:, j].argmax()) == i:
                    got, _n = do_two(en[i]["path"], mn[j]["path"]); pairs += got; matched += 1
            notes.append(f"{matched} хос файл (англи {len(en)}, монгол {len(mn)} файлаас)")
        note = "; ".join(notes) or "хос болох файл олдсонгүй"
    # шүүлтүүр
    low = lambda s: bool(re.match(r"^[\W\d]*[a-zа-яёөү]", s))  # noqa: E731
    end = lambda s: bool(re.search(r"[.!?:;»”\"\)]\s*$", s))  # noqa: E731
    seen = set()
    for f in ("data/train_v9_ot.csv", "data/val.csv", "data/test.csv"):
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            import pandas as pd
            seen |= set(pd.read_csv(p, keep_default_na=False, usecols=["english"]).english)
    # Шалгалтын материалтай давхцах эсэх: бүх шалгалт (үндсэн + админы нэмсэн, унтраасныг ч оруулаад)-ын
    # өгүүлбэрийг жижиг үсэг, цэг таслалгүй хэлбэрт оруулж, англи ЭСВЭЛ монгол тал нь таарвал/агуулагдвал хасна
    import glob
    norm = lambda x: re.sub(r"[\W_]+", "", str(x).lower())  # noqa: E731
    tst_en, tst_mn = set(), set()
    for p in [os.path.join(ROOT, f) for f in ("data/val.csv", "data/test.csv", "data/test_laws.csv", "data/test_vision2050.csv",
                                               "benchmark/new_test/C_unseen_reports.csv")] \
            + glob.glob(os.path.join(ROOT, "benchmark/custom_tests/*.csv*")):
        try:
            import pandas as pd
            t = pd.read_csv(p, keep_default_na=False, usecols=["english", "mongolian"])
        except Exception:  # noqa: BLE001
            continue
        tst_en |= {norm(x) for x in t.english if len(norm(x)) >= 12}
        tst_mn |= {norm(x) for x in t.mongolian if len(norm(x)) >= 12}
    long_en = [x for x in tst_en if len(x) >= 40]; long_mn = [x for x in tst_mn if len(x) >= 40]

    def in_tests(e, m):
        ne, nm = norm(e), norm(m)
        if ne in tst_en or nm in tst_mn:
            return True
        if len(ne) > 60 and any(x in ne for x in long_en):   # урт догол мөр дотор шалгалтын өгүүлбэр орсон бол
            return True
        return len(nm) > 60 and any(x in nm for x in long_mn)
    keep, why = [], {}
    for e, m, s in pairs:
        r = ""
        if in_tests(e, m):
            r = "testset"
        elif PII.search(e) or PII.search(m):
            r = "pii"
        elif too_short(e, m):
            r = "short"
        elif s < (TABLE_MIN if (e, m) in table_keys else contrib.SIM_FLOOR):
            r = "labse"
        elif ra.ok_pair(e, m):
            r = ra.ok_pair(e, m)
        elif (e, m) not in table_keys and ((low(e) or low(m)) or (end(e) != end(m) and max(len(e), len(m)) > 60)):
            r = "fragment"
        elif sum(bool(re.fullmatch(r"[-\d.,%()]+", w)) for w in e.split()) / max(1, len(e.split())) > 0.3:
            r = "table"
        elif e in seen:
            r = "duplicate"
        if r:
            why[r] = why.get(r, 0) + 1
        else:
            keep.append((e, m, s)); seen.add(e)
    with contrib.connect() as con:
        con.execute("DELETE FROM contrib_pairs WHERE contribution_id=?", (cid,))
        con.executemany("INSERT INTO contrib_pairs(contribution_id,english,mongolian,labse_sim,is_table) VALUES (?,?,?,?,?)",
                        [(cid, e, m, s, int((e, m) in table_keys)) for e, m, s in keep])
        thr = con.execute("SELECT min_sim FROM contributions WHERE id=?", (cid,)).fetchone()[0]
        inc = [x for x in keep if x[2] >= (thr if thr >= 0 else MIN_SIM) - 1e-9]
        low_n = len(keep) - len(inc)
        msg = f"{note}; тэгшилсэн {len(pairs)}, үлдсэн {len(inc)}" + (
            f"; таарц багатай {low_n} хос нөөцөд (босгыг доошлуулбал нэмэгдэнэ)" if low_n else "") + (
            "; хассан: " + ", ".join(f"{k} {v}" for k, v in sorted(why.items())) if why else "")
        con.execute("UPDATE contributions SET status=?, n_pairs=?, mean_sim=?, message=? WHERE id=?",
                    ("ready" if keep else "failed", len(inc),
                     round(sum(s for *_, s in inc) / len(inc), 3) if inc else 0, msg, cid))
    print("done", cid, msg, flush=True)
    try:   # шинэ илгээлтийг сэдвээр ангилах (арын горимд)
        contrib.start_topics_job()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    if sys.argv[1] == "--killed":  # хугацаа/санах ойн хязгаараас хэтэрч зогссон (хор хөнөөлтэй эсвэл хэт том файл)
        with contrib.connect() as con:
            con.execute("UPDATE contributions SET status='failed', message=? WHERE id=? AND status IN ('queued','processing')",
                        ("Алдаа: боловсруулалт хэт удаан эсвэл хэт их санах ой шаардсан тул зогсоосон (файл эвдэрсэн эсвэл хор хөнөөлтэй байж болзошгүй).", sys.argv[2]))
        sys.exit(0)
    try:
        main(sys.argv[1])
    except Exception as ex:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        msg = ("Алдаа: PDF файл нууц үгээр хамгаалагдсан тул уншиж чадсангүй." if "encrypted" in str(ex) or "password" in str(ex)
               else f"Алдаа: {type(ex).__name__}: {str(ex)[:200]}")
        with contrib.connect() as con:
            con.execute("UPDATE contributions SET status='failed', message=? WHERE id=?", (msg, sys.argv[1]))
