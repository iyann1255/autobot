import os
import json
import hmac
import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

from dotenv import load_dotenv
from aiohttp import web

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

# =========================
# CONFIG
# =========================
load_dotenv()
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("auto-order-ipaymu")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN belum diisi.")

ADMIN_IDS = set()
for x in os.getenv("ADMIN_IDS", "").split(","):
    x = x.strip()
    if x.isdigit():
        ADMIN_IDS.add(int(x))

DB_PATH = os.getenv("DB_PATH", "data.sqlite").strip()

IPAYMU_VA = os.getenv("IPAYMU_VA", "").strip()
IPAYMU_API_KEY = os.getenv("IPAYMU_API_KEY", "").strip()
IPAYMU_BASE_URL = os.getenv("IPAYMU_BASE_URL", "https://sandbox.ipaymu.com").strip().rstrip("/")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
BIND_HOST = os.getenv("BIND_HOST", "0.0.0.0").strip()
BIND_PORT = int(os.getenv("BIND_PORT", "8080").strip())

if not IPAYMU_VA or not IPAYMU_API_KEY or not PUBLIC_BASE_URL:
    raise SystemExit("IPAYMU_VA / IPAYMU_API_KEY / PUBLIC_BASE_URL wajib diisi.")

# notify endpoint
NOTIFY_PATH = "/ipaymu/notify"
NOTIFY_URL = f"{PUBLIC_BASE_URL}{NOTIFY_PATH}"

# =========================
# DB
# =========================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            note TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            product_id INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            note TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'PENDING', -- PENDING|PAYMENT_CREATED|PAID|DONE|CANCELLED|EXPIRED
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,

            -- iPaymu data
            reference_id TEXT UNIQUE,
            ipaymu_sid TEXT DEFAULT '',
            ipaymu_trx_id TEXT DEFAULT '',
            pay_url TEXT DEFAULT ''
        );
        """)

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# =========================
# iPaymu helpers
# =========================
def ipaymu_timestamp() -> str:
    # Format: YYYYMMDDhhmmss
    return datetime.now().strftime("%Y%m%d%H%M%S")

def _json_minified(data: Dict[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)

def ipaymu_signature(method: str, va: str, body: Dict[str, Any], secret: str) -> Tuple[str, str]:
    """
    StringToSign (yang umum dipakai di sample/SDK):
      METHOD:VA:SHA256(MINIFIED_JSON_BODY):SECRET
    signature:
      HMAC_SHA256(StringToSign, SECRET)
    """
    json_body = _json_minified(body)
    body_hash = hashlib.sha256(json_body.encode("utf-8")).hexdigest().lower()
    string_to_sign = f"{method.upper()}:{va}:{body_hash}:{secret}"
    sig = hmac.new(secret.encode("latin-1"), string_to_sign.encode("latin-1"), hashlib.sha256).hexdigest()
    ts = ipaymu_timestamp()
    return sig, ts

async def ipaymu_create_qris_redirect_payment(
    session, *, reference_id: str, product_name: str, qty: int, price: int,
    buyer_name: str, buyer_phone: str, buyer_email: str
) -> Dict[str, Any]:
    """
    Redirect Payment API: POST /api/v2/payment
    Kita set paymentMethod=qris supaya diarahkan ke QRIS.
    """
    url = f"{IPAYMU_BASE_URL}/api/v2/payment"

    body = {
        "product": [product_name],
        "qty": [qty],
        "price": [price],
        "description": [f"Order {reference_id}"],
        "returnUrl": f"{PUBLIC_BASE_URL}/thanks",   # optional (kamu bisa ganti)
        "cancelUrl": f"{PUBLIC_BASE_URL}/cancel",   # optional (kamu bisa ganti)
        "notifyUrl": NOTIFY_URL,
        "referenceId": reference_id,
        "buyerName": buyer_name or "Buyer",
        "buyerPhone": buyer_phone or "",
        "buyerEmail": buyer_email or "",
        "paymentMethod": "qris",
    }

    signature, ts = ipaymu_signature("POST", IPAYMU_VA, body, IPAYMU_API_KEY)
    headers = {
        "Content-Type": "application/json",
        "va": IPAYMU_VA,
        "signature": signature,
        "timestamp": ts,
    }

    async with session.post(url, data=_json_minified(body).encode("utf-8"), headers=headers) as resp:
        data = await resp.json(content_type=None)
        if resp.status != 200:
            raise RuntimeError(f"iPaymu HTTP {resp.status}: {data}")
        return data

# =========================
# BOT UI
# =========================
def rupiah(n: int) -> str:
    return f"Rp{n:,}".replace(",", ".")

def kb_main(is_admin_user: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🛒 Katalog", callback_data="cat")],
        [InlineKeyboardButton("📦 Order Saya", callback_data="my")],
    ]
    if is_admin_user:
        rows.append([InlineKeyboardButton("⚙️ Admin: Produk", callback_data="adm_products")])
        rows.append([InlineKeyboardButton("🧾 Admin: Order", callback_data="adm_orders")])
    return InlineKeyboardMarkup(rows)

def kb_products(items: List[sqlite3.Row], prefix="buy_") -> InlineKeyboardMarkup:
    rows = []
    for p in items[:20]:
        rows.append([InlineKeyboardButton(f"{p['name']} • {rupiah(p['price'])}", callback_data=f"{prefix}{p['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    return InlineKeyboardMarkup(rows)

def kb_admin_products(items: List[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("➕ Tambah Produk", callback_data="adm_add")],
    ]
    for p in items[:20]:
        status = "ON" if p["active"] else "OFF"
        rows.append([InlineKeyboardButton(f"{p['name']} ({status})", callback_data=f"adm_edit_{p['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    return InlineKeyboardMarkup(rows)

def gen_reference_id(user_id: int, order_id: int) -> str:
    # unik + mudah ditrack
    return f"ORD-{user_id}-{order_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    text = (
        "Auto Order siap jalan.\n\n"
        "Pilih dulu produknya, nanti aku buatin QRIS iPaymu.\n"
        f"Notify URL: `{NOTIFY_URL}`"
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main(is_admin(u.id))
    )

async def on_home(q, u):
    await q.edit_message_text("Menu:", reply_markup=kb_main(is_admin(u.id)))

# =========================
# Admin: Add/Edit Produk (via chat command sederhana)
# =========================
# Format:
# /addprod Nama Produk | 20000 | catatan opsional
# /setprod ID | Nama Baru | 25000 | active=1 | catatan
# /delprod ID
async def addprod_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        return await update.message.reply_text("Khusus admin.")
    raw = update.message.text.replace("/addprod", "", 1).strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 2:
        return await update.message.reply_text("Format: /addprod Nama | harga | catatan(optional)")
    name = parts[0]
    if not name:
        return await update.message.reply_text("Nama produk kosong.")
    if not parts[1].isdigit():
        return await update.message.reply_text("Harga harus angka (tanpa titik/koma).")
    price = int(parts[1])
    note = parts[2] if len(parts) >= 3 else ""
    with db() as conn:
        conn.execute("INSERT INTO products(name, price, active, note) VALUES(?,?,1,?)", (name, price, note))
    await update.message.reply_text(f"OK. Produk ditambah: {name} ({rupiah(price)})")

async def setprod_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        return await update.message.reply_text("Khusus admin.")
    raw = update.message.text.replace("/setprod", "", 1).strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 3:
        return await update.message.reply_text("Format: /setprod ID | Nama | harga | active=1/0(optional) | catatan(optional)")
    if not parts[0].isdigit():
        return await update.message.reply_text("ID harus angka.")
    pid = int(parts[0])
    name = parts[1]
    if not parts[2].isdigit():
        return await update.message.reply_text("Harga harus angka.")
    price = int(parts[2])

    active = None
    note = None
    for p in parts[3:]:
        if p.startswith("active="):
            v = p.split("=", 1)[1].strip()
            if v in ("0", "1"):
                active = int(v)
        else:
            note = p

    with db() as conn:
        row = conn.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone()
        if not row:
            return await update.message.reply_text("Produk tidak ditemukan.")
        if active is None:
            conn.execute("UPDATE products SET name=?, price=? WHERE id=?", (name, price, pid))
        else:
            conn.execute("UPDATE products SET name=?, price=?, active=? WHERE id=?", (name, price, active, pid))
        if note is not None:
            conn.execute("UPDATE products SET note=? WHERE id=?", (note, pid))
    await update.message.reply_text(f"OK. Produk #{pid} diupdate.")

async def delprod_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        return await update.message.reply_text("Khusus admin.")
    parts = update.message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await update.message.reply_text("Format: /delprod ID")
    pid = int(parts[1])
    with db() as conn:
        conn.execute("DELETE FROM products WHERE id=?", (pid,))
    await update.message.reply_text(f"OK. Produk #{pid} dihapus.")

# =========================
# Katalog + Buat Order + Create QRIS
# =========================
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = update.effective_user
    data = q.data

    if data == "home":
        return await on_home(q, u)

    if data == "cat":
        with db() as conn:
            items = conn.execute("SELECT * FROM products WHERE active=1 ORDER BY id DESC").fetchall()
        if not items:
            return await q.edit_message_text("Belum ada produk aktif.", reply_markup=kb_main(is_admin(u.id)))
        return await q.edit_message_text("Katalog (klik untuk order):", reply_markup=kb_products(items, prefix="buy_"))

    if data.startswith("buy_"):
        pid = int(data.split("_", 1)[1])
        with db() as conn:
            p = conn.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if not p:
            return await q.edit_message_text("Produk tidak tersedia.", reply_markup=kb_main(is_admin(u.id)))

        # Step simpel: qty=1 default + catatan via reply
        context.user_data["pending_pid"] = pid
        return await q.edit_message_text(
            f"Produk: *{p['name']}*\nHarga: *{rupiah(p['price'])}*\n\n"
            "Balas chat ini dengan format:\n"
            "`qty | catatan`\n"
            "Contoh: `1 | ubot 1 bulan (username @xxx)`\n\n"
            "Atau ketik `cancel` buat batal.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="cat")]])
        )

    if data == "my":
        with db() as conn:
            rows = conn.execute(
                "SELECT o.*, p.name AS product_name FROM orders o JOIN products p ON p.id=o.product_id "
                "WHERE o.user_id=? ORDER BY o.id DESC LIMIT 10",
                (u.id,)
            ).fetchall()
        if not rows:
            return await q.edit_message_text("Belum ada order.", reply_markup=kb_main(is_admin(u.id)))

        lines = []
        for r in rows:
            lines.append(
                f"• #{r['id']} {r['product_name']} x{r['qty']} — *{r['status']}* — {rupiah(r['amount'])}"
            )
            if r["pay_url"]:
                lines.append(f"  Bayar: {r['pay_url']}")
        return await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main(is_admin(u.id)))

    if data == "adm_products":
        if not is_admin(u.id):
            return await q.edit_message_text("Khusus admin.", reply_markup=kb_main(False))
        with db() as conn:
            items = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
        return await q.edit_message_text(
            "Admin Produk:\n\n"
            "Cmd cepat:\n"
            "`/addprod Nama | harga | catatan`\n"
            "`/setprod ID | Nama | harga | active=1/0 | catatan`\n"
            "`/delprod ID`\n",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_admin_products(items),
        )

    if data == "adm_orders":
        if not is_admin(u.id):
            return await q.edit_message_text("Khusus admin.", reply_markup=kb_main(False))
        with db() as conn:
            rows = conn.execute(
                "SELECT o.*, p.name AS product_name FROM orders o JOIN products p ON p.id=o.product_id "
                "ORDER BY o.id DESC LIMIT 12"
            ).fetchall()
        if not rows:
            return await q.edit_message_text("Belum ada order.", reply_markup=kb_main(True))

        lines = ["Order terbaru:"]
        kb = []
        for r in rows:
            lines.append(f"• #{r['id']} @{r['username'] or r['user_id']} — {r['product_name']} x{r['qty']} — {r['status']}")
            kb.append([
                InlineKeyboardButton(f"✅ Done #{r['id']}", callback_data=f"adm_done_{r['id']}"),
                InlineKeyboardButton(f"❌ Cancel #{r['id']}", callback_data=f"adm_cancel_{r['id']}"),
            ])
        kb.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
        return await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))

    if data.startswith("adm_done_"):
        if not is_admin(u.id):
            return
        oid = int(data.split("_", 2)[2])
        with db() as conn:
            conn.execute("UPDATE orders SET status='DONE', updated_at=? WHERE id=?", (now_str(), oid))
        return await q.edit_message_text(f"Order #{oid} => DONE", reply_markup=kb_main(True))

    if data.startswith("adm_cancel_"):
        if not is_admin(u.id):
            return
        oid = int(data.split("_", 2)[2])
        with db() as conn:
            conn.execute("UPDATE orders SET status='CANCELLED', updated_at=? WHERE id=?", (now_str(), oid))
        return await q.edit_message_text(f"Order #{oid} => CANCELLED", reply_markup=kb_main(True))

async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    text = (update.message.text or "").strip()

    # Kalau user lagi input qty|catatan setelah klik produk
    if "pending_pid" in context.user_data:
        if text.lower() == "cancel":
            context.user_data.pop("pending_pid", None)
            return await update.message.reply_text("Batal. Balik ke menu.", reply_markup=kb_main(is_admin(u.id)))

        m = [p.strip() for p in text.split("|", 1)]
        if len(m) < 1 or not m[0].isdigit():
            return await update.message.reply_text("Format salah. Contoh: `1 | ubot 1 bulan @username`", parse_mode=ParseMode.MARKDOWN)
        qty = int(m[0])
        note = m[1] if len(m) > 1 else ""

        pid = int(context.user_data["pending_pid"])
        context.user_data.pop("pending_pid", None)

        with db() as conn:
            p = conn.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
            if not p:
                return await update.message.reply_text("Produk sudah tidak tersedia.")
            amount = int(p["price"]) * qty

            # create order row first
            cur = conn.execute(
                "INSERT INTO orders(user_id, username, product_id, qty, amount, note, status, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?, 'PENDING', ?, ?)",
                (u.id, u.username or "", pid, qty, amount, note, now_str(), now_str())
            )
            oid = cur.lastrowid
            ref = gen_reference_id(u.id, oid)
            conn.execute("UPDATE orders SET reference_id=?, updated_at=? WHERE id=?", (ref, now_str(), oid))

        # create payment to iPaymu (QRIS)
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                resp = await ipaymu_create_qris_redirect_payment(
                    session,
                    reference_id=ref,
                    product_name=p["name"],
                    qty=qty,
                    price=int(p["price"]),
                    buyer_name=u.first_name or "Buyer",
                    buyer_phone="",   # opsional: kalau mau minta user isi, bikin flow tambahan
                    buyer_email="",
                )
        except Exception as e:
            log.exception("ipaymu create payment failed")
            with db() as conn:
                conn.execute("UPDATE orders SET status='CANCELLED', updated_at=? WHERE reference_id=?", (now_str(), ref))
            return await update.message.reply_text(f"Gagal bikin QRIS iPaymu: {e}")

        # parse response
        pay_url = ""
        sid = ""
        if isinstance(resp, dict) and resp.get("Status") == 200:
            data = resp.get("Data") or {}
            pay_url = data.get("Url", "") or data.get("UrlPayment", "") or ""
            sid = data.get("SessionID", "") or data.get("SessionId", "") or ""
        else:
            with db() as conn:
                conn.execute("UPDATE orders SET status='CANCELLED', updated_at=? WHERE reference_id=?", (now_str(), ref))
            return await update.message.reply_text(f"iPaymu balas error: {resp}")

        with db() as conn:
            conn.execute(
                "UPDATE orders SET status='PAYMENT_CREATED', ipaymu_sid=?, pay_url=?, updated_at=? WHERE reference_id=?",
                (sid, pay_url, now_str(), ref)
            )

        msg = (
            f"Invoice dibuat.\n\n"
            f"Order: *#{oid}*\n"
            f"Produk: *{p['name']}* x{qty}\n"
            f"Total: *{rupiah(amount)}*\n"
            f"Ref: `{ref}`\n\n"
            f"Bayar QRIS di link ini:\n{pay_url}\n\n"
            "Kalau sudah bayar, status akan otomatis ke-update."
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main(is_admin(u.id)))

# =========================
# Callback Receiver (iPaymu notifyUrl)
# =========================
async def ipaymu_notify(request: web.Request) -> web.Response:
    """
    iPaymu kirim POST form-data (umumnya) berisi:
      trx_id, status, status_code, sid, reference_id, ...
    """
    app = request.app
    bot_app: Application = app["bot_app"]

    data = await request.post()
    trx_id = str(data.get("trx_id", "") or "")
    status = str(data.get("status", "") or "")
    status_code = str(data.get("status_code", "") or "")
    sid = str(data.get("sid", "") or "")
    reference_id = str(data.get("reference_id", "") or "")

    if not reference_id:
        return web.json_response({"ok": False, "error": "missing reference_id"}, status=400)

    # idempotent update: kalau sudah PAID/DONE jangan diubah-ubah
    with db() as conn:
        row = conn.execute("SELECT * FROM orders WHERE reference_id=?", (reference_id,)).fetchone()
        if not row:
            return web.json_response({"ok": False, "error": "order not found"}, status=404)

        current = row["status"]
        new_status = current

        # mapping status_code: 0 pending, 1 berhasil, -2 expired (sesuai doc Postman)
        if status_code == "1" or status.lower() == "berhasil":
            if current not in ("PAID", "DONE"):
                new_status = "PAID"
        elif status_code == "-2" or status.lower() == "expired":
            if current not in ("PAID", "DONE"):
                new_status = "EXPIRED"
        else:
            # pending atau lainnya
            if current == "PAYMENT_CREATED":
                new_status = "PAYMENT_CREATED"

        if new_status != current:
            conn.execute(
                "UPDATE orders SET status=?, ipaymu_trx_id=?, ipaymu_sid=?, updated_at=? WHERE reference_id=?",
                (new_status, trx_id, sid, now_str(), reference_id)
            )

    # notify user + admin kalau PAID
    if new_status == "PAID":
        user_id = int(row["user_id"])
        oid = int(row["id"])
        amount = int(row["amount"])
        await bot_app.bot.send_message(
            chat_id=user_id,
            text=(
                f"Pembayaran *berhasil*.\n"
                f"Order *#{oid}* ({rupiah(amount)}) sekarang *PAID*.\n"
                "Tunggu admin deliver ya."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        for adm in ADMIN_IDS:
            await bot_app.bot.send_message(
                chat_id=adm,
                text=(
                    f"✅ *PAID*\n"
                    f"Order *#{oid}* — user `{user_id}`\n"
                    f"Ref: `{reference_id}`\n"
                    f"trx_id: `{trx_id}`\n"
                    f"Total: *{rupiah(amount)}*"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )

    return web.json_response({"ok": True})

async def start_callback_server(bot_app: Application) -> web.AppRunner:
    app = web.Application()
    app["bot_app"] = bot_app
    app.router.add_post(NOTIFY_PATH, ipaymu_notify)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=BIND_HOST, port=BIND_PORT)
    await site.start()
    log.info("Callback server listening on http://%s:%s%s", BIND_HOST, BIND_PORT, NOTIFY_PATH)
    return runner

# =========================
# MAIN
# =========================
async def post_init(app: Application):
    # start aiohttp server in same event loop
    app.bot_data["notify_runner"] = await start_callback_server(app)

async def post_shutdown(app: Application):
    runner: web.AppRunner = app.bot_data.get("notify_runner")
    if runner:
        await runner.cleanup()

def main():
    init_db()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("addprod", addprod_cmd))
    application.add_handler(CommandHandler("setprod", setprod_cmd))
    application.add_handler(CommandHandler("delprod", delprod_cmd))

    application.add_handler(CallbackQueryHandler(cb_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))

    log.info("Bot running. NotifyUrl=%s", NOTIFY_URL)
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
