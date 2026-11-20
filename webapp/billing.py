"""Үнэ, худалдан авалт, төлбөр (данс / QPay) ба админы баталгаажуулалт.

Урсгал (Bolorsoft-ийн худалдан авах алхмуудтай ижил санаа):
  1. /pricing           багцаа сонгоно (1 сар / 3 сар)
  2. /buy               хувь хүн эсвэл байгууллага, мэдээлэл, төлбөрийн хэлбэр
  3. /orders/<код>      дансны мэдээлэл эсвэл QPay QR, "Төлбөр төлсөн" товч
  4. Админ самбар       орлогыг шалгаад "Баталгаажуулах" -> эрх автоматаар нээгдэнэ

Одоогоор QPay API холбогдоогүй тул төлбөрийг админ гараар баталгаажуулна.
Жинхэнэ мэдээллийг config.py-ийн PAYMENT хэсэгт солино.
"""
import secrets
from datetime import date, timedelta

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import db
from config import EBARIMT_ENABLED, PAYMENT, PRICES, SEAT_MAX

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id            TEXT PRIMARY KEY,        -- гүйлгээний утга болгон бичүүлэх код, жишээ: HR-482913
    user_id       INTEGER NOT NULL REFERENCES users(id),
    period        TEXT NOT NULL,           -- monthly / quarterly (хуучин захиалгад yearly байж болно)
    seats         INTEGER NOT NULL DEFAULT 1,
    customer_type TEXT NOT NULL,           -- person / org
    contact_name  TEXT NOT NULL DEFAULT '',
    phone         TEXT NOT NULL DEFAULT '',
    org_name      TEXT NOT NULL DEFAULT '',
    org_register  TEXT NOT NULL DEFAULT '',
    ebarimt       INTEGER NOT NULL DEFAULT 0,
    method        TEXT NOT NULL,           -- bank / qpay
    amount        INTEGER NOT NULL,
    status        TEXT NOT NULL,           -- pending / claimed / confirmed / cancelled
    created_at    TEXT NOT NULL,
    claimed_at    TEXT,
    confirmed_at  TEXT
);
"""

STATUS_LABELS = {"pending": "Төлбөр хүлээгдэж байна", "claimed": "Төлсөн гэж мэдэгдсэн, шалгаж байна",
                 "confirmed": "Баталгаажсан", "cancelled": "Цуцлагдсан"}
PERIOD_LABELS = {"monthly": "1 сар", "quarterly": "3 сар", "yearly": "1 жил"}
PERIOD_MONTHS = {"monthly": 1, "quarterly": 3, "yearly": 12}


def price(period, seats=1):
    return PRICES[period] * max(1, min(int(seats), SEAT_MAX))


def new_order_id(con):
    while True:
        oid = f"HR-{secrets.randbelow(900000) + 100000}"
        if not con.execute("SELECT 1 FROM orders WHERE id=?", (oid,)).fetchone():
            return oid


def extend_user(con, user_id, months):
    """Төлбөр баталгаажмагц эрхийг сунгана (идэвхтэй бол дуусах огнооноос нь цааш)."""
    u = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    active = u["plan"] == "paid" and u["paid_until"] and u["paid_until"] >= date.today().isoformat()
    start = date.fromisoformat(u["paid_until"]) if active else date.today()
    until = start + timedelta(days=30 * months if months < 12 else 365)
    since = u["paid_since"] if active and u["paid_since"] else date.today().isoformat()
    new_plan = u["plan"] if u["plan"] in ("admin", "contributor", "amnesty_mn") else "paid"
    con.execute("UPDATE users SET plan=?, paid_until=?, paid_since=? WHERE id=?",
                (new_plan, until.isoformat(), since, user_id))


def register(app, page, current_user, require_admin):
    """main.py-аас дуудаж хаягуудыг (route) бүртгэнэ."""

    @app.get("/pricing", response_class=HTMLResponse)
    def pricing(request: Request):
        return page(request, "pricing.html", prices=PRICES, payment=PAYMENT)

    @app.get("/buy", response_class=HTMLResponse)
    def buy_form(request: Request, period: str = "quarterly", edit: str = ""):
        user = current_user(request)
        if not user:
            return RedirectResponse(f"/register?next=/buy?period={period}", status_code=303)
        prev, start = {}, 1
        if edit:  # захиалгын хуудаснаас "Буцах" дарсан: өмнөх сонголтыг бөглөж, төлбөрийн алхмаас эхэлнэ
            with db.connect() as con:
                o = con.execute("SELECT * FROM orders WHERE id=? AND user_id=? AND status IN ('pending','claimed')",
                                (edit, user["id"])).fetchone()
            if o:
                prev, period, start = dict(o), o["period"], 3
        if period not in PRICES:
            period = "quarterly"
        return page(request, "buy.html", period=period, prices=PRICES, seat_max=SEAT_MAX, payment=PAYMENT,
                    error=None, prev=prev, start_step=start)

    @app.post("/buy")
    def buy(request: Request, period: str = Form(...), customer_type: str = Form("person"),
            seats: int = Form(1), contact_name: str = Form(""), phone: str = Form(""),
            org_name: str = Form(""), org_register: str = Form(""), ebarimt: str = Form(""),
            method: str = Form("bank"), edit: str = Form("")):
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)

        def again(msg):
            return page(request, "buy.html", period=period, prices=PRICES, seat_max=SEAT_MAX,
                        payment=PAYMENT, error=msg, prev={}, start_step=1)
        if period not in PRICES or method not in ("bank", "qpay") or customer_type not in ("person", "org"):
            return again("Сонголтоо шалгана уу.")
        if not contact_name.strip() or not phone.strip():
            return again("Нэр, утасны дугаараа оруулна уу.")
        if customer_type == "org" and (not org_name.strip() or not org_register.strip()):
            return again("Байгууллагын нэр, регистрийн дугаарыг оруулна уу.")
        seats = 1  # байгууллага = зөвхөн байгууллагын нэр дээрх нэхэмжлэх/И-баримт, нэг хэрэглэгчийн эрх
        with db.connect() as con:
            if edit:  # засварласан бол хуучин (төлөгдөөгүй) захиалгыг шинээр солино
                con.execute("UPDATE orders SET status='cancelled' WHERE id=? AND user_id=? AND status IN ('pending','claimed')",
                            (edit, user["id"]))
            oid = new_order_id(con)
            con.execute(
                "INSERT INTO orders(id,user_id,period,seats,customer_type,contact_name,phone,org_name,"
                "org_register,ebarimt,method,amount,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, user["id"], period, seats, customer_type, contact_name.strip()[:100], phone.strip()[:30],
                 org_name.strip()[:150], org_register.strip()[:20], 1 if (ebarimt and EBARIMT_ENABLED) else 0, method,
                 price(period, seats), "pending", db.now_iso()))
        return RedirectResponse(f"/orders/{oid}", status_code=303)

    def own_order(request, oid):
        user = current_user(request)
        if not user:
            raise HTTPException(403, "Нэвтэрнэ үү.")
        with db.connect() as con:
            o = con.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, user["id"])).fetchone()
        if not o:
            raise HTTPException(404, "Захиалга олдсонгүй.")
        return user, dict(o)

    @app.get("/orders/{oid}", response_class=HTMLResponse)
    def order_page(request: Request, oid: str):
        _, o = own_order(request, oid)
        return page(request, "order.html", o=o, payment=PAYMENT, status_labels=STATUS_LABELS,
                    period_labels=PERIOD_LABELS)

    @app.post("/orders/{oid}/claim")
    def order_claim(request: Request, oid: str):
        _, o = own_order(request, oid)
        if o["status"] == "pending":
            with db.connect() as con:
                con.execute("UPDATE orders SET status='claimed', claimed_at=? WHERE id=?", (db.now_iso(), oid))
        return RedirectResponse(f"/orders/{oid}", status_code=303)

    @app.post("/orders/{oid}/cancel")
    def order_cancel(request: Request, oid: str):
        _, o = own_order(request, oid)
        if o["status"] in ("pending", "claimed"):
            with db.connect() as con:
                con.execute("UPDATE orders SET status='cancelled' WHERE id=?", (oid,))
        return RedirectResponse("/account", status_code=303)

    @app.post("/admin/orders/{oid}")
    def admin_order(request: Request, oid: str, action: str = Form(...)):
        require_admin(request)
        with db.connect() as con:
            o = con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if not o:
                raise HTTPException(404)
            if action == "confirm" and o["status"] in ("pending", "claimed"):
                extend_user(con, o["user_id"], PERIOD_MONTHS[o["period"]])
                con.execute("UPDATE orders SET status='confirmed', confirmed_at=? WHERE id=?", (db.now_iso(), oid))
            elif action == "cancel" and o["status"] in ("pending", "claimed"):
                con.execute("UPDATE orders SET status='cancelled' WHERE id=?", (oid,))
        return RedirectResponse("/admin#orders", status_code=303)
