"""Загварыг ачаалж, текст орчуулах хэсэг.

Загвар нэг удаадаа ~128 токен (1-2 өгүүлбэр) л боловсруулдаг тул урт текстийг
догол мөр -> өгүүлбэр болгон хувааж, багцаар (batch) орчуулаад буцааж нийлүүлнэ.
"""
import os
import re
import threading

from config import LANG_CODES, MAX_TOKENS, MODELS, NUM_BEAMS

_models = {}
_lock = threading.Lock()  # GPU-г нэг зэрэг хоёр хүсэлт ашиглахаас сэргийлнэ
_load_lock = threading.Lock()  # серверийн эхэнд ачаалж байх үед ирсэн хүсэлт давхар ачаалахгүй

SENT_SPLIT = re.compile(r"(?<=[.!?;])\s+(?=[\"“«(]?[A-ZА-ЯЁӨҮ0-9])")


# Админ самбараас загвар шинэчлэхэд энэ файлд шинэ зам бичигдэнэ (8000 болон 8080 сервер хоёулаа уншина).
ACTIVE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "benchmark", "active_models.json")


def model_path(direction):
    try:
        import json
        p = json.load(open(ACTIVE_FILE)).get(direction, {}).get("path")
        if p and os.path.isdir(p):
            return p
    except (OSError, ValueError):
        pass
    return MODELS[direction]


def available_directions():
    return {d: os.path.isdir(model_path(d)) for d in MODELS}


def _load(direction):
    path = model_path(direction)
    cur = _models.get(direction)
    if cur and cur[3] == path:
        return cur[:3]
    with _load_lock:
        cur = _models.get(direction)
        if not cur or cur[3] != path:
            old = _models.pop(direction, None)
            del old
            _models[direction] = (*_really_load(direction, path), path)
    return _models[direction][:3]


def _really_load(direction, path=None):
    import torch
    from transformers import AutoModelForSeq2SeqLM
    from transformers.models.nllb.tokenization_nllb import NllbTokenizer

    src, tgt = direction.split("-")
    path = path or MODELS[direction]
    tok = NllbTokenizer.from_pretrained(path, src_lang=LANG_CODES[src], tgt_lang=LANG_CODES[tgt])
    model = AutoModelForSeq2SeqLM.from_pretrained(path)
    device = os.environ.get("TRANSLATOR_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")  # GPU сургалтын үеэр cpu
    if device == "cuda":
        model = model.half()  # GPU санах ойг хоёр дахин бага эзэлнэ, хурдан
    model.to(device).eval()
    model.generation_config.forced_bos_token_id = tok.convert_tokens_to_ids(LANG_CODES[tgt])
    return tok, model, device


# 2026-11-10: монгол орчуулга англиас ~1.3 дахин олон токен болдог тул 128 токен оролттой урт өгүүлбэрийн
# орчуулга 128 дээр тасарч байсан ("...Динуши"). Одоо 96 токеноос урт бол хувааж, гаралтыг 256 хүртэл зөвшөөрнө.
PIECE_TOKENS = 96

# Хашилт: загвар нь шулуун " хашилтыг сурсан, “ ” ‘ ’ « »-г ихэвчлэн гээдэг (үгтэй нь хамт).
# Тиймээс орчуулахын өмнө шулуун болгож, өгүүлбэрийн эхэнд ганцаараа байгаа хашилтыг (олон догол мөртэй
# ишлэл) салгаж аваад орчуулсны дараа буцааж залгана. Эх текст “ ” хэлбэртэй байсан бол гаралтыг ч мөн.
CURLY_D = re.compile(r"[“”„«»]")
SINGLE_Q = re.compile(r"(?<![\w])‘|’(?![\w])")


def _norm_quotes(s):
    s = CURLY_D.sub('"', s)
    s = SINGLE_Q.sub('"', s)
    return s.replace("’", "'")


def _restore_quotes(s, curly):
    if not curly:
        return s
    out, opening = [], True
    for ch in s:
        if ch == '"':
            out.append("“" if opening else "”"); opening = not opening
        else:
            out.append(ch)
    return "".join(out)


def _pieces(sentence, tok):
    """Хэт урт өгүүлбэрийг таслал, цэгтэй таслалаар нь хуваана."""
    if len(tok(sentence)["input_ids"]) <= PIECE_TOKENS:
        return [sentence]
    parts, cur = [], ""
    for chunk in re.split(r"(?<=[,;:])\s+", sentence):
        cand = (cur + " " + chunk).strip()
        if cur and len(tok(cand)["input_ids"]) > PIECE_TOKENS:
            parts.append(cur)
            cur = chunk
        else:
            cur = cand
    return parts + ([cur] if cur else [])


LIST_MARK = re.compile(r"^\s*(?:\(?\d{1,3}(?:\.\d{1,3})*[.)]|\(?[a-zа-я][.)]|[IVX]{1,4}[.)]|[-•*▪–])\s+")
CAPS_WORD = re.compile(r"\b[A-ZА-ЯЁӨҮ]{2,}\b")


def _is_caps(s):
    letters = [c for c in s if c.isalpha()]
    return len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) > 0.8


def _decap(s, acronyms):
    """'THE CENTRAL AFRICAN REPUBLIC'S SPECIAL CRIMINAL COURT' -> 'The central african republic's ...'
    Загвар бүхэлдээ том үсгээр бичсэн текстийг сургалтад бараг хараагүй тул ("ЭРХИЙН ЭРХИЙН ЭРХИЙН..."
    гэж гацдаг) жижиг үсэг рүү хувиргаад орчуулна. Баримт дотор товчилсон үг (SCC, MINUSCA) хэвээр үлдэнэ."""
    out = CAPS_WORD.sub(lambda m: m.group(0) if m.group(0) in acronyms else m.group(0).lower(), s)
    return re.sub(r"^\W*\w", lambda m: m.group(0).upper(), out)  # эхний үсгийг том (“ хашилтын дараа ч)


class Busy(RuntimeError):
    """GPU санах ой дүүрсэн (жишээ нь сургалт зэрэг явж байна). Хэрэглэгчид "түр хүлээнэ үү" гэж хэлнэ."""


def _generate(tok, model, device, batch, beams):
    enc = tok(batch, return_tensors="pt", padding=True, truncation=True, max_length=MAX_TOKENS).to(device)
    gen = model.generate(
        **enc, num_beams=beams,
        # Орчуулга эх текстээсээ хэт урт болж "гацах"-аас сэргийлнэ
        max_new_tokens=min(2 * MAX_TOKENS, int(enc["input_ids"].shape[1] * 2.5) + 10),
        no_repeat_ngram_size=4,     # ижил 4 үгийн хэллэгийг давтахгүй
        repetition_penalty=1.1)
    return tok.batch_decode(gen, skip_special_tokens=True)


def _generate_safe(tok, model, device, batch):
    """GPU санах ой дутвал (2026-11-12: MADLAD сургалттай зэрэг ажиллахад 500 алдаа гарч байсан)
    санах ойг цэвэрлээд нэг нэгээр нь дахин оролдоно; бүтэхгүй бол Busy."""
    import torch
    try:
        return _generate(tok, model, device, batch, NUM_BEAMS)
    except torch.OutOfMemoryError:
        torch.cuda.empty_cache()
    res = []
    for item in batch:
        for beams in (NUM_BEAMS, 1):
            try:
                res += _generate(tok, model, device, [item], beams)
                break
            except torch.OutOfMemoryError:
                torch.cuda.empty_cache()
        else:
            raise Busy("Сервер одоо хэт ачаалалтай байна. Хэдэн минутын дараа дахин оролдоно уу.")
    return res


def _translate_sentences(sentences, direction, batch_size=16, on_progress=None):
    """Өгүүлбэр бүрийг орчуулна (хэт урт бол хэсэгчлэн). Ижил урттай жагсаалт буцаана."""
    import torch
    tok, model, device = _load(direction)
    # Жижиг үсэгтэй өгүүлбэрүүдэд том үсгээр бичигдсэн үгс = товчилсон нэрс (SCC, CAR, UN)
    orig = list(sentences)
    acronyms = {w for s in sentences if not _is_caps(s) for w in CAPS_WORD.findall(s)}
    caps = [_is_caps(s) for s in sentences]
    sentences = [_decap(s, acronyms) if c else s for s, c in zip(sentences, caps)]
    # Жагсаалтын дугаар/тэмдгийг ("12.", "a)", "•") загварт өгөхгүй, орчуулсны дараа буцааж залгана.
    # Өмнө нь загвар дугаарыг гээж, мөн дараагийн үгийг жижиг үсгээр эхлүүлдэг байсан.
    markers = [LIST_MARK.match(s) for s in sentences]
    prefixes = [m.group(0) if m and m.end() < len(s) else "" for m, s in zip(markers, sentences)]
    sentences = [s[len(p):] for s, p in zip(sentences, prefixes)]
    curly = [bool(re.search(r"[“”‘’«»„]", s)) for s in sentences]
    sentences = [_norm_quotes(s) for s in sentences]
    # Хашилтын тоо сондгой бол (ишлэл энэ өгүүлбэрээс эхэлсэн/дууссан) захын хашилтыг салгана
    qpre, qsuf = [], []
    for i, s in enumerate(sentences):
        pre = suf = ""
        if s.count('"') % 2 == 1:
            if s.startswith('"'):
                pre, s = '"', s[1:]
            elif s.endswith('"') or re.search(r'"[.!?]?$', s):
                suf = '"'; s = re.sub(r'"([.!?]?)$', r"\1", s)
        elif s.startswith('"') and s.endswith('"') and s.count('"') == 2:
            pre, suf, s = '"', '"', s[1:-1]
        qpre.append(pre); qsuf.append(suf); sentences[i] = s
    upper_start = [s[:1].isupper() for s in sentences]
    units = [(si, piece) for si, sent in enumerate(sentences) for piece in _pieces(sent, tok)]
    out = [""] * len(units)
    for start in range(0, len(units), batch_size):
        batch = [u[1] for u in units[start:start + batch_size]]
        with _lock, torch.inference_mode():
            out[start:start + len(batch)] = _generate_safe(tok, model, device, batch)
        if on_progress:
            on_progress(min(1.0, (start + len(batch)) / max(1, len(units))))
    result = [""] * len(sentences)
    for (si, _), text in zip(units, out):
        result[si] = (result[si] + " " + text).strip()
    # Эх нь гарчиг шиг бүхэлдээ том үсэгтэй байсан бол орчуулгыг ч том үсгээр буцаана
    result = [r.upper() if c else r for r, c in zip(result, caps)]
    result = [qa + r.strip('"') + qb if (qa or qb) else r for r, qa, qb in zip(result, qpre, qsuf)]
    result = [_restore_quotes(r, c) for r, c in zip(result, curly)]
    # Эх нь том үсгээр эхэлсэн бол орчуулга ч том үсгээр эхэлнэ (QA 2026-11-10: "Recommendations" -> "зөвлөмж")
    result = [(r[:1].upper() + r[1:]) if up and r[:1].islower() else r for r, up in zip(result, upper_start)]
    result = [_keep_emoji(src, r) for src, r in zip(orig, result)]
    if direction == "mn-en":
        result = [_gender_neutral(src, r) for src, r in zip(orig, result)]
    return [(p.strip() + " " + r) if p else r for p, r in zip(prefixes, result)]


# Монгол хэлэнд хүйсийн төлөөний үг байхгүй ("тэр"). Эх өгүүлбэрт хүйс заасан үг байхгүй бол загварын таамагласан
# he/she-г "he/she" болгоно (QA 2026-11-13: "Тэр эмч" -> "He", "Тэр сувилагч" -> "She" гэх хэвшмэл ойлголт).
GENDER_CUES = re.compile(r"эмэгтэй|эрэгтэй|ээж|аав|эх\b|эцэг|охин|хүү\b|хүүхэн|эхнэр|нөхөр|эгч|ах\b|ахын|дүүгийн|хадам|бэр|хатагтай|ноён|"
                         r"бүсгүй|залуу эр|эр хүн|эмэгтэйчүүд|эрчүүд|ээжийн|аавын|авгай|өвөө|эмээ|хөвгүүн|охид|жирэмсэн|эхчүүд|"
                         r"\bхатан\b|\bхаан\b|\bхунтайж\b|\bгүнж\b", re.I)
_NOT_NOUN = {"to", "for", "with", "at", "in", "on", "and", "or", "from", "by", "about", "that", "because", "when", "if", "as",
             "was", "is", "were", "has", "had", "will", "would", "could", "should", "not", "the", "a", "an", "into", "after", "before"}


def _gender_neutral(src, out):
    if GENDER_CUES.search(src) or not re.search(r"\b(he|she|him|her|his|hers|himself|herself)\b", out, re.I):
        return out

    def rep(m):
        w, nxt = m.group(1), (m.group(2) or "")
        lw = w.lower()
        if lw in ("he", "she"):
            r = "he/she"
        elif lw in ("himself", "herself"):
            r = "himself/herself"
        elif lw == "his":
            r = "his/her"
        elif lw == "him":
            r = "him/her"
        elif lw == "hers":
            r = "his/hers"
        else:  # "her": араас нь нэр үг бол эзэмших (his/her), үгүй бол тусагдахуун (him/her)
            r = "his/her" if nxt and nxt.lower() not in _NOT_NOUN else "him/her"
        return (r[0].upper() + r[1:] if w[0].isupper() else r) + m.group(0)[len(w):]
    return re.sub(r"\b(he|she|him|her|his|hers|himself|herself)\b(?=(?:\s+([A-Za-z]+))?)", rep, out, flags=re.I)


EMOJI = re.compile("(?:[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF](?:\uFE0F|\u200D[\U0001F000-\U0001FAFF\u2600-\u27BF])*)")


def _keep_emoji(src, out):
    """Загвар emoji-г гээдэг (QA 2026-11-13: "🕊️✊" алга болсон). Гээгдсэнийг төгсгөлд нь буцааж залгана."""
    out = re.sub(" {2,}", " ", re.sub("[\uFE0F\u200D]+(?=\\s|$)", "", out)).rstrip()
    missing = [e for e in EMOJI.findall(src) if e not in out]
    return (out + " " + "".join(missing)).strip() if missing else out


def translate_paragraphs(paragraphs, direction, batch_size=16, on_progress=None):
    """Файлд: paragraphs (list[str]) -> ижил урттай орчуулсан list[str]."""
    sents, owner = [], []
    for pi, para in enumerate(paragraphs):
        for seg in segment_text(para):
            sents.append(seg["text"])
            owner.append(pi)
    # Зөвхөн тоо/тэмдэгтэй мөрийг (хүснэгтийн нүд "640", "+14.7%", "2022-2023") загварт өгөхгүй, хэвээр нь үлдээнэ.
    # QA 2026-11-13: "640" -> "640 хүн" гэж байхгүй үг нэмж байсан.
    keep = [bool(re.fullmatch(r"[\d\s.,%+\-–—()/:;№#*$€₮]+", t)) for t in sents]
    todo = [t for t, k in zip(sents, keep) if not k]
    tr = iter(_translate_sentences(todo, direction, batch_size, on_progress) if todo else [])
    out = [t if k else next(tr) for t, k in zip(sents, keep)]
    result = [""] * len(paragraphs)
    for pi, text in zip(owner, out):
        result[pi] = (result[pi] + " " + text).strip()
    return result


# ---------------------------------------------------------------- хуулж оруулсан текстийг өгүүлбэрт хуваах
# PDF, вэб хуудаснаас хуулахад өгүүлбэрийн дунд мөр шилждэг ("...from the\nCAR and abroad").
# Тиймээс ганц мөр шилжилтийг зай гэж үзээд, зөвхөн дараах тохиолдолд шинэ догол мөр эхэлнэ:
#   - хоосон мөр (догол мөрийн жинхэнэ зааг)
#   - шинэ мөр жагсаалтын тэмдгээр эхэлсэн ("-", "•", "1.", "a)")
PARA_BREAK = re.compile(r"\n[ \t]*\n\s*|\n(?=[ \t]*(?:[-•*▪–]\s|\d{1,3}[.)]\s|[a-zа-я][.)]\s))")
SENT_END = re.compile(r"(?<=[.!?])[\"”»)]*\s+(?=[\"“«(]?[A-ZА-ЯЁӨҮ0-9])")


def _clean(s):
    s = re.sub(r"(?<=\w)-[ \t]*\n[ \t]*(?=\w)", "-", s)  # "Secretary-\nGeneral" -> "Secretary-General"
    return re.sub(r"\s+", " ", s).strip()


def _heading_breaks(text):
    """Гарчгийн мөрийг ("Overview", "1. Introduction") дараагийн өгүүлбэрээс салгах.
    Гарчиг гэж үзэх нөхцөл: богино (≤ 8 үг), цэг/таслалаар төгсөөгүй, том үсэг эсвэл тоогоор эхэлсэн,
    мөн дараагийн мөр нь том үсгээр эхэлсэн. Өгүүлбэрийн дундуур таслагдсан мөр ихэвчлэн урт эсвэл
    дараагийн мөр нь жижиг үсгээр эхэлдэг тул нөлөөлөхгүй."""
    out, pos = [], 0
    lines = text.split("\n")
    for i, line in enumerate(lines[:-1]):
        end = pos + len(line)
        nxt = lines[i + 1].lstrip()
        t = line.strip()
        if (t and len(t.split()) <= 14 and not re.search(r"[.,;:!?…\-–—(]$", t)
                and re.match(r"[\"“«(]?[A-ZА-ЯЁӨҮ0-9]", t) and re.match(r"[\"“«(]?[A-ZА-ЯЁӨҮ0-9]", nxt or "")
                and not re.search(r"\b(the|a|an|of|and|or|to|in|for|with|by|on|at|from|болон|ба|нь|гэж)$", t, re.I)):
            out.append((end, end + 1))
        pos = end + 1
    return out


def segment_text(text):
    """Текстийг өгүүлбэрүүдэд хуваана. Өгүүлбэр бүр эх текст дэх байрлалтай (start, end)
    тул вэб дээр орчуулсан өгүүлбэр дээр хулгана аваачихад эх өгүүлбэрийг тодруулж болно."""
    segs, para = [], 0
    pos = 0
    blocks = []
    breaks = [(m.start(), m.end()) for m in PARA_BREAK.finditer(text)] + _heading_breaks(text)
    for start, end in sorted(set(breaks)):
        if start < pos:
            continue
        blocks.append((pos, start))
        pos = end
    blocks.append((pos, len(text)))
    for b_start, b_end in blocks:
        block = text[b_start:b_end]
        if not block.strip():
            continue
        cur = 0
        cuts = [m.start() for m in SENT_END.finditer(block)] + [len(block)]
        for cut in cuts:
            raw = block[cur:cut]
            lead = len(raw) - len(raw.lstrip())
            body = raw.strip()
            if re.fullmatch(r"(\d{1,3}(\.\d{1,3})*|[a-zA-Zа-яА-Я])[.)]", body) and cut < len(block):
                continue  # "1." / "a)" жагсаалтын дугаарыг дараагийн өгүүлбэртэй хамт үлдээнэ
            if body:
                s0 = b_start + cur + lead
                segs.append({"start": s0, "end": s0 + len(body), "text": _clean(body), "para": para})
            cur = cut
        para += 1
    return segs


def translate_segments(text, direction):
    """Вэбийн текст орчуулга: [{start, end, para, source, translation}, ...]"""
    segs = segment_text(text)
    out = _translate_sentences([s["text"] for s in segs], direction)
    return [{"start": s["start"], "end": s["end"], "para": s["para"], "translation": t}
            for s, t in zip(segs, out)]


def translate_text(text, direction):
    segs = translate_segments(text, direction)
    paras = {}
    for s in segs:
        paras.setdefault(s["para"], []).append(s["translation"])
    return "\n\n".join(" ".join(v) for _, v in sorted(paras.items()))
