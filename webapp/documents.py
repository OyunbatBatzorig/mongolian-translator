"""Файлаас текст гаргах (PDF / DOCX / TXT), хуудас тоолох, орчуулгыг DOCX болгох.

DOCX нь үнэндээ XML файлууд агуулсан ZIP архив тул нэмэлт сан суулгалгүйгээр
Python-ийн zipfile-аар уншиж, бичиж болно.
"""
import html
import os
import re
import zipfile
from xml.sax.saxutils import escape

import pymupdf

from config import CHARS_PER_PAGE

ALLOWED = {".pdf", ".docx", ".txt", ".doc", ".odt", ".rtf"}
LEGACY = {".doc", ".odt", ".rtf"}   # LibreOffice-оор DOCX болгож хөрвүүлнэ (хамгаалагдсан процесст)


def read_text(path):
    """TXT-ийн кодчилолыг таних: BOM (UTF-8/UTF-16), UTF-8, эсвэл хуучин Windows кирилл (cp1251)."""
    raw = open(path, "rb").read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1251")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


OCR_BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "ocr_env", "bin", "tesseract")
OCR_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "ocr_env", "tessdata")
OCR_MIN_CHARS = 20      # хуудсанд үүнээс цөөн текст байвал скан гэж үзнэ


def ocr_available():
    return os.path.exists(OCR_BIN) and os.path.exists(os.path.join(OCR_DATA, "mon.traineddata"))


def ocr_page(page, dpi=300, timeout=90):
    """Скан хуудсыг Tesseract (англи+монгол) -оор уншиж догол мөрүүдийг буцаана. Тусдаа процесс, хугацаа/санах ойн хязгаартай."""
    import subprocess, tempfile
    with tempfile.TemporaryDirectory(prefix="ocr_") as t:
        img = os.path.join(t, "p.png")
        page.get_pixmap(dpi=dpi).save(img)
        r = subprocess.run(["prlimit", "--as=2000000000", "--", OCR_BIN, img, "-", "-l", "eng+mon", "--tessdata-dir", OCR_DATA, "--psm", "3"],
                           capture_output=True, text=True, timeout=timeout, env={"OMP_THREAD_LIMIT": "2", "PATH": os.environ.get("PATH", "")})
    paras = []
    for block in re.split(r"\n\s*\n", r.stdout):
        lines = [x.strip() for x in block.split("\n") if x.strip()]
        if not lines:
            continue
        if len(lines) >= 2 and len(lines[0].split()) <= 10 and not re.search(r"[.!?:;,]$", lines[0]) and lines[1][:1].isupper():
            paras.append(lines[0]); lines = lines[1:]   # гарчиг + догол мөр нэг блокт
        t = ""
        for ln in lines:  # мөрийн төгсгөлийн үг тасалгааг нийлүүлнэ
            t = t[:-1] + ln if t.endswith("-") and ln[:1].islower() else (t + " " + ln).strip()
        t = re.sub(r"^З\.(?=\s?[А-ЯЁӨҮA-Z])", "3.", t)          # OCR: "3." -> "З." (кирилл З) андуурал
        t = re.sub(r"^(\d{1,2}(?:\.\d{1,2})*)\.(?=[^\s\d.])", r"\1. ", t)  # "1.Удиртгал" -> "1. Удиртгал"
        if paras and not re.search(r"[.!?:;»”\"\)]$", paras[-1]) and t[:1].islower():
            paras[-1] += " " + t      # OCR хоосон мөрөөр тасалсан өгүүлбэрийг нийлүүлнэ
        else:
            paras.append(t)
    return paras


def convert_legacy(path, timeout=120):
    """.doc/.odt/.rtf -> .docx (LibreOffice, тусдаа профайл, хугацаа ба санах ойн хязгаартай). Шинэ замыг буцаана."""
    import shutil, subprocess, tempfile
    work = tempfile.mkdtemp(prefix="lo_")
    try:
        r = subprocess.run(["prlimit", "--as=3000000000", "--", "soffice", f"-env:UserInstallation=file://{work}/profile", "--headless",
                            "--norestore", "--convert-to", "docx", "--outdir", work, os.path.abspath(path)],
                           capture_output=True, timeout=timeout, env={"HOME": work, "PATH": os.environ.get("PATH", "")})
        out = os.path.join(work, os.path.splitext(os.path.basename(path))[0] + ".docx")
        if r.returncode != 0 or not os.path.exists(out):
            raise ValueError("convert")
        dst = os.path.splitext(path)[0] + ".docx"
        shutil.move(out, dst)
        return dst
    except subprocess.TimeoutExpired:
        raise ValueError("convert timeout")
    finally:
        shutil.rmtree(work, ignore_errors=True)


MAX_XML_BYTES = 80 * 1024 * 1024   # DOCX доторх XML-ийн дээд хэмжээ: "zip bomb" (жижиг файл задлахад хэдэн GB) хамгаалалт


def zip_read(path, member, limit=MAX_XML_BYTES):
    """ZIP (DOCX) доторх файлыг хэмжээг нь хязгаарлаж уншина. Толгой дээрх хэмжээг ч, бодит задарсан хэмжээг ч шалгана."""
    with zipfile.ZipFile(path) as z:
        zi = z.getinfo(member)
        if zi.file_size > limit:
            raise ValueError("DOCX доторх текст хэт том байна.")
        with z.open(zi) as f:
            data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError("DOCX доторх текст хэт том байна.")
    return data


def _docx_paragraphs(path):
    x = zip_read(path, "word/document.xml").decode("utf-8", errors="replace")
    x = re.sub(r"<mc:Fallback>.*?</mc:Fallback>", "", x, flags=re.S)      # давхар хадгалсан текст хайрцаг
    # Word-ын "Track changes": устгасан (w:del, w:moveFrom) хэсгийг хасаж, нэмсэн (w:ins)-ийг үлдээнэ
    # = бүх өөрчлөлтийг "Accept all" хийсэнтэй адил. Өмнө нь хоёуланг нь орчуулж байсан.
    x = re.sub(r"<w:del\b[^>]*>.*?</w:del>|<w:moveFrom\b[^>]*>.*?</w:moveFrom>", "", x, flags=re.S)
    x = re.sub(r"<wp:posOffset>.*?</wp:posOffset>", "", x, flags=re.S)     # зургийн байрлалын тоо
    x = re.sub(r"<w:instrText[^>]*>.*?</w:instrText>", "", x, flags=re.S)  # Word-ын далд код (PAGEREF...)
    x = re.sub(r"<w:tab/>|<w:br/>", " ", x)
    paras = re.findall(r"<w:p[ >].*?</w:p>", x, flags=re.S)
    out = [html.unescape(re.sub(r"<[^>]+>", "", p)).strip() for p in paras]
    # Зүүлт (footnote) ба төгсгөлийн тайлбар (endnote): өмнө нь огт орчуулагддаггүй байсан -> баримтын төгсгөлд нэмнэ
    for part in ("word/footnotes.xml", "word/endnotes.xml"):
        try:
            fx = zip_read(path, part).decode("utf-8", errors="replace")
        except (KeyError, ValueError):
            continue
        notes = re.findall(r"<w:(?:footnote|endnote)\b(?![^>]*w:type=)[^>]*>(.*?)</w:(?:footnote|endnote)>", fx, flags=re.S)
        texts = [html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<w:tab/>|<w:br/>", " ", n))).strip() for n in notes]
        texts = [t for t in texts if t]
        if texts:
            out += [""] + [f"[{i}] {t}" for i, t in enumerate(texts, 1)]
    return out


def _docx_pages(path, paragraphs):
    try:
        app = zip_read(path, "docProps/app.xml", 1024 * 1024).decode("utf-8", errors="replace")
        m = re.search(r"<Pages>(\d+)</Pages>", app)
        if m:
            return int(m.group(1))
    except (KeyError, ValueError):
        pass
    return max(1, -(-sum(len(p) for p in paragraphs) // CHARS_PER_PAGE))


def _pdf_paragraphs(path, ocr=True):
    doc = pymupdf.open(path)
    pages = []
    for page in doc:
        if ocr and len(page.get_text("text").strip()) < OCR_MIN_CHARS and page.get_images() and ocr_available():
            try:  # текстгүй, зурагтай хуудас = скан -> OCR
                pages.append(ocr_page(page))
                continue
            except Exception:  # noqa: BLE001
                pass
        # "blocks" нь PDF дээрх текстийн хэсгүүд (ойролцоогоор догол мөр)
        blocks = []
        for b in page.get_text("blocks"):
            lines = [x.strip() for x in b[4].split("\n") if x.strip()]
            # Гарчиг ба догол мөр нэг блокт наалдсан бол ("1. Introduction\nEveryone has...") гарчгийг салгана
            head = []
            if len(lines) >= 2 and len(lines[0].split()) <= 10 and not re.search(r"[.!?:;,]$", lines[0]) \
                    and lines[1][:1].isupper() and len(" ".join(lines[1:])) > 2 * len(lines[0]):
                head, lines = [lines[0]], lines[1:]
            for t in head + [re.sub(r"\s+", " ", " ".join(lines)).strip()]:
                if t and not re.fullmatch(r"\d{1,4}(\s*/\s*\d{1,4})?", t):  # хуудасны дугаарыг алгасна
                    blocks.append(t)
        pages.append(blocks)
    # Хуудас бүрийн дээр/доор давтагддаг гарчиг ("Amnesty International 6", тайлангийн нэр)-ыг хасна.
    # Тоог нь арилгаад харьцуулна, 3+ хуудсанд давтагдсан богино мөр бол running header гэж үзнэ.
    key = lambda t: re.sub(r"\d+", "#", t.lower())  # noqa: E731
    from collections import Counter
    seen = Counter(k for blocks in pages for k in {key(t) for t in blocks if len(t) < 200})
    limit = max(3, len(pages) // 4)
    return [t for blocks in pages for t in blocks if not (len(t) < 200 and seen[key(t)] >= limit)]


def scanned_pages(path):
    """PDF-ийн текстгүй, зурагтай (скан) хуудасны тоо."""
    doc = pymupdf.open(path)
    return sum(1 for p in doc if len(p.get_text("text").strip()) < OCR_MIN_CHARS and p.get_images())


def read_document(path, ext, ocr=True):
    """(догол мөрүүд, хуудасны тоо) буцаана. ocr=True: скан хуудсыг OCR-оор уншина."""
    if ext == ".pdf":
        doc = pymupdf.open(path)
        if doc.needs_pass:
            raise ValueError("password")
        return _pdf_paragraphs(path, ocr), len(doc)
    if ext == ".docx":
        paras = _docx_paragraphs(path)
        return paras, _docx_pages(path, paras)
    text = read_text(path)
    paras = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return paras, max(1, -(-len(text) // CHARS_PER_PAGE))


def write_docx(paragraphs, path, title=""):
    """Хамгийн энгийн хэлбэрийн, Word-оор нээгддэг DOCX файл үүсгэнэ."""
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{escape(p)}</w:t></w:r></w:p>' for p in paragraphs)
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>'),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>'),
        "word/document.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body>{body}</w:body></w:document>'),
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)


def split_document(path, ext, pages, per_part):
    """Урт файлыг per_part хуудастай хэсгүүдэд хуваана. [(хэсгийн зам, өргөтгөл, хуудас, (эхний, сүүлийн хуудас))].
    PDF: жинхэнэ хуудсаар нь хуваана. DOCX/TXT: хуудасны тоо ойролцоо тул текстийн уртаар
    тэнцүү хувааж, хэсэг бүрийг .txt болгож хадгална (орчуулгын үр дүн ямар ч байсан DOCX болно)."""
    base = os.path.splitext(path)[0]
    parts = []
    if ext == ".pdf":
        src = pymupdf.open(path)
        for i, start in enumerate(range(0, pages, per_part)):
            end = min(start + per_part, pages) - 1
            out = pymupdf.open()
            out.insert_pdf(src, from_page=start, to_page=end)
            p = f"{base}_part{i + 1}.pdf"
            out.save(p)
            parts.append((p, ".pdf", end - start + 1, (start + 1, end + 1)))
        return parts
    paras, _ = read_document(path, ext)
    total = max(1, sum(len(x) + 1 for x in paras))
    target = total * per_part / pages          # нэг хэсэгт ногдох тэмдэгт
    groups, cur, size = [], [], 0
    for x in paras:
        if cur and size + len(x) > target:
            groups.append(cur); cur, size = [], 0
        cur.append(x); size += len(x) + 1
    if cur:
        groups.append(cur)
    first = 1
    for i, g in enumerate(groups):
        n = max(1, min(per_part, round(pages * sum(len(x) + 1 for x in g) / total)))
        p = f"{base}_part{i + 1}.txt"
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(g))
        last = pages if i == len(groups) - 1 else min(pages, first + n - 1)
        parts.append((p, ".txt", n, (first, last)))
        first = last + 1
    return parts


# ---------------------------------------------------------------- хор хөнөөлтэй файлаас хамгаалах
MAX_FILE_CHARS = 3_000_000   # нэг файлд ~1200 хуудас текст; түүнээс их бол орчуулгын дарааллыг дүүргэхгүйн тулд татгалзана
INSPECT_TIMEOUT = 90          # сек
INSPECT_MAX_MEM = 4 * 1024 ** 3


def inspect_safe(path, ext):
    """Файлыг тусдаа процесст (хугацаа, санах ойн хязгаартай) уншиж (хуудас, тэмдэгт) буцаана.
    Эвдэрсэн, "bomb", хэт нарийн бүтэцтэй файл серверийг гацаахгүй. Амжилтгүй бол ValueError."""
    import json
    import subprocess
    import sys
    try:
        out = subprocess.run(["prlimit", f"--as={INSPECT_MAX_MEM}", "--", sys.executable, os.path.abspath(__file__),
                              "--inspect", os.path.abspath(path), ext], capture_output=True, text=True, timeout=INSPECT_TIMEOUT,
                             cwd=os.path.dirname(os.path.abspath(__file__)), env={**os.environ, "CUDA_VISIBLE_DEVICES": ""})
    except subprocess.TimeoutExpired:
        raise ValueError("timeout")
    if out.returncode != 0:
        raise ValueError("password" if "password" in out.stderr[-500:] else "read")
    r = json.loads(out.stdout.strip().splitlines()[-1])
    return r["pages"], r["chars"], r.get("scanned", 0)


if __name__ == "__main__" and len(__import__("sys").argv) == 4 and __import__("sys").argv[1] == "--inspect":
    import json
    import sys
    paras, pages = read_document(sys.argv[2], sys.argv[3], ocr=False)   # шалгалтад OCR хийхгүй (удаан)
    sc = scanned_pages(sys.argv[2]) if sys.argv[3] == ".pdf" else 0
    print(json.dumps({"pages": pages, "chars": sum(len(p) for p in paras), "scanned": sc}))


# ---------------------------------------------------------------- DOCX-ийг хэлбэржилтийг нь хадгалан орчуулах
# Эх файлын бүтэц (хүснэгт, гарчгийн загвар, тод бичиг, зураг, толгой/хөл, зүүлт) хэвээр үлдэнэ: догол мөр бүрийн
# текстийг орчуулгаар солино. Догол мөр дотор хэд хэдэн хэлбэржилт байвал эхний хэсгийнх нь хэлбэрийг авна.
DOCX_TEXT_PARTS = re.compile(r"^word/(document|footnotes|endnotes|header\d*|footer\d*)\.xml$")
_P_INNER = re.compile(r"<w:p\b[^>]*?(?<!/)>(?:(?!<w:p\b).)*?</w:p>", re.S)
_T = re.compile(r"(<w:t\b[^>]*>)(.*?)(</w:t>)", re.S)
MAX_DOCX_TOTAL = 400 * 1024 * 1024


def _accept_changes(p):
    p = re.sub(r"<w:del\b[^>]*>.*?</w:del>|<w:moveFrom\b[^>]*>.*?</w:moveFrom>", "", p, flags=re.S)
    return re.sub(r"</?w:(?:ins|moveTo)\b[^>]*>", "", p)


def _p_text(p):
    p = re.sub(r"<w:tab/>|<w:br/>", "<w:t> </w:t>", p)
    return html.unescape("".join(m.group(2) for m in _T.finditer(p))).strip()


def translate_docx(src, dst, translate):
    """translate(list[str]) -> list[str]. DOCX-ийн бүтцийг хадгалан текстийг орчуулж dst-д бичнэ."""
    zin = zipfile.ZipFile(src)
    parts = {i.filename: zip_read(src, i.filename).decode("utf-8", errors="replace")
             for i in zin.infolist() if DOCX_TEXT_PARTS.match(i.filename)}
    texts = []
    for name, x in parts.items():
        x = re.sub(r"<mc:Fallback>.*?</mc:Fallback>", "", x, flags=re.S)  # текст хайрцгийн давхар хуулбар
        parts[name] = x
        for m in _P_INNER.finditer(x):
            t = _p_text(_accept_changes(m.group(0)))
            if t:
                texts.append(t)
    uniq = list(dict.fromkeys(texts))
    tr = dict(zip(uniq, translate(uniq))) if uniq else {}

    def repl(m):
        p = _accept_changes(m.group(0))
        t = _p_text(p)
        if not t or t not in tr:
            return p
        first = [True]

        def put(mt):
            if first[0]:
                first[0] = False
                return '<w:t xml:space="preserve">' + escape(tr[t]) + "</w:t>"
            return mt.group(1) + mt.group(3)
        p = re.sub(r"<w:tab/>|<w:br/>", "", p)   # таб/мөр шилжилт орчуулгад нийлсэн
        return _T.sub(put, p)
    out = {n: _P_INNER.sub(repl, x) for n, x in parts.items()}
    total = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            if info.filename in out:
                zout.writestr(info.filename, out[info.filename].encode("utf-8"))
                continue
            with zin.open(info) as fi, zout.open(info.filename, "w") as fo:   # зураг г.м. хэвээр (хэмжээ хязгаартай)
                while True:
                    b = fi.read(1 << 20)
                    if not b:
                        break
                    total += len(b)
                    if total > MAX_DOCX_TOTAL:
                        raise ValueError("DOCX хэт том")
                    fo.write(b)
    return len(uniq)
