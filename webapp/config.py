"""Вебийн бүх тохиргоо нэг дор. Хязгаарыг өөрчлөх бол зөвхөн энд засна."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)

# --- Хэрэглэгчийн 4 төрөл ба хязгаар ---
#  1. Үнэгүй хэрэглэгч (бүртгэлгүй зочин "guest"): 1200 тэмдэгт, сард 1 файл.
#     Бүртгүүлбэл ("unpaid"): +1 файл (сард 2), эхний 14 хоногт 5000 тэмдэгт ("trial").
#  2. Төлбөр төлсөн ("paid"): 5000 тэмдэгт, сард 30 файл.
#  3. Датасет өгсөн ("contributor"): төлбөртэйтэй адил, сард 50 файл, төлбөргүй, админ удирдана.
#  4. Админ ("admin"): бүх эрх, хязгааргүй.
#  5. Монголын Эмнести Интернэшнл ("amnesty_mn"): байгууллагын эрх, датасет өгсөнтэй адил (5000, сард 50), төлбөргүй.
TEXT_LIMITS = {"guest": 1200, "unpaid": 1200, "trial": 5000, "paid": 5000, "contributor": 5000,
               "amnesty_mn": 5000, "admin": 5000}
FILES_PER_MONTH = {"guest": 1, "unpaid": 2, "paid": 30, "contributor": 50, "amnesty_mn": 50,
                   "admin": None}  # None = хязгааргүй
TRIAL_DAYS = 14                # бүртгүүлснээс хойш 5000 тэмдэгтийн эрхтэй байх хоног
MAX_TEXT_CHARS = max(TEXT_LIMITS.values())
MAX_FILE_PAGES = 20            # нэг файлын дээд хуудас
GUEST_EMAIL = "guest@system.local"  # бүртгэлгүй зочдын файлыг холбох дотоод бүртгэл (нэвтрэх боломжгүй)
MAX_UPLOAD_MB = 15             # файлын дээд хэмжээ
CHARS_PER_PAGE = 1800          # DOCX/TXT-д хуудас тоолох ойролцоо утга

# --- Эрхийн төрлүүд ---
#  "unpaid"      - бүртгүүлсэн ч төлбөр төлөөгүй (эхний 14 хоногт туршилтын эрхтэй)
#  "paid"        - төлбөртэй: paid_until огноо хүртэл бүрэн эрхтэй
#  "contributor" - датасет өгсөн хүмүүс: үнэгүй, хугацаагүй бүрэн эрхтэй
#  "admin"       - бүх эрх + админ самбар
PLANS = ["unpaid", "paid", "contributor", "amnesty_mn", "admin"]
PLAN_LABELS = {
    "unpaid": "Үнэгүй (бүртгэлтэй)",
    "paid": "Төлбөртэй",
    "contributor": "Хувь нэмэр оруулагч (үнэгүй)",
    "amnesty_mn": "Монголын Эмнести Интернэшнл",
    "admin": "Админ",
}

# --- Загварууд: чиглэл бүрт нэг загвар. Байхгүй чиглэл вебэд идэвхгүй харагдана. ---
MODELS = {
    "en-mn": os.environ.get("MODEL_EN_MN", os.path.join(ROOT_DIR, "benchmark", "nllb13_v9_ot", "final")),  # 2026-11-12: v8 + Оюу Толгой 60k + хүний эрх x3 (өмнөх: nllb13_v6_bt)
    "mn-en": os.environ.get("MODEL_MN_EN", os.path.join(ROOT_DIR, "benchmark", "nllb13_mn_en_v3", "final")),  # 2026-11-12: v2 + Оюу Толгой/UN (өмнөх: nllb13_mn_en_v2)
}
LANG_CODES = {"en": "eng_Latn", "mn": "khk_Cyrl"}
MAX_TOKENS = 128               # загварыг сургасан урт (train.py MAX_LEN)
NUM_BEAMS = 4

# --- Хадгалах газар ---
DB_PATH = os.environ.get("APP_DB", os.path.join(BASE_DIR, "instance", "app.db"))
UPLOAD_DIR = os.path.join(os.path.dirname(DB_PATH), "uploads")    # орчуулсны дараа устгагдана
RESULT_DIR = os.path.join(os.path.dirname(DB_PATH), "results")    # татаж авсны дараа / 24 цагийн дараа устгагдана
RESULT_TTL_HOURS = 24

SESSION_DAYS = 14

# --- Үнэ (төгрөг). 3 сарынх нь сарын үнийн 3 сараас 20% хямд (100₮-өөр доош бүхэлчилсэн). ---
MONTHLY_PRICE = 19_900
QUARTERLY_DISCOUNT = 0.20
PRICES = {"monthly": MONTHLY_PRICE,
          "quarterly": int(MONTHLY_PRICE * 3 * (1 - QUARTERLY_DISCOUNT)) // 100 * 100}   # = 47,700₮
# И-баримт олгох систем бэлэн болтол бүх хуудаснаас нуух. Бэлэн болмогц True болгоно.
EBARIMT_ENABLED = False
SEAT_MAX = 1               # одоогоор нэг захиалга = нэг хэрэглэгчийн эрх (байгууллага нь зөвхөн нэхэмжлэх/И-баримтад)

# --- Төлбөрийн мэдээлэл. ⚠️ ЖИШЭЭ: жинхэнэ мэдээллээр солино уу! ---
PAYMENT = {
    "bank_name": "Хаан банк",
    "account_number": "5753859695",
    "iban": "MN64 0005 00 5753859695",
    "account_holder": "Оюунбат Батзориг",
    "qpay_enabled": False,   # QPay merchant API холбогдсон үед True болгоно
    "contact_email": "info@somename.com",           # ЖИШЭЭ: албан ёсны хаягаар солино
    "contact_phone": "7230-4432",
}

# Нэвтрээгүй зочин нэг IP хаягаас минутад хэдэн удаа орчуулж болох вэ (GPU-г хамгаалах)
GUEST_REQUESTS_PER_MINUTE = 20
