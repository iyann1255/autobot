import os
import sqlite3
import logging
from datetime import datetime
from typing import List, Optional

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG
# =========================
load_dotenv()
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("auto-order-manual")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN belum diisi.")

DB_PATH = os.getenv("DB_PATH", "data.sqlite").strip()

ADMIN_IDS = set()
for x in os.getenv("ADMIN_IDS", "").split(","):
    x = x.strip()
    if x.isdigit():
        ADMIN_IDS.add(int(x))

# Payment info
DANA_NUMBER = os.getenv("DANA_NUMBER", "").strip()
DANA_NAME = os.getenv("DANA_NAME", "").strip()

BANK_NAME = os.getenv("BANK_NAME", "").strip()
BANK_ACCOUNT = os.getenv("BANK_ACCOUNT", "").strip()
BANK_HOLDER = os.getenv("BANK_HOLDER", "").strip()

QRIS_FILE_ID = os.getenv("QRIS_FILE_ID", "").strip()
QRIS_LOCAL_PATH = os.getenv("QRIS_LOCAL_PATH", "qris.jpg").strip()  # optional local file

# =========================
# DB
# =========================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def init_db() -> None:
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
            status TEXT NOT NULL DEFAULT 'WAITING_PAYMENT',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,

            proof_file_id TEXT DEFAULT '',
            proof_caption TEXT DEFAULT '',
            admin_note TEXT DEFAULT ''
        );
        """)

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def rupiah(n: int) -> str:
    return f"Rp{n:,}".replace(",", ".")

# =========================
# UI
# =========================
def kb_main(admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🛒 Katalog", callback_data="cat")],
        [InlineKeyboardButton("📦 Order Saya", callback_data="my")],
    ]
    if admin:
        rows.append([InlineKeyboardButton("⚙️ Admin: Produk", callback_data="adm_products")])
        rows.append([InlineKeyboardButton("🧾 Admin: Order", callback_data="adm_orders")])
    return InlineKeyboardMarkup(rows)

def kb_products(items: List[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = []
    for p in items[:30]:
        rows.append([InlineKeyboardButton(f"{p['name']} • {rupiah(p['price'])}", callback_data=f"buy_{p['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    return InlineKeyboardMarkup(rows)

def kb_admin_products(items: List[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("➕ Tambah Produk", callback_data="adm_help_add")],
    ]
    for p in items[:30]:
        status = "ON" if p["active"] else "OFF"
        rows.append([InlineKeyboardButton(f"#{p['id']} {p['name']} ({status})", callback_data="noop")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    return InlineKeyboardMarkup(rows)

def kb_admin_order_actions(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"adm_appr_{order_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"adm_rej_{order_id}"),
        ]
    ])

# =========================
# TEXT HELPERS
# =========================
def payment_instructions(amount: int, order_id: int) -> str:
    lines = [
        "Silakan bayar sesuai metode yang kamu pilih:",
        "",
        f"Total: *{rupiah(amount)}*",
        f"Order ID: `#{order_id}`",
        "",
        "1) *DANA*",
        f"• No: `{DANA_NUMBER or '-'}'",
        f"• Nama: *{DANA_NAME or '-'}*",
        "",
        "2) *Transfer Bank*",
        f"• Bank: *{BANK_NAME or '-'}*",
        f"• Rek: `{BANK_ACCOUNT or '-'}'",
        f"• A/N: *{BANK_HOLDER or '-'}*",
        "",
        "3) *QRIS*",
        "• Scan QRIS dari foto yang aku kirim.",
        "",
        "Setelah bayar, *kirim bukti bayar (foto)* ke bot ini.",
        "Biar gak nyasar, kasih caption: `#<order_id>` contoh: `#12`.",
    ]
    return "\n".join(lines)

async def send_qris(update_or_chat, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Tries:
    1) sendPhoto by file_id (recommended)
    2) sendPhoto by local file (qris.jpg)
    If none available, skip silently.
    """
    chat_id = update_or_chat if isinstance(update_or_chat, int) else update_or_chat.effective_chat.id

    try:
        if QRIS_FILE_ID:
            await context.bot.send_photo(chat_id=chat_id, photo=QRIS_FILE_ID, caption="QRIS (scan untuk bayar)")
            return
        if os.path.exists(QRIS_LOCAL_PATH):
            with open(QRIS_LOCAL_PATH, "rb") as f:
                await context.bot.send_photo(chat_id=chat_id, photo=f, caption="QRIS (scan untuk bayar)")
                return
    except Exception:
        log.exception("Failed to send QRIS photo")

# =========================
# COMMANDS (Admin)
# =========================
# /addprod Nama | 20000 | catatan
# /setprod ID | Nama | 25000 | active=1 | catatan
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
    if not parts[1].isdigit():
        return await update.message.reply_text("Harga harus angka (tanpa titik).")
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

# Optional helper: set QRIS file_id easily
async def setqris_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin can reply to a QRIS photo and run /setqris to print file_id to save in .env
    """
    u = update.effective_user
    if not is_admin(u.id):
        return await update.message.reply_text("Khusus admin.")
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        return await update.message.reply_text("Reply ke foto QRIS dulu, lalu ketik /setqris")
    file_id = update.message.reply_to_message.photo[-1].file_id
    await update.message.reply_text(
        f"Ini QRIS_FILE_ID:\n`{file_id}`\n\nSimpan ke .env lalu restart bot.",
        parse_mode=ParseMode.MARKDOWN,
    )

# =========================
# USER FLOWS
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(
        "Auto Order aktif.\nPilih produk di Katalog, bayar manual, kirim bukti. Beres.",
        reply_markup=kb_main(is_admin(u.id)),
    )

async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = update.effective_user
    data = q.data

    if data == "home":
        return await q.edit_message_text("Menu:", reply_markup=kb_main(is_admin(u.id)))

    if data == "cat":
        with db() as conn:
            items = conn.execute("SELECT * FROM products WHERE active=1 ORDER BY id DESC").fetchall()
        if not items:
            return await q.edit_message_text("Belum ada produk aktif.", reply_markup=kb_main(is_admin(u.id)))
        return await q.edit_message_text("Katalog (klik untuk order):", reply_markup=kb_products(items))

    if data.startswith("buy_"):
        pid = int(data.split("_", 1)[1])
        with db() as conn:
            p = conn.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if not p:
            return await q.edit_message_text("Produk tidak tersedia.", reply_markup=kb_main(is_admin(u.id)))

        context.user_data["pending_pid"] = pid
        return await q.edit_message_text(
            f"Produk: *{p['name']}*\nHarga: *{rupiah(p['price'])}*\n\n"
            "Balas chat ini dengan format:\n"
            "`qty | catatan`\n"
            "Contoh: `1 | ubot 1 bulan (@username)`\n\n"
            "Atau ketik `cancel` buat batal.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="cat")]]),
        )

    if data == "my":
        with db() as conn:
            rows = conn.execute(
                "SELECT o.*, p.name AS product_name FROM orders o JOIN products p ON p.id=o.product_id "
                "WHERE o.user_id=? ORDER BY o.id DESC LIMIT 10",
                (u.id,),
            ).fetchall()
        if not rows:
            return await q.edit_message_text("Belum ada order.", reply_markup=kb_main(is_admin(u.id)))

        lines = []
        for r in rows:
            lines.append(f"• #{r['id']} {r['product_name']} x{r['qty']} — *{r['status']}* — {rupiah(r['amount'])}")
        return await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main(is_admin(u.id)))

    if data == "adm_products":
        if not is_admin(u.id):
            return await q.edit_message_text("Khusus admin.", reply_markup=kb_main(False))
        with db() as conn:
            items = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
        return await q.edit_message_text(
            "Admin Produk:\n\n"
            "Cmd:\n"
            "`/addprod Nama | harga | catatan`\n"
            "`/setprod ID | Nama | harga | active=1/0 | catatan`\n"
            "`/delprod ID`\n\n"
            "QRIS:\n"
            "Reply foto QRIS lalu `/setqris` untuk ambil QRIS_FILE_ID.",
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

        lines = ["Order terbaru (klik approve/reject dari bukti yang masuk):"]
        lines.append("")
        for r in rows:
            lines.append(f"• #{r['id']} @{r['username'] or r['user_id']} — {r['product_name']} x{r['qty']} — {r['status']}")
        lines.append("")
        lines.append("Tip: approve/reject paling enak dari pesan bukti bayar yang bot forward ke admin.")
        return await q.edit_message_text("\n".join(lines), reply_markup=kb_main(True))

    if data.startswith("adm_appr_") or data.startswith("adm_rej_"):
        if not is_admin(u.id):
            return
        action, oid = data.rsplit("_", 1)
        order_id = int(oid)

        with db() as conn:
            row = conn.execute(
                "SELECT o.*, p.name AS product_name FROM orders o JOIN products p ON p.id=o.product_id WHERE o.id=?",
                (order_id,),
            ).fetchone()
        if not row:
            return await q.edit_message_text("Order tidak ditemukan.")

        if action.startswith("adm_appr"):
            new_status = "PAID"
            admin_note = "Approved"
            user_msg = (
                f"✅ Pembayaran kamu *diterima*.\n"
                f"Order *#{order_id}* sekarang *PAID*.\n"
                "Admin bakal deliver barangnya. Tenang, ini bukan ghosting."
            )
        else:
            new_status = "REJECTED"
            admin_note = "Rejected"
            user_msg = (
                f"❌ Bukti bayar *ditolak*.\n"
                f"Order *#{order_id}* status: *REJECTED*.\n"
                "Silakan kirim ulang bukti yang jelas (atau chat admin)."
            )

        with db() as conn:
            conn.execute(
                "UPDATE orders SET status=?, admin_note=?, updated_at=? WHERE id=?",
                (new_status, admin_note, now_str(), order_id),
            )

        # notify user
        try:
            await context.bot.send_message(chat_id=int(row["user_id"]), text=user_msg, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            log.exception("Failed to notify user")

        return await q.edit_message_text(
            f"OK. Order #{order_id} => {new_status}\nUser: {row['user_id']}\nProduk: {row['product_name']}",
            reply_markup=kb_main(True),
        )

    if data == "noop":
        return

async def msg_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    text = (update.message.text or "").strip()

    # bind proof target quickly: /confirm 12
    if text.lower().startswith("/confirm"):
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            return await update.message.reply_text("Format: /confirm <order_id>")
        oid = int(parts[1])
        with db() as conn:
            row = conn.execute("SELECT id FROM orders WHERE id=? AND user_id=?", (oid, u.id)).fetchone()
        if not row:
            return await update.message.reply_text("Order tidak ditemukan (atau bukan punyamu).")
        context.user_data["await_proof_order_id"] = oid
        return await update.message.reply_text(f"OK. Kirim foto bukti untuk order #{oid} sekarang.")

    # create order after selecting product
    if "pending_pid" in context.user_data:
        if text.lower() == "cancel":
            context.user_data.pop("pending_pid", None)
            return await update.message.reply_text("Batal. Balik ke menu.", reply_markup=kb_main(is_admin(u.id)))

        parts = [p.strip() for p in text.split("|", 1)]
        if not parts[0].isdigit():
            return await update.message.reply_text("Format salah. Contoh: `1 | ubot 1 bulan @username`", parse_mode=ParseMode.MARKDOWN)
        qty = int(parts[0])
        note = parts[1] if len(parts) > 1 else ""

        pid = int(context.user_data.pop("pending_pid"))

        with db() as conn:
            p = conn.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
            if not p:
                return await update.message.reply_text("Produk sudah tidak tersedia.")
            amount = int(p["price"]) * qty
            cur = conn.execute(
                "INSERT INTO orders(user_id, username, product_id, qty, amount, note, status, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?, 'WAITING_PAYMENT', ?, ?)",
                (u.id, u.username or "", pid, qty, amount, note, now_str(), now_str()),
            )
            oid = cur.lastrowid

        # set awaiting proof
        context.user_data["await_proof_order_id"] = oid

        await update.message.reply_text(
            f"Order dibuat.\n\n"
            f"Order ID: *#{oid}*\n"
            f"Produk: *{p['name']}* x{qty}\n"
            f"Total: *{rupiah(amount)}*\n\n"
            + payment_instructions(amount, oid),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_main(is_admin(u.id)),
        )

        # send QRIS photo (if available)
        await send_qris(update, context)
        return

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not update.message.photo:
        return

    file_id = update.message.photo[-1].file_id
    caption = (update.message.caption or "").strip()

    # Determine order id: prefer caption "#12", else user_data awaiting
    order_id: Optional[int] = None
    m = None
    if caption:
        m = __import__("re").search(r"#(\d+)", caption)
    if m:
        order_id = int(m.group(1))
    else:
        order_id = context.user_data.get("await_proof_order_id")

    if not order_id:
        return await update.message.reply_text(
            "Aku butuh order_id biar bukti gak nyasar.\n"
            "Kirim ulang fotonya pakai caption `#<order_id>` (contoh: `#12`), atau ketik `/confirm 12` dulu."
        )

    with db() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, u.id)).fetchone()
        if not row:
            return await update.message.reply_text("Order tidak ditemukan (atau bukan punyamu).")
        # save proof
        conn.execute(
            "UPDATE orders SET proof_file_id=?, proof_caption=?, status='PROOF_SUBMITTED', updated_at=? WHERE id=?",
            (file_id, caption, now_str(), order_id),
        )

    # notify user
    await update.message.reply_text(
        f"OK. Bukti untuk order *#{order_id}* sudah masuk.\n"
        "Tunggu admin verifikasi.",
        parse_mode=ParseMode.MARKDOWN,
    )

    # forward proof to admins with action buttons
    with db() as conn:
        info = conn.execute(
            "SELECT o.*, p.name AS product_name FROM orders o JOIN products p ON p.id=o.product_id WHERE o.id=?",
            (order_id,),
        ).fetchone()

    admin_text = (
        f"🧾 *Bukti Bayar Masuk*\n"
        f"Order: *#{order_id}*\n"
        f"User: `{info['user_id']}` @{info['username'] or '-'}\n"
        f"Produk: *{info['product_name']}* x{info['qty']}\n"
        f"Total: *{rupiah(int(info['amount']))}*\n"
        f"Catatan: {info['note'] or '-'}\n"
        f"Caption: {caption or '-'}\n"
        f"Status: *{info['status']}*"
    )

    for adm in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                chat_id=adm,
                photo=file_id,
                caption=admin_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_admin_order_actions(order_id),
            )
        except Exception:
            log.exception("Failed to send proof to admin")

# =========================
# MAIN
# =========================
def main():
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))

    # admin product management
    app.add_handler(CommandHandler("addprod", addprod_cmd))
    app.add_handler(CommandHandler("setprod", setprod_cmd))
    app.add_handler(CommandHandler("delprod", delprod_cmd))
    app.add_handler(CommandHandler("setqris", setqris_cmd))

    # callbacks + messages
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_text_handler))

    log.info("Bot running (manual payment). DB=%s", DB_PATH)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
