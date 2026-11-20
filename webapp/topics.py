"""Датаг АГУУЛГААР нь (сэдвээр) бүлэглэх. Гадны AI ашиглахгүй: энэ компьютер дээрх LaBSE-ээр өгүүлбэрийн утгыг
сэдэв бүрийн жишээ өгүүлбэрүүдтэй харьцуулна (хэрэглэгч 2026-11-15).
Дүрэм:
  * Сэдвийн нэрийг солиход дата нь хэвээр (сэдэв дотоод ID-тай).
  * Сэдвийг устгахад түүний дата хамгийн ойр бусад сэдэв рүү, ойр сэдэв байхгүй бол "Бусад" руу шилжинэ.
  * Аль ч сэдэвтэй хангалттай ойр биш өгүүлбэр "Бусад"-д орно.
  * "Бусад" дотор том, нэгэн төрлийн бүлэг гарч ирвэл админд "шинэ сэдэв болгох уу?" гэж санал болгоно.
"""
import json, os, re, sqlite3
from datetime import datetime, timezone

OTHER = "other"
MIN_DENSITY = 8.0           # 1000 үгэнд ногдох түлхүүр үгийн тоо үүнээс бага бол "Бусад" (олон сэдэвт ерөнхий текст)
SUGGEST_MIN_SHARE = 0.01    # "Бусад" доторх бүлэг нийт датаны ≥1% бол санал болгоно
SUGGEST_MIN_N = 300

# Сэдэв бүр: (id, нэр, түлхүүр үгс). Түлхүүр үг нь үгийн эхлэл байж болно ("эрүүдэн" -> эрүүдэн шүүх, эрүүдэн шүүлт).
# Админ нэмж, засаж болно. Гадны AI шаардлагагүй, ойлгомжтой.
DEFAULT_TOPICS = [
 ("hr", "Хүний эрх, эрх чөлөө", ["human right", "torture", "detention", "detainee", "fair trial", "freedom of expression", "freedom of assembly",
   "defender", "death penalty", "ill-treatment", "discriminat", "refugee", "asylum", "prisoner",
   "хүний эрх", "эрүүдэн", "хорих", "цагдан хор", "шударга шүүх", "үзэл бодол", "жагсаал", "ялгаварла", "эрх хамгаалагч", "хоригдол", "дүрвэгс", "эрх зөрч"]),
 ("gender", "Эмэгтэйчүүд, хүүхэд, жендэр", ["women", "girl", "gender", "domestic violence", "child", "sexual", "maternal", "reproductive",
   "эмэгтэй", "жендэр", "гэр бүлийн хүчирхийл", "хүүхд", "хүүхэд", "бэлгийн", "жирэмсэн", "охид"]),
 ("env", "Байгаль орчин, уур амьсгал", ["environment", "climate", "pollution", "emission", "biodiversity", "wildlife", "groundwater", "waste",
   "desertification", "air quality", "байгаль орчин", "уур амьсгал", "бохирдол", "ялгарал", "ан амьтан", "гүний ус", "хог хаягдал", "цөлжилт", "агаарын"]),
 ("mining", "Уул уурхай, газар, малчид", ["mine", "mining", "copper", "exploration", "licence", "license", "herder", "pasture", "livestock", "resettlement",
   "tailings", "уурхай", "зэс", "хайгуул", "тусгай зөвшөөрөл", "малчин", "бэлчээр", "мал аж ахуй", "нүүлгэн шилжүүл", "хүдэр"]),
 ("econ", "Эдийн засаг, санхүү, төсөв", ["gdp", "budget", "fiscal", "inflation", "economic", "economy", "revenue", "tax", "debt", "investment", "bank",
   "finance", "export", "эдийн засаг", "төсөв", "инфляц", "орлого", "татвар", "хөрөнгө оруулалт", "банк", "санхүү", "экспорт", "дотоодын нийт"]),
 ("health", "Эрүүл мэнд", ["health", "hospital", "disease", "medical", "patient", "mortality", "vaccin", "hiv", "tuberculosis",
   "эрүүл мэнд", "эмнэлэг", "өвчин", "эмчилгээ", "өвчтөн", "эндэгдэл", "вакцин", "сүрьеэ"]),
 ("edu", "Боловсрол", ["education", "school", "teacher", "student", "learning", "curriculum", "universit",
   "боловсрол", "сургууль", "багш", "сурагч", "оюутан", "их сургууль"]),
 ("law", "Хууль, засаглал, төр", ["law", "parliament", "constitution", "court", "legislation", "governance", "corruption", "public administration",
   "хууль", "заалт", "их хурал", "үндсэн хууль", "шүүх", "засаглал", "авлига", "төрийн байгууллага"]),
 ("social", "Хөдөлмөр, нийгмийн хамгаалал", ["employment", "unemploy", "labour", "labor", "wage", "pension", "social welfare", "social protection",
   "social insurance", "poverty", "хөдөлмөр", "ажилгүй", "цалин", "тэтгэвэр", "нийгмийн халамж", "нийгмийн даатгал", "ядуурал", "ажил эрхлэлт"]),
]
OTHER_NAME = "Бусад / ерөнхий текст"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (id TEXT PRIMARY KEY, name TEXT NOT NULL, examples TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS topic_suggest_dismissed (id TEXT PRIMARY KEY, centroid TEXT NOT NULL, at TEXT NOT NULL, by TEXT NOT NULL);
"""


def init(con):
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(contrib_pairs)")}
    if "topic" not in cols:
        con.execute("ALTER TABLE contrib_pairs ADD COLUMN topic TEXT")
    if not con.execute("SELECT 1 FROM topics").fetchone():
        for tid, name, ex in DEFAULT_TOPICS:
            con.execute("INSERT INTO topics(id,name,examples,created_at,created_by) VALUES (?,?,?,?,?)",
                        (tid, name, json.dumps(ex, ensure_ascii=False), _now(), "system"))


def active_topics(con):
    """examples багана = түлхүүр үгсийн жагсаалт (JSON)."""
    return [dict(id=r[0], name=r[1], examples=json.loads(r[2])) for r in
            con.execute("SELECT id, name, examples FROM topics WHERE active=1 ORDER BY created_at, id")]


def compile_kw(words):
    """Түлхүүр үгсийг нэг regex болгоно: үгийн эхнээс таарна (англи: үгийн хил, монгол: үгийн эхлэл)."""
    parts = sorted({w.strip().lower() for w in words if len(w.strip()) >= 2}, key=len, reverse=True)
    if not parts:
        return None
    return re.compile(r"(?<![\w])(?:" + "|".join(re.escape(p) for p in parts) + ")", re.I)


def score_text(text, compiled):
    """compiled: [(tid, regex)]. Буцаах: {tid: 1000 үгэнд ногдох түлхүүр үгийн тоо}."""
    n = max(1, len(text.split()))
    return {tid: (len(rx.findall(text)) * 1000.0 / n if rx else 0.0) for tid, rx in compiled}


def pick(scores, min_density=MIN_DENSITY):
    if not scores:
        return OTHER, 0.0
    tid = max(scores, key=scores.get)
    return (tid, scores[tid]) if scores[tid] >= min_density else (OTHER, scores[tid])


MN_STOP = set("ба болон нь нэг юм энэ тэр бол байна байгаа байсан гэж гэх гэсэн зэрэг мөн хүртэл дээр доор ч ийм тийм бөгөөд буюу эсвэл "
              "хамт оны онд сарын өдрийн хувь хувиар тухай дагуу шаардлагатай байх болно байдал хийх".split())
EN_STOP = set("the of and to in a for is on that by with as are be at or from this an was were it its which has have not will".split())


def keywords(texts, n=6):
    from collections import Counter
    c = Counter()
    for t in texts:
        for w in re.findall(r"[A-Za-zА-Яа-яЁёӨөҮү]{4,}", t.lower()):
            if w not in MN_STOP and w not in EN_STOP:
                c[w] += 1
    return [w for w, _ in c.most_common(n)]
