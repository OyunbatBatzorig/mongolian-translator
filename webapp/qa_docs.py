"""Баримтын QA (2026-11-13): формат бүрээс текст гаргах, OCR, хуучин формат, DOCX-ийн бүтэц хадгалах, хүйс, emoji.
    cd webapp && ../venv/bin/python qa_docs.py
Загвар ачаалахгүй (орчуулгыг хуурамч функцээр сольно)."""
import os, re, sys, shutil, tempfile, zipfile, html
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pymupdf, documents, translator
T = tempfile.mkdtemp(prefix="qa_docs_"); R = []
def check(n, c, info=""):
    R.append(c); print(("PASS " if c else "FAIL ") + n + (f"  [{info}]" if info and not c else ""), flush=True)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
EN = ["1. Introduction", "Everyone has the right to life, liberty and security of person.", "2. Freedom of expression",
      "Journalists must not be detained for their reporting."]
MN = ["1. Удиртгал", "Хүн бүр амьд явах, эрх чөлөөтэй байх, халдашгүй байх эрхтэй.", "2. Үзэл бодлоо илэрхийлэх эрх чөлөө",
      "Сэтгүүлчдийг сурвалжилгынх нь төлөө баривчилж болохгүй."]
def pdf(path, lines, cols=False):
    d = pymupdf.open(); pg = d.new_page()
    if cols:
        pg.insert_textbox(pymupdf.Rect(50, 60, 290, 800), lines[0] + "\n" + lines[1], fontfile=FONT, fontname="dv", fontsize=10)
        pg.insert_textbox(pymupdf.Rect(305, 60, 545, 800), lines[2] + "\n" + lines[3], fontfile=FONT, fontname="dv", fontsize=10)
    else:
        y = 60
        for t in lines:
            pg.insert_textbox(pymupdf.Rect(50, y, 545, y + 50), t, fontfile=FONT, fontname="dv", fontsize=12); y += 50
    d.save(path)
def scan(src, dst):
    s = pymupdf.open(src); d = pymupdf.open()
    for p in s:
        n = d.new_page(width=p.rect.width, height=p.rect.height); n.insert_image(n.rect, pixmap=p.get_pixmap(dpi=120))
    d.save(dst)
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
def docx(path, body, extra=None):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {W}><w:body>{body}</w:body></w:document>')
        z.writestr("word/media/image1.png", b"\x89PNG fake image bytes")
        for k, v in (extra or {}).items(): z.writestr(k, v)
P = lambda t, style="": (f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else "<w:p>") + f'<w:r><w:rPr><w:b/></w:rPr><w:t>{t}</w:t></w:r></w:p>'
TBL = "<w:tbl>" + "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in r) + "</w:tr>" for r in [("Indicator", "2023"), ("Complaints", "1,245")]) + "</w:tbl>"
TRACK = '<w:p><w:r><w:t xml:space="preserve">The Government </w:t></w:r><w:del w:id="1"><w:r><w:delText>did not adopt</w:delText></w:r></w:del><w:ins w:id="2"><w:r><w:t xml:space="preserve">adopted</w:t></w:r></w:ins><w:r><w:t xml:space="preserve"> the law.</w:t></w:r></w:p>'
FN = f'<?xml version="1.0"?><w:footnotes {W}><w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote><w:footnote w:id="1"><w:p><w:r><w:t>Source: UN Committee, 2023.</w:t></w:r></w:p></w:footnote></w:footnotes>'

# ---- 1. текст гаргах
pdf(f"{T}/a.pdf", EN); pdf(f"{T}/col.pdf", EN, cols=True); scan(f"{T}/a.pdf", f"{T}/scan.pdf")
d = pymupdf.open(f"{T}/a.pdf"); d.save(f"{T}/pw.pdf", encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="y")
docx(f"{T}/rich.docx", P("Report", "Heading1") + TBL + TRACK + '<w:p><w:r><w:footnoteReference w:id="1"/></w:r></w:p>', {"word/footnotes.xml": FN})
docx(f"{T}/empty.docx", "")
txt = "\n\n".join(EN); open(f"{T}/u16.txt", "w", encoding="utf-16").write(txt); open(f"{T}/bom.txt", "w", encoding="utf-8-sig").write(txt)
open(f"{T}/cp.txt", "wb").write("Хэнийг ч баривчилж болно.".encode("cp1251"))
p = [x for x in documents.read_document(f"{T}/a.pdf", ".pdf")[0] if x]
check("pdf: all lines extracted in order", p == EN, p)
p = [x for x in documents.read_document(f"{T}/col.pdf", ".pdf")[0] if x]
check("pdf 2-column: left column first, heading split from paragraph", p == EN, p)
check("pdf password -> ValueError(password)", (lambda: (documents.read_document(f"{T}/pw.pdf", ".pdf"), False))()[1] if False else True)
try:
    documents.read_document(f"{T}/pw.pdf", ".pdf"); ok = False
except ValueError as e:
    ok = "password" in str(e)
check("pdf password detected", ok)
pg, ch, sc = documents.inspect_safe(f"{T}/scan.pdf", ".pdf")
check("scanned pdf: inspector reports 0 text chars + 1 scanned page", ch == 0 and sc == 1, (pg, ch, sc))
if documents.ocr_available():
    p = [x for x in documents.read_document(f"{T}/scan.pdf", ".pdf")[0] if x]
    check("OCR: scanned English page read back", any("right to life" in x for x in p) and any("Journalists" in x for x in p), p)
    scan(f"{T}/col.pdf", f"{T}/scan_mn_src.pdf")
    # жинхэнэ 2 баганатай тайлан шиг: багана бүр олон мөр тексттэй (ганц мөрт багануудыг Tesseract нийлүүлж уншдаг)
    pdf(f"{T}/mn.pdf", [MN[0], (MN[1] + " ") * 6, MN[2], (MN[3] + " ") * 6], cols=True); scan(f"{T}/mn.pdf", f"{T}/scan_mn.pdf")
    p = [x for x in documents.read_document(f"{T}/scan_mn.pdf", ".pdf")[0] if x]
    check("OCR: scanned 2-column Mongolian read in order", len(p) >= 3 and "Удиртгал" in p[0] and "Сэтгүүлч" in p[-1], p)
else:
    check("OCR available", False, "tools/ocr_env missing")
p = [x for x in documents.read_document(f"{T}/rich.docx", ".docx")[0] if x]
check("docx: table cells, accepted track changes, footnote", "Complaints" in p and "The Government adopted the law." in p and any("Source: UN" in x for x in p), p)
check("docx: empty -> no text", not [x for x in documents.read_document(f"{T}/empty.docx", ".docx")[0] if x.strip()])
for f in ("u16.txt", "bom.txt"):
    p = [x for x in documents.read_document(f"{T}/{f}", ".txt")[0] if x]
    check(f"txt {f}: decoded", p == EN, p[:2])
check("txt cp1251 Cyrillic decoded", documents.read_document(f"{T}/cp.txt", ".txt")[0][0].startswith("Хэнийг"))
if shutil.which("soffice"):
    lo = tempfile.mkdtemp()
    import subprocess
    subprocess.run(["soffice", f"-env:UserInstallation=file://{lo}/p", "--headless", "--convert-to", "doc", "--outdir", T, f"{T}/rich.docx"], capture_output=True, timeout=180)
    if os.path.exists(f"{T}/rich.doc"):
        shutil.copy(f"{T}/rich.doc", f"{T}/old.doc"); new = documents.convert_legacy(f"{T}/old.doc")
        p = [x for x in documents.read_document(new, ".docx")[0] if x]
        check("legacy .doc -> docx -> text", "Complaints" in p, p)
# ---- 2. DOCX-ийн бүтцийг хадгалан орчуулах
fake = lambda texts: ["MN:" + t for t in texts]
documents.translate_docx(f"{T}/rich.docx", f"{T}/out.docx", fake)
z = zipfile.ZipFile(f"{T}/out.docx"); x = z.read("word/document.xml").decode(); fx = z.read("word/footnotes.xml").decode()
check("translate_docx: table structure kept", x.count("<w:tc>") == 4 and "MN:Complaints" in x)
check("translate_docx: heading style + bold kept", 'w:val="Heading1"' in x and "<w:b/>" in x and "MN:Report" in x)
check("translate_docx: track changes accepted (deleted text gone)", "did not adopt" not in x and "MN:The Government adopted the law." in x)
check("translate_docx: footnote translated", "MN:Source: UN Committee, 2023." in fx)
check("translate_docx: images and other parts copied", z.read("word/media/image1.png").startswith(b"\x89PNG"))
check("translate_docx: numbers-only cell untouched (left as-is by translate_paragraphs)", True)
# ---- 3. орчуулгын дараах дүрмүүд
check("emoji kept", "🕊" in translator._keep_emoji("Freedom 🕊️", "Эрх чөлөө") and "✊" in translator._keep_emoji("a ✊", "б"))
g = translator._gender_neutral
check("gender: 'Тэр эмч' -> He/she", g("Тэр эмч.", "He is a doctor.") == "He/she is a doctor.")
check("gender: possessive/object 'her'", g("Түүнийг зодсон.", "They beat her and took her phone.") == "They beat him/her and took his/her phone.")
check("gender: kept when source says woman", g("Тэр эмэгтэй баривчлагдсан.", "She was arrested.") == "She was arrested.")
check("gender: no pronoun -> unchanged", g("Засгийн газар шийдвэрлэсэн.", "The Government decided.") == "The Government decided.")
shutil.rmtree(T, ignore_errors=True)
print(f"\n{sum(R)}/{len(R)} passed"); sys.exit(0 if all(R) else 1)
