import os
import re
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

# Biaya admin per order (flat)
ADMIN_FEE = int(os.getenv("ADMIN_FEE", "200"))

# Manual payment info
DANA_NUMBER = os.getenv("DANA_NUMBER", "").strip()
DANA_NAME = os.getenv("DANA_NAME", "").strip()

BANK_NAME = os.getenv("BANK_NAME", "").strip()
BANK_ACCOUNT = os.getenv("BANK_ACCOUNT", "").strip()
BANK_HOLDER = os.getenv("BANK_HOLDER", "").strip()

# QRIS photo (choose one)
QRIS_FILE_ID = os.getenv("QRIS_FILE_ID", "").strip()
QRIS_LOCAL_PATH = os.getenv("QRIS_LOCAL_PATH", "qris.jpg").strip()

# Testimoni channel
TESTI_CHANNEL_ID = os.getenv("TESTI_CHANNEL_ID", "").strip()  # @username or -100xxxx
TESTI_CONTACT = os.getenv("TESTI_CONTACT", "@Jdiginibebot").strip()

# Pagination
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "6"))

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
        [InlineKeyboardButton("⭐ Testimoni", callback_data="testi_help")],
    ]
    if admin:
        rows.append([InlineKeyboardButton("⚙️ Admin: Produk", callback_data="adm_products")])
        rows.append([InlineKeyboardButton("🧾 Admin: Order", callback_data="adm_orders")])
    return InlineKeyboardMarkup(rows)

def kb_products_paged(items: List[sqlite3.Row], page: int) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = items[start:end]

    rows = []
    for p in page_items:
        rows.append([
            InlineKeyboardButton(
                f"{p['name']} • {rupiah(p['price'])}",
                callback_data=f"buy_{p['id']}"
            )
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⏮ Sebelumnya", callback_data=f"cat_{page-1}"))
    if end < len(items):
        nav.append(InlineKeyboardButton("⏭ Selanjutnya", callback_data=f"cat_{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    return InlineKeyboardMarkup(rows)

def kb_admin_products(items: List[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("ℹ️ Format Bulk Add", callback_data="adm_help_add")]]
    for p in items[:30]:
        status = "ON" if p["active"] else "OFF"
        rows.append([InlineKeyboardButton(f"#{p['id']} {p['name']} ({status})", callback_data="noop")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    return InlineKeyboardMarkup(rows)

def kb_admin_order_actions(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"adm_appr_{order_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"adm_rej_{order_id}"),
    ]])

def kb_qty() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1️⃣", callback_data="qty_1"),
            InlineKeyboardButton("2️⃣", callback_data="qty_2"),
            InlineKeyboardButton("3️⃣", callback_data="qty_3"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="cat")]
    ])

# =========================
# HELPERS
# =========================
def payment_instructions(amount: int, order_id: int) -> str:
    return "\n".join([
        "Silakan lakukan pembayaran:",
        "",
        f"💰 *Total Bayar: {rupiah(amount)}*",
        f"🧾 (Termasuk biaya admin {rupiah(ADMIN_FEE)})",
        f"Order ID: `#{order_id}`",
        "",
        "Metode:",
        "• DANA",
        "• Transfer Bank",
        "• QRIS",
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
    ])

async def send_qris(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
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

def format_testimoni_card(barang: str, harga_rp: str, kontak: str) -> str:
    barang = (barang or "-").strip().upper()
    harga_rp = (harga_rp or "-").strip()
    kontak = (kontak or "-").strip()

    return (
        "╔══✦•··········•✦══╗\n"
        "        💥  T E S T I M O N I  💥\n"
        "╚══✦•··········•✦══╝\n\n"
        "         TRANSAKSI BERHASIL\n\n"
        f"🛍  BARANG : {barang}\n"
        f"💰  HARGA  : {harga_rp}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "✨  ALL TRANSAKSI SELESAI  ✨\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📞  HUBUNGI KAMI\n"
        f"➤ {kontak}"
    )

# =========================
# COMMANDS
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    admin = is_admin(u.id)

    text = (
        "📦 *AUTO ORDER BOT*\n\n"
        "🛒 *Cara Order*\n"
        "1) Buka Katalog\n"
        "2) Pilih produk\n"
        "3) Pilih jumlah (1/2/3)\n"
        "4) Bayar (DANA / BANK / QRIS)\n"
        "5) Kirim bukti\n\n"
        f"🧾 Biaya admin per order: *{rupiah(ADMIN_FEE)}*\n\n"
        "👤 *USER CMD*\n"
        "/start - buka menu\n"
        "/confirm <id> - konfirmasi order\n"
        "/testi <id> - kirim testimoni\n"
    )

    if admin:
        text += (
            "\n👑 *ADMIN CMD*\n"
            "/addprod - tambah produk (list)\n"
            "/setprod - edit produk\n"
            "/delprod - hapus produk\n"
            "/setqris - ambil QRIS_FILE_ID\n"
        )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_main(admin),
    )

# Bulk /addprod list mode (ADMIN ONLY)
async def addprod_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        return await update.message.reply_text("Khusus admin.")

    lines = (update.message.text or "").splitlines()
    if len(lines) <= 1:
        return await update.message.reply_text(
            "Format bulk:\n"
            "/addprod\n"
            "Nama Produk | harga | catatan(optional)\n"
            "Nama Produk | harga\n\n"
            "Contoh:\n"
            "/addprod\n"
            "UBOT 1 BULAN | 20000 | garansi 7 hari\n"
            "PREMIUM TELE | 35000"
        )

    success, failed = [], []

    with db() as conn:
        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue

            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 2:
                failed.append(f"❌ {line} (format salah)")
                continue

            name = parts[0]
            price_raw = parts[1].replace(".", "").replace(",", "").strip()
            note = parts[2] if len(parts) >= 3 else ""

            if not name:
                failed.append(f"❌ {line} (nama kosong)")
                continue
            if not price_raw.isdigit():
                failed.append(f"❌ {line} (harga invalid)")
                continue

            price = int(price_raw)
            cur = conn.execute(
                "INSERT INTO products(name, price, active, note) VALUES(?,?,1,?)",
                (name, price, note),
            )
            pid = cur.lastrowid
            success.append(f"✅ #{pid} {name} ({rupiah(price)})")

    msg = []
    if success:
        msg.append("🟢 *Produk berhasil ditambahkan:*")
        msg.extend(success)
    if failed:
        msg.append("\n🔴 *Gagal ditambahkan:*")
        msg.extend(failed)

    await update.message.reply_text("\n".join(msg), parse_mode=ParseMode.MARKDOWN)

async def setprod_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        return await update.message.reply_text("Khusus admin.")

    raw = update.message.text.replace("/setprod", "", 1).strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 3:
        return await update.message.reply_text(
            "Format:\n"
            "/setprod ID | Nama | harga | active=1/0(optional) | catatan(optional)"
        )

    if not parts[0].isdigit():
        return await update.message.reply_text("ID harus angka.")
    pid = int(parts[0])

    name = parts[1]
    price_raw = parts[2].replace(".", "").replace(",", "").strip()
    if not price_raw.isdigit():
        return await update.message.reply_text("Harga harus angka.")
    price = int(price_raw)

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

    parts = (update.message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await update.message.reply_text("Format: /delprod ID")

    pid = int(parts[1])
    with db() as conn:
        conn.execute("DELETE FROM products WHERE id=?", (pid,))
    await update.message.reply_text(f"OK. Produk #{pid} dihapus.")

async def setqris_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def testi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user

    if not TESTI_CHANNEL_ID:
        return await update.message.reply_text("TESTI_CHANNEL_ID belum diset di .env, atau bot belum admin channel.")

    if not context.args:
        return await update.message.reply_text("Format: /testi <order_id> (contoh: /testi 12)")

    raw = context.args[0].strip()
    raw = raw[1:] if raw.startswith("#") else raw
    if not raw.isdigit():
        return await update.message.reply_text("Order ID harus angka. Contoh: /testi 12")

    oid = int(raw)

    with db() as conn:
        row = conn.execute(
            "SELECT o.*, p.name AS product_name "
            "FROM orders o JOIN products p ON p.id=o.product_id "
            "WHERE o.id=?",
            (oid,),
        ).fetchone()

    if not row:
        return await update.message.reply_text("Order tidak ditemukan.")

    if not is_admin(u.id):
        if int(row["user_id"]) != u.id:
            return await update.message.reply_text("Itu bukan order kamu.")
        if row["status"] not in ("PAID", "DONE"):
            return await update.message.reply_text(
                f"Order #{oid} statusnya masih *{row['status']}*.\n"
                "Testimoni hanya bisa kalau sudah PAID/DONE.",
                parse_mode=ParseMode.MARKDOWN,
            )

    caption = format_testimoni_card(
        barang=row["product_name"],
        harga_rp=rupiah(int(row["amount"])),
        kontak=TESTI_CONTACT,
    )

    await context.bot.send_message(chat_id=TESTI_CHANNEL_ID, text=caption)
    await update.message.reply_text(
        f"Berhasil. Testimoni order *#{oid}* sudah di-upload ke channel.",
        parse_mode=ParseMode.MARKDOWN,
    )

async def confirm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not context.args:
        return await update.message.reply_text("Format: /confirm <order_id>  (contoh: /confirm 12)")

    raw = context.args[0].strip()
    raw = raw[1:] if raw.startswith("#") else raw
    if not raw.isdigit():
        return await update.message.reply_text("Order ID harus angka. Contoh: /confirm 12")

    oid = int(raw)
    with db() as conn:
        row = conn.execute("SELECT id FROM orders WHERE id=? AND user_id=?", (oid, u.id)).fetchone()
    if not row:
        return await update.message.reply_text("Order tidak ditemukan (atau bukan punyamu).")

    context.user_data["await_proof_order_id"] = oid
    await update.message.reply_text(f"OK. Kirim foto bukti untuk order #{oid} sekarang.")

# =========================
# CALLBACKS
# =========================
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = update.effective_user
    data = q.data

    if data == "home":
        return await q.edit_message_text("Menu:", reply_markup=kb_main(is_admin(u.id)))

    # Katalog page 0
    if data == "cat":
        with db() as conn:
            items = conn.execute("SELECT * FROM products WHERE active=1 ORDER BY id ASC").fetchall()
        if not items:
            return await q.edit_message_text("Belum ada produk aktif.", reply_markup=kb_main(is_admin(u.id)))
        return await q.edit_message_text(
            "🛒 *Katalog Produk*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_products_paged(items, page=0),
        )

    # Katalog pagination
    if data.startswith("cat_"):
        page = int(data.split("_")[1])
        with db() as conn:
            items = conn.execute("SELECT * FROM products WHERE active=1 ORDER BY id ASC").fetchall()
        return await q.edit_message_text(
            "🛒 *Katalog Produk*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_products_paged(items, page=page),
        )

    # Buy: choose qty
    if data.startswith("buy_"):
        pid = int(data.split("_", 1)[1])
        with db() as conn:
            p = conn.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if not p:
            return await q.edit_message_text("Produk tidak tersedia.", reply_markup=kb_main(is_admin(u.id)))

        context.user_data["buy_pid"] = pid
        context.user_data["buy_price"] = int(p["price"])

        return await q.edit_message_text(
            f"🛍 *{p['name']}*\n"
            f"Harga satuan: *{rupiah(int(p['price']))}*\n\n"
            "Pilih jumlah (1/2/3):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_qty(),
        )

    # Qty selected -> show totals and ask note
    if data.startswith("qty_"):
        qty = int(data.split("_")[1])
        price = int(context.user_data.get("buy_price", 0))

        subtotal = price * qty
        total = subtotal + ADMIN_FEE

        context.user_data["pending_qty"] = qty

        return await q.edit_message_text(
            f"📦 Jumlah: *{qty}*\n"
            f"💵 Subtotal: *{rupiah(subtotal)}*\n"
            f"🧾 Biaya Admin: *{rupiah(ADMIN_FEE)}*\n"
            f"💰 Total Bayar: *{rupiah(total)}*\n\n"
            "Balas dengan catatan (opsional), atau ketik `-` jika kosong.",
            parse_mode=ParseMode.MARKDOWN,
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
        lines.append("")
        lines.append("Buat testimoni: /testi <order_id>  (contoh: /testi 12)")
        return await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main(is_admin(u.id)))

    if data == "testi_help":
        msg = (
            "⭐ *Testimoni*\n\n"
            "Format:\n"
            "`/testi <order_id>`\n"
            "Contoh: `/testi 12`\n\n"
            "Syarat:\n"
            "• Order kamu harus status *PAID* atau *DONE*.\n"
            "• Nanti bot auto upload ke channel testimoni."
        )
        return await q.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main(is_admin(u.id)))

    if data == "adm_products":
        if not is_admin(u.id):
            return await q.edit_message_text("Khusus admin.", reply_markup=kb_main(False))
        with db() as conn:
            items = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
        return await q.edit_message_text(
            "Admin Produk:\n\n"
            "Cmd bulk add:\n"
            "`/addprod` lalu isi list per baris:\n"
            "`Nama | harga | catatan(optional)`\n\n"
            "Cmd lain:\n"
            "`/setprod ID | Nama | harga | active=1/0 | catatan`\n"
            "`/delprod ID`\n\n"
            "QRIS:\n"
            "Reply foto QRIS lalu `/setqris` untuk ambil QRIS_FILE_ID.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_admin_products(items),
        )

    if data == "adm_help_add":
        return await q.edit_message_text(
            "Bulk add produk:\n\n"
            "/addprod\n"
            "UBOT 1 BULAN | 20000 | garansi 7 hari\n"
            "UBOT 3 BULAN | 55000\n"
            "PREMIUM TELE | 35000 | fast approve\n\n"
            "Harga boleh: 20000 / 20.000 / 20,000",
            reply_markup=kb_main(True),
        )

    if data == "noop":
        return

# =========================
# MESSAGE HANDLERS
# =========================
async def msg_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This text handler is used for: note input after qty selection
    u = update.effective_user
    text = (update.message.text or "").strip()

    # if user is in "note entry" stage
    if "buy_pid" in context.user_data and "pending_qty" in context.user_data:
        pid = int(context.user_data.pop("buy_pid"))
        qty = int(context.user_data.pop("pending_qty"))
        note = "" if text == "-" else text

        with db() as conn:
            p = conn.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
            if not p:
                return await update.message.reply_text("Produk sudah tidak tersedia.")

            subtotal = int(p["price"]) * qty
            amount = subtotal + ADMIN_FEE  # include fee

            cur = conn.execute(
                "INSERT INTO orders(user_id, username, product_id, qty, amount, note, status, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?, 'WAITING_PAYMENT', ?, ?)",
                (u.id, u.username or "", pid, qty, amount, note, now_str(), now_str()),
            )
            oid = cur.lastrowid

        context.user_data["await_proof_order_id"] = oid

        await update.message.reply_text(
            f"✅ Order dibuat.\n\n"
            f"Order ID: *#{oid}*\n"
            f"Produk: *{p['name']}* x{qty}\n"
            f"Subtotal: *{rupiah(subtotal)}*\n"
            f"Biaya Admin: *{rupiah(ADMIN_FEE)}*\n"
            f"Total: *{rupiah(amount)}*\n\n"
            + payment_instructions(amount, oid),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_main(is_admin(u.id)),
        )
        await send_qris(update.effective_chat.id, context)
        return

    # Otherwise ignore non-command text
    return

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not update.message.photo:
        return

    file_id = update.message.photo[-1].file_id
    caption = (update.message.caption or "").strip()

    order_id: Optional[int] = None
    if caption:
        m = re.search(r"#(\d+)", caption)
        if m:
            order_id = int(m.group(1))
    if not order_id:
        order_id = context.user_data.get("await_proof_order_id")

    if not order_id:
        return await update.message.reply_text(
            "Aku butuh order_id biar bukti gak nyasar.\n"
            "Kirim ulang fotonya pakai caption `#<order_id>` (contoh: `#12`), atau pakai `/confirm <id>` dulu."
        )

    with db() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, u.id)).fetchone()
        if not row:
            return await update.message.reply_text("Order tidak ditemukan (atau bukan punyamu).")

        conn.execute(
            "UPDATE orders SET proof_file_id=?, proof_caption=?, status='PROOF_SUBMITTED', updated_at=? WHERE id=?",
            (file_id, caption, now_str(), order_id),
        )

    await update.message.reply_text(
        f"OK. Bukti untuk order *#{order_id}* sudah masuk.\nTunggu admin verifikasi.",
        parse_mode=ParseMode.MARKDOWN,
    )

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
        f"Total: *{rupiah(int(info['amount']))}* (incl admin {rupiah(ADMIN_FEE)})\n"
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

async def confirm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not context.args:
        return await update.message.reply_text("Format: /confirm <order_id>  (contoh: /confirm 12)")

    raw = context.args[0].strip()
    raw = raw[1:] if raw.startswith("#") else raw
    if not raw.isdigit():
        return await update.message.reply_text("Order ID harus angka. Contoh: /confirm 12")

    oid = int(raw)
    with db() as conn:
        row = conn.execute("SELECT id FROM orders WHERE id=? AND user_id=?", (oid, u.id)).fetchone()
    if not row:
        return await update.message.reply_text("Order tidak ditemukan (atau bukan punyamu).")

    context.user_data["await_proof_order_id"] = oid
    await update.message.reply_text(f"OK. Kirim foto bukti untuk order #{oid} sekarang.")

# =========================
# MAIN
# =========================
def main():
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("addprod", addprod_cmd))   # bulk list add
    app.add_handler(CommandHandler("setprod", setprod_cmd))
    app.add_handler(CommandHandler("delprod", delprod_cmd))
    app.add_handler(CommandHandler("setqris", setqris_cmd))
    app.add_handler(CommandHandler("testi", testi_cmd))
    app.add_handler(CommandHandler("confirm", confirm_cmd))

    # callbacks + messages
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_text_handler))

    log.info("Bot running. DB=%s", DB_PATH)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
