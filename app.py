import os
import re
import sqlite3
import logging
from datetime import datetime, date
from typing import List, Optional, Tuple

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
from telegram.error import BadRequest

# =========================
# CONFIG
# =========================
load_dotenv()
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("auto-order-b")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN belum diisi.")

DB_PATH = os.getenv("DB_PATH", "data.sqlite").strip()

ADMIN_IDS = set()
for x in os.getenv("ADMIN_IDS", "").split(","):
    x = x.strip()
    if x.isdigit():
        ADMIN_IDS.add(int(x))

# Pagination katalog user
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "6"))

# Pagination admin produk
ADMIN_PROD_PAGE_SIZE = int(os.getenv("ADMIN_PROD_PAGE_SIZE", "8"))

# Payment data
DANA_NUMBER = os.getenv("DANA_NUMBER", "").strip()
DANA_NAME = os.getenv("DANA_NAME", "").strip()

BANK_NAME = os.getenv("BANK_NAME", "").strip()
BANK_ACCOUNT = os.getenv("BANK_ACCOUNT", "").strip()
BANK_HOLDER = os.getenv("BANK_HOLDER", "").strip()

QRIS_FILE_ID = os.getenv("QRIS_FILE_ID", "").strip()
QRIS_LOCAL_PATH = os.getenv("QRIS_LOCAL_PATH", "qris.jpg").strip()

# Fee per metode
FEE_DANA = int(os.getenv("FEE_DANA", "200"))
FEE_BANK = int(os.getenv("FEE_BANK", "200"))
FEE_QRIS = int(os.getenv("FEE_QRIS", "200"))

# Testimoni channel
TESTI_CHANNEL_ID = os.getenv("TESTI_CHANNEL_ID", "").strip()  # @username or -100xxxx
TESTI_CONTACT = os.getenv("TESTI_CONTACT", "@Jdiginibebot").strip()

# =========================
# DB
# =========================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def rupiah(n: int) -> str:
    return f"Rp{int(n):,}".replace(",", ".")

def _has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(c["name"] == col for c in cols)

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

        CREATE TABLE IF NOT EXISTS vouchers (
            code TEXT PRIMARY KEY,
            type TEXT NOT NULL,         -- 'fixed' or 'percent'
            value INTEGER NOT NULL,     -- fixed: rupiah; percent: 1-100
            max_uses INTEGER DEFAULT 0, -- 0 = unlimited
            used_count INTEGER DEFAULT 0,
            expires TEXT DEFAULT ''     -- 'YYYY-MM-DD' or empty
        );
        """)

        # migrate orders
        if not _has_column(conn, "orders", "payment_method"):
            conn.execute("ALTER TABLE orders ADD COLUMN payment_method TEXT DEFAULT ''")
        if not _has_column(conn, "orders", "fee"):
            conn.execute("ALTER TABLE orders ADD COLUMN fee INTEGER DEFAULT 0")
        if not _has_column(conn, "orders", "discount"):
            conn.execute("ALTER TABLE orders ADD COLUMN discount INTEGER DEFAULT 0")
        if not _has_column(conn, "orders", "voucher_code"):
            conn.execute("ALTER TABLE orders ADD COLUMN voucher_code TEXT DEFAULT ''")

        # migrate products
        if not _has_column(conn, "products", "requires_account"):
            conn.execute("ALTER TABLE products ADD COLUMN requires_account INTEGER NOT NULL DEFAULT 0")

# =========================
# ERROR HANDLER + SAFE EDIT
# =========================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled exception: %s", context.error)

async def safe_edit_text(q, text: str, *, parse_mode=None, reply_markup=None):
    """
    Safe edit for callback query message:
    - Ignore "Message is not modified"
    - Fallback send_message when edit isn't possible (e.g., message is media)
    """
    try:
        return await q.edit_message_text(text=text, parse_mode=parse_mode, reply_markup=reply_markup)
    except BadRequest as e:
        msg = str(e).lower()
        if "message is not modified" in msg:
            return
        chat_id = None
        try:
            if q.message:
                chat_id = q.message.chat_id
        except Exception:
            chat_id = None
        if chat_id is None:
            raise
        return await q.get_bot().send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)

# =========================
# VOUCHER LOGIC
# =========================
def parse_date_yyyy_mm_dd(s: str) -> Optional[date]:
    try:
        y, m, d = s.split("-")
        return date(int(y), int(m), int(d))
    except Exception:
        return None

def compute_discount(voucher_row: sqlite3.Row, subtotal: int) -> int:
    vtype = (voucher_row["type"] or "").lower()
    val = int(voucher_row["value"])
    if subtotal <= 0:
        return 0
    if vtype == "percent":
        val = max(0, min(100, val))
        return max(0, (subtotal * val) // 100)
    return max(0, min(subtotal, val))

def validate_voucher(code: str, subtotal: int) -> Tuple[bool, str, int, Optional[sqlite3.Row]]:
    code = (code or "").strip().upper()
    if not code:
        return False, "Kode voucher kosong.", 0, None

    with db() as conn:
        v = conn.execute("SELECT * FROM vouchers WHERE code=?", (code,)).fetchone()

    if not v:
        return False, "Voucher tidak ditemukan.", 0, None

    exp = (v["expires"] or "").strip()
    if exp:
        exp_d = parse_date_yyyy_mm_dd(exp)
        if not exp_d:
            return False, "Voucher invalid (format expiry rusak).", 0, None
        if date.today() > exp_d:
            return False, "Voucher sudah expired.", 0, None

    max_uses = int(v["max_uses"] or 0)
    used = int(v["used_count"] or 0)
    if max_uses > 0 and used >= max_uses:
        return False, "Voucher sudah habis kuota.", 0, None

    disc = compute_discount(v, subtotal)
    if disc <= 0:
        return False, "Voucher tidak berlaku untuk subtotal ini.", 0, None

    return True, "OK", disc, v

def increment_voucher_use(code: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE vouchers SET used_count = COALESCE(used_count,0) + 1 WHERE code=?",
            (code.strip().upper(),),
        )

# =========================
# PRODUCT ACCOUNT FLAG
# =========================
def product_requires_account(pid: int) -> bool:
    with db() as conn:
        r = conn.execute("SELECT requires_account FROM products WHERE id=?", (pid,)).fetchone()
    return bool(r and int(r["requires_account"]) == 1)

# =========================
# UI
# =========================
def kb_main(admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🛒 Katalog", callback_data="cat")],
        [InlineKeyboardButton("📦 Order Saya", callback_data="my")],
    ]
    if admin:
        rows.append([InlineKeyboardButton("⚙️ Admin: Produk", callback_data="adm_products_0")])
        rows.append([InlineKeyboardButton("🧾 Admin: Orders", callback_data="adm_orders")])
        rows.append([InlineKeyboardButton("🎟 Admin: Voucher", callback_data="adm_vouchers")])
    return InlineKeyboardMarkup(rows)

def kb_products_paged(items: List[sqlite3.Row], page: int) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = items[start:end]

    rows = []
    for p in page_items:
        rows.append([InlineKeyboardButton(f"{p['name']} • {rupiah(p['price'])}", callback_data=f"buy_{p['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⏮ Sebelumnya", callback_data=f"cat_{page-1}"))
    if end < len(items):
        nav.append(InlineKeyboardButton("⏭ Selanjutnya", callback_data=f"cat_{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("⬅️ Menu", callback_data="home")])
    return InlineKeyboardMarkup(rows)

def kb_qty_panel(qty: int) -> InlineKeyboardMarkup:
    qty = max(1, min(99, qty))
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➖", callback_data="qty_minus"),
            InlineKeyboardButton(f"Qty: {qty}", callback_data="noop"),
            InlineKeyboardButton("➕", callback_data="qty_plus"),
        ],
        [InlineKeyboardButton("✅ Lanjut", callback_data="qty_done")],
        [InlineKeyboardButton("⬅️ Katalog", callback_data="cat")],
    ])

def kb_account_skip() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Lewati", callback_data="acc_skip")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="back_qty")],
    ])

def kb_voucher_or_skip() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎟 Pakai Voucher", callback_data="use_voucher")],
        [InlineKeyboardButton("Lewati", callback_data="skip_voucher")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="back_qty")],
    ])

def kb_payment_methods() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("DANA", callback_data="pay_DANA"),
            InlineKeyboardButton("BANK", callback_data="pay_BANK"),
            InlineKeyboardButton("QRIS", callback_data="pay_QRIS"),
        ],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="back_voucher")],
    ])

def kb_admin_order_actions(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"adm_appr_{order_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"adm_rej_{order_id}"),
        InlineKeyboardButton("🏁 DONE", callback_data=f"adm_done_{order_id}"),
    ]])

def kb_admin_products(items: List[sqlite3.Row], page: int) -> InlineKeyboardMarkup:
    start = page * ADMIN_PROD_PAGE_SIZE
    end = start + ADMIN_PROD_PAGE_SIZE
    chunk = items[start:end]

    rows = []
    for p in chunk:
        status = "✅ READY" if int(p["active"]) == 1 else "⛔ NO READY"
        req = "🧾AKUN" if int(p["requires_account"]) == 1 else "—"
        pid = int(p["id"])
        rows.append([
            InlineKeyboardButton(f"{status} #{pid}", callback_data=f"adm_toggle_{pid}_{page}"),
            InlineKeyboardButton(f"{p['name']} ({req})", callback_data=f"adm_req_{pid}_{page}"),
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⏮", callback_data=f"adm_products_{page-1}"))
    if end < len(items):
        nav.append(InlineKeyboardButton("⏭", callback_data=f"adm_products_{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("⬅️ Menu", callback_data="home")])
    return InlineKeyboardMarkup(rows)

# =========================
# HELPERS
# =========================
def fee_for_method(method: str) -> int:
    method = (method or "").upper()
    if method == "DANA":
        return FEE_DANA
    if method == "BANK":
        return FEE_BANK
    if method == "QRIS":
        return FEE_QRIS
    return 0

def format_testimoni_card(barang: str, total_rp: str, kontak: str) -> str:
    barang = (barang or "-").strip().upper()
    total_rp = (total_rp or "-").strip()
    kontak = (kontak or "-").strip()
    return (
        "╔══✦•··········•✦══╗\n"
        "        💥  T E S T I M O N I  💥\n"
        "╚══✦•··········•✦══╝\n\n"
        "         TRANSAKSI BERHASIL\n\n"
        f"🛍  BARANG : {barang}\n"
        f"💰  HARGA  : {total_rp}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "✨  ALL TRANSAKSI SELESAI  ✨\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📞  HUBUNGI KAMI\n"
        f"➤ {kontak}"
    )

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

def payment_instructions(method: str, total: int, order_id: int, fee: int, discount: int, subtotal: int) -> str:
    method = method.upper()
    lines = []
    lines.append("Silakan lakukan pembayaran:")
    lines.append("")
    lines.append(f"🧾 Order ID: `#{order_id}`")
    lines.append(f"🛍 Subtotal: *{rupiah(subtotal)}*")
    if discount > 0:
        lines.append(f"🎟 Diskon: *- {rupiah(discount)}*")
    lines.append(f"💸 Fee {method}: *{rupiah(fee)}*")
    lines.append(f"💰 *Total Bayar: {rupiah(total)}*")
    lines.append("")

    if method == "DANA":
        lines.append("Metode: *DANA*")
        lines.append(f"• No: `{DANA_NUMBER or '-'}`")
        lines.append(f"• Nama: *{DANA_NAME or '-'}*")
    elif method == "BANK":
        lines.append("Metode: *TRANSFER BANK*")
        lines.append(f"• Bank: *{BANK_NAME or '-'}*")
        lines.append(f"• Rek: `{BANK_ACCOUNT or '-'}`")
        lines.append(f"• A/N: *{BANK_HOLDER or '-'}*")
    else:
        lines.append("Metode: *QRIS*")
        lines.append("• Scan QRIS dari foto yang aku kirim.")

    lines.append("")
    lines.append("Setelah bayar, kirim *bukti bayar (foto)* ke bot ini.")
    lines.append("Biar gak nyasar, kasih caption: `#<order_id>` contoh: `#12`.")
    lines.append("Kalau males ngetik caption: pakai `/confirm <id>` dulu, baru kirim foto.")
    return "\n".join(lines)

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
        "3) Atur jumlah pakai ➕ / ➖\n"
        "4) (Jika diminta) isi data akun/catatan\n"
        "5) (Opsional) pakai voucher\n"
        "6) Pilih metode bayar (DANA/BANK/QRIS)\n"
        "7) Kirim bukti bayar\n\n"
        "👤 *USER CMD*\n"
        "/start - buka menu\n"
        "/confirm <id> - ikat bukti ke order\n"
        "/testi <id> - kirim testimoni (PAID/DONE)\n"
    )

    if admin:
        text += (
            "\n👑 *ADMIN CMD*\n"
            "/addprod - tambah produk (list)\n"
            "/setprod - edit produk\n"
            "/delprod - hapus produk\n"
            "/setqris - ambil QRIS_FILE_ID\n"
            "/voucheradd - tambah voucher\n"
            "/voucherdel - hapus voucher\n"
            "/voucherlist - list voucher\n"
        )

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main(admin))

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
            "Catatan optional bisa dipakai buat keterangan.\n"
            "Butuh AKUN diatur dari panel tombol Admin Produk."
        )

    ok, bad = [], []
    with db() as conn:
        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 2:
                bad.append(f"❌ {line} (format salah)")
                continue

            name = parts[0]
            price_raw = parts[1].replace(".", "").replace(",", "").strip()
            note = parts[2] if len(parts) >= 3 else ""
            if not name:
                bad.append(f"❌ {line} (nama kosong)")
                continue
            if not price_raw.isdigit():
                bad.append(f"❌ {line} (harga invalid)")
                continue

            price = int(price_raw)
            cur = conn.execute(
                "INSERT INTO products(name, price, active, note, requires_account) VALUES(?,?,1,?,0)",
                (name, price, note),
            )
            ok.append(f"✅ #{cur.lastrowid} {name} ({rupiah(price)})")

    msg = []
    if ok:
        msg.append("🟢 *Produk ditambahkan:*")
        msg.extend(ok)
    if bad:
        msg.append("\n🔴 *Gagal:*")
        msg.extend(bad)
    await update.message.reply_text("\n".join(msg), parse_mode=ParseMode.MARKDOWN)

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
        parse_mode=ParseMode.MARKDOWN
    )

async def confirm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not context.args:
        return await update.message.reply_text("Format: /confirm <order_id> (contoh: /confirm 12)")
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

async def testi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not TESTI_CHANNEL_ID:
        return await update.message.reply_text("TESTI_CHANNEL_ID belum diset, atau bot belum admin channel.")

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
            "FROM orders o JOIN products p ON p.id=o.product_id WHERE o.id=?",
            (oid,),
        ).fetchone()

    if not row:
        return await update.message.reply_text("Order tidak ditemukan.")

    if not is_admin(u.id):
        if int(row["user_id"]) != u.id:
            return await update.message.reply_text("Itu bukan order kamu.")
        if row["status"] not in ("PAID", "DONE"):
            return await update.message.reply_text(
                f"Order #{oid} status masih *{row['status']}*. Testimoni hanya bisa kalau PAID/DONE.",
                parse_mode=ParseMode.MARKDOWN
            )

    caption = format_testimoni_card(row["product_name"], rupiah(int(row["amount"])), TESTI_CONTACT)
    await context.bot.send_message(chat_id=TESTI_CHANNEL_ID, text=caption)
    await update.message.reply_text(f"Berhasil. Testimoni order *#{oid}* sudah di-upload.", parse_mode=ParseMode.MARKDOWN)

# =========================
# ADMIN: VOUCHER COMMANDS
# =========================
async def voucheradd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        return await update.message.reply_text("Khusus admin.")

    raw = update.message.text.replace("/voucheradd", "", 1).strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 3:
        return await update.message.reply_text(
            "Format:\n"
            "/voucheradd CODE | fixed/percent | value | max_uses(optional) | expires(optional YYYY-MM-DD)\n\n"
            "Contoh:\n"
            "/voucheradd NEWYEAR | fixed | 2000 | 100 | 2026-02-01\n"
            "/voucheradd HEMAT10 | percent | 10"
        )

    code = parts[0].upper()
    vtype = parts[1].lower()
    val_raw = parts[2].replace(".", "").replace(",", "").strip()
    if vtype not in ("fixed", "percent"):
        return await update.message.reply_text("Type harus fixed atau percent.")
    if not val_raw.isdigit():
        return await update.message.reply_text("Value harus angka.")

    value = int(val_raw)
    if vtype == "percent" and not (1 <= value <= 100):
        return await update.message.reply_text("Percent harus 1..100")

    max_uses = 0
    expires = ""
    if len(parts) >= 4 and parts[3]:
        mu = parts[3].strip()
        if mu.isdigit():
            max_uses = int(mu)
    if len(parts) >= 5 and parts[4]:
        exp = parts[4].strip()
        if exp:
            if not parse_date_yyyy_mm_dd(exp):
                return await update.message.reply_text("expires harus format YYYY-MM-DD")
            expires = exp

    with db() as conn:
        conn.execute(
            "INSERT INTO vouchers(code, type, value, max_uses, used_count, expires) "
            "VALUES(?,?,?,?,0,?) "
            "ON CONFLICT(code) DO UPDATE SET type=excluded.type, value=excluded.value, max_uses=excluded.max_uses, expires=excluded.expires",
            (code, vtype, value, max_uses, expires),
        )
    await update.message.reply_text(f"OK. Voucher {code} disimpan. ({vtype} {value})")

async def voucherdel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        return await update.message.reply_text("Khusus admin.")
    if not context.args:
        return await update.message.reply_text("Format: /voucherdel CODE")
    code = context.args[0].strip().upper()
    with db() as conn:
        conn.execute("DELETE FROM vouchers WHERE code=?", (code,))
    await update.message.reply_text(f"OK. Voucher {code} dihapus.")

async def voucherlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        return await update.message.reply_text("Khusus admin.")
    with db() as conn:
        rows = conn.execute("SELECT * FROM vouchers ORDER BY code ASC").fetchall()
    if not rows:
        return await update.message.reply_text("Belum ada voucher.")
    lines = ["🎟 *Daftar Voucher*:"]
    for v in rows[:50]:
        mx = int(v["max_uses"] or 0)
        used = int(v["used_count"] or 0)
        exp = (v["expires"] or "").strip() or "-"
        lines.append(f"• `{v['code']}` — {v['type']} {v['value']} | used {used}/{mx if mx>0 else '∞'} | exp {exp}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# =========================
# CALLBACKS
# =========================
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = update.effective_user
    data = q.data

    if data == "noop":
        return

    if data == "home":
        return await safe_edit_text(q, "Menu:", reply_markup=kb_main(is_admin(u.id)))

    # ---------- USER: KATALOG ----------
    if data == "cat":
        with db() as conn:
            items = conn.execute("SELECT * FROM products WHERE active=1 ORDER BY id ASC").fetchall()
        if not items:
            return await safe_edit_text(q, "Belum ada produk aktif.", reply_markup=kb_main(is_admin(u.id)))
        context.user_data.pop("checkout", None)
        context.user_data.pop("await_voucher_code", None)
        context.user_data.pop("await_account_info", None)
        return await safe_edit_text(
            q,
            "🛒 *Katalog Produk*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_products_paged(items, page=0),
        )

    if data.startswith("cat_"):
        page = int(data.split("_")[1])
        with db() as conn:
            items = conn.execute("SELECT * FROM products WHERE active=1 ORDER BY id ASC").fetchall()
        return await safe_edit_text(
            q,
            "🛒 *Katalog Produk*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_products_paged(items, page=page),
        )

    # ---------- USER: PILIH PRODUK ----------
    if data.startswith("buy_"):
        pid = int(data.split("_", 1)[1])
        with db() as conn:
            p = conn.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if not p:
            return await safe_edit_text(q, "Produk tidak tersedia.", reply_markup=kb_main(is_admin(u.id)))

        context.user_data["checkout"] = {
            "pid": pid,
            "pname": p["name"],
            "price": int(p["price"]),
            "qty": 1,
            "voucher_code": "",
            "discount": 0,
            "note": "",  # akun/catatan
        }
        ck = context.user_data["checkout"]
        context.user_data.pop("await_voucher_code", None)
        context.user_data.pop("await_account_info", None)

        return await safe_edit_text(
            q,
            f"🛍 *{ck['pname']}*\n"
            f"Harga: *{rupiah(ck['price'])}*\n\n"
            "Atur jumlah:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_qty_panel(ck["qty"]),
        )

    # ---------- USER: QTY ----------
    if data in ("qty_plus", "qty_minus", "qty_done", "back_qty"):
        ck = context.user_data.get("checkout")
        if not ck:
            return await safe_edit_text(q, "Session checkout hilang. Balik ke katalog.", reply_markup=kb_main(is_admin(u.id)))

        if data == "qty_plus":
            ck["qty"] = max(1, min(99, int(ck["qty"]) + 1))
        elif data == "qty_minus":
            ck["qty"] = max(1, min(99, int(ck["qty"]) - 1))
        elif data == "qty_done":
            subtotal = ck["price"] * ck["qty"]
            ck["discount"] = 0
            ck["voucher_code"] = ""

            # kalau produk butuh akun/catatan, minta dulu
            if product_requires_account(int(ck["pid"])):
                context.user_data["await_account_info"] = True
                return await safe_edit_text(
                    q,
                    "🧾 *Masukin data akun / catatan untuk pesanan ini*\n"
                    "Contoh:\n"
                    "`email|password` atau `username` atau `request khusus`.\n\n"
                    "Kalau produk ini tidak butuh akun, klik *Lewati*.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=kb_account_skip(),
                )

            # tidak butuh akun -> lanjut voucher
            return await safe_edit_text(
                q,
                f"✅ Qty dipilih: *{ck['qty']}*\n"
                f"🛍 Produk: *{ck['pname']}*\n"
                f"💵 Subtotal: *{rupiah(subtotal)}*\n\n"
                "Mau pakai voucher?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_voucher_or_skip(),
            )
        elif data == "back_qty":
            context.user_data.pop("await_account_info", None)

        subtotal = ck["price"] * ck["qty"]
        return await safe_edit_text(
            q,
            f"🛍 *{ck['pname']}*\n"
            f"Harga: *{rupiah(ck['price'])}*\n"
            f"Subtotal: *{rupiah(subtotal)}*\n\n"
            "Atur jumlah:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_qty_panel(ck["qty"]),
        )

    # ---------- USER: SKIP AKUN ----------
    if data == "acc_skip":
        ck = context.user_data.get("checkout")
        if not ck:
            context.user_data.pop("await_account_info", None)
            return await safe_edit_text(q, "Session checkout hilang. Balik ke /start.", reply_markup=kb_main(is_admin(u.id)))

        ck["note"] = ""
        context.user_data.pop("await_account_info", None)

        subtotal = ck["price"] * ck["qty"]
        return await safe_edit_text(
            q,
            f"🛍 *{ck['pname']}*\n"
            f"Qty: *{ck['qty']}*\n"
            f"Subtotal: *{rupiah(subtotal)}*\n\n"
            "Mau pakai voucher?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_voucher_or_skip(),
        )

    # ---------- USER: VOUCHER FLOW ----------
    if data in ("use_voucher", "skip_voucher", "back_voucher"):
        ck = context.user_data.get("checkout")
        if not ck:
            return await safe_edit_text(q, "Session checkout hilang. Balik ke katalog.", reply_markup=kb_main(is_admin(u.id)))

        if data == "use_voucher":
            context.user_data["await_voucher_code"] = True
            return await safe_edit_text(
                q,
                "Kirim *kode voucher* sekarang.\n"
                "Contoh: `HEMAT10`\n\n"
                "Kalau mau lewati: ketik `SKIP`.",
                parse_mode=ParseMode.MARKDOWN,
            )

        if data == "skip_voucher":
            ck["voucher_code"] = ""
            ck["discount"] = 0
            return await safe_edit_text(q, "Pilih metode pembayaran:", reply_markup=kb_payment_methods())

        if data == "back_voucher":
            subtotal = ck["price"] * ck["qty"]
            disc = int(ck.get("discount", 0))
            return await safe_edit_text(
                q,
                f"🛍 *{ck['pname']}*\n"
                f"Qty: *{ck['qty']}*\n"
                f"Subtotal: *{rupiah(subtotal)}*\n"
                f"Diskon: *- {rupiah(disc)}*\n\n"
                "Mau pakai voucher?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_voucher_or_skip(),
            )

    # ---------- USER: PAYMENT + CREATE ORDER ----------
    if data.startswith("pay_"):
        ck = context.user_data.get("checkout")
        if not ck:
            return await safe_edit_text(q, "Session checkout hilang. Balik ke katalog.", reply_markup=kb_main(is_admin(u.id)))

        method = data.split("_", 1)[1].upper()
        fee = fee_for_method(method)

        subtotal = ck["price"] * ck["qty"]
        discount = int(ck.get("discount", 0))
        voucher_code = (ck.get("voucher_code") or "").upper()

        base = max(0, subtotal - discount)
        total = base + fee

        with db() as conn:
            cur = conn.execute(
                "INSERT INTO orders(user_id, username, product_id, qty, amount, note, status, created_at, updated_at, payment_method, fee, discount, voucher_code) "
                "VALUES(?,?,?,?,?,?, 'WAITING_PAYMENT', ?, ?, ?, ?, ?, ?)",
                (
                    u.id,
                    u.username or "",
                    int(ck["pid"]),
                    int(ck["qty"]),
                    int(total),
                    (ck.get("note", "") or "").strip(),
                    now_str(),
                    now_str(),
                    method,
                    int(fee),
                    int(discount),
                    voucher_code,
                ),
            )
            oid = cur.lastrowid

        if voucher_code:
            increment_voucher_use(voucher_code)

        context.user_data["await_proof_order_id"] = oid
        context.user_data.pop("checkout", None)
        context.user_data.pop("await_voucher_code", None)
        context.user_data.pop("await_account_info", None)

        msg = payment_instructions(method, total, oid, fee, discount, subtotal)
        await safe_edit_text(q, msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main(is_admin(u.id)))

        if method == "QRIS":
            await send_qris(update.effective_chat.id, context)
        return

    # ---------- USER: MY ORDERS ----------
    if data == "my":
        with db() as conn:
            rows = conn.execute(
                "SELECT o.*, p.name AS product_name FROM orders o JOIN products p ON p.id=o.product_id "
                "WHERE o.user_id=? ORDER BY o.id DESC LIMIT 10",
                (u.id,),
            ).fetchall()
        if not rows:
            return await safe_edit_text(q, "Belum ada order.", reply_markup=kb_main(is_admin(u.id)))

        lines = ["📦 *Order kamu (10 terakhir):*\n"]
        for r in rows:
            method = (r["payment_method"] or "-")
            lines.append(f"• #{r['id']} {r['product_name']} x{r['qty']} — *{r['status']}* — {rupiah(r['amount'])} — {method}")
        lines.append("")
        lines.append("Buat testimoni: /testi <order_id>")
        return await safe_edit_text(q, "\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main(is_admin(u.id)))

    # ---------- ADMIN: PRODUCTS PANEL ----------
    if data.startswith("adm_products_"):
        if not is_admin(u.id):
            return await safe_edit_text(q, "Khusus admin.", reply_markup=kb_main(False))

        page = int(data.split("_")[-1])
        with db() as conn:
            items = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()

        if not items:
            return await safe_edit_text(q, "Belum ada produk.", reply_markup=kb_main(True))

        return await safe_edit_text(
            q,
            "⚙️ *Admin Produk*\n\n"
            "Klik tombol:\n"
            "• Kiri: toggle READY / NO READY\n"
            "• Kanan: toggle BUTUH AKUN (🧾AKUN)",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_admin_products(items, page),
        )

    if data.startswith("adm_toggle_"):
        if not is_admin(u.id):
            return
        # format: adm_toggle_{pid}_{page}
        parts = data.split("_")
        pid = int(parts[2])
        page = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 0

        with db() as conn:
            p = conn.execute("SELECT active FROM products WHERE id=?", (pid,)).fetchone()
            if not p:
                return await safe_edit_text(q, "Produk tidak ditemukan.", reply_markup=kb_main(True))
            new_active = 0 if int(p["active"]) == 1 else 1
            conn.execute("UPDATE products SET active=? WHERE id=?", (new_active, pid))
            items = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()

        return await safe_edit_text(
            q,
            "⚙️ *Admin Produk*\n\n(Updated)",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_admin_products(items, page),
        )

    if data.startswith("adm_req_"):
        if not is_admin(u.id):
            return
        # format: adm_req_{pid}_{page}
        parts = data.split("_")
        pid = int(parts[2])
        page = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 0

        with db() as conn:
            p = conn.execute("SELECT requires_account FROM products WHERE id=?", (pid,)).fetchone()
            if not p:
                return await safe_edit_text(q, "Produk tidak ditemukan.", reply_markup=kb_main(True))
            new_val = 0 if int(p["requires_account"]) == 1 else 1
            conn.execute("UPDATE products SET requires_account=? WHERE id=?", (new_val, pid))
            items = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()

        return await safe_edit_text(
            q,
            "⚙️ *Admin Produk*\n\n(Updated)",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_admin_products(items, page),
        )

    # ---------- ADMIN: ORDERS / VOUCHERS QUICK INFO ----------
    if data == "adm_orders":
        if not is_admin(u.id):
            return await safe_edit_text(q, "Khusus admin.", reply_markup=kb_main(False))
        with db() as conn:
            rows = conn.execute(
                "SELECT o.*, p.name AS product_name FROM orders o JOIN products p ON p.id=o.product_id "
                "ORDER BY o.id DESC LIMIT 12"
            ).fetchall()
        if not rows:
            return await safe_edit_text(q, "Belum ada order.", reply_markup=kb_main(True))
        lines = ["🧾 *Order terbaru*:\n"]
        for r in rows:
            lines.append(f"• #{r['id']} {r['product_name']} x{r['qty']} — {r['status']} — {rupiah(r['amount'])} — {r['payment_method'] or '-'}")
        lines.append("\nApprove/Reject/DONE muncul di pesan bukti bayar yang masuk ke admin.")
        return await safe_edit_text(q, "\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main(True))

    if data == "adm_vouchers":
        if not is_admin(u.id):
            return await safe_edit_text(q, "Khusus admin.", reply_markup=kb_main(False))
        with db() as conn:
            rows = conn.execute("SELECT * FROM vouchers ORDER BY code ASC LIMIT 15").fetchall()
        lines = [
            "🎟 *Admin Voucher*",
            "",
            "Tambah/Update:",
            "`/voucheradd CODE | fixed/percent | value | max_uses(optional) | expires(optional YYYY-MM-DD)`",
            "",
            "Hapus:",
            "`/voucherdel CODE`",
            "",
            "List:",
            "`/voucherlist`",
            "",
            "Preview:"
        ]
        if not rows:
            lines.append("• (belum ada voucher)")
        else:
            for v in rows:
                mx = int(v["max_uses"] or 0)
                used = int(v["used_count"] or 0)
                exp = (v["expires"] or "").strip() or "-"
                lines.append(f"• `{v['code']}` — {v['type']} {v['value']} | used {used}/{mx if mx>0 else '∞'} | exp {exp}")
        return await safe_edit_text(q, "\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main(True))

    # ---------- ADMIN: ACTIONS ----------
    if data.startswith(("adm_appr_", "adm_rej_", "adm_done_")):
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
            return await safe_edit_text(q, "Order tidak ditemukan.", reply_markup=kb_main(True))

        if action.startswith("adm_appr"):
            new_status = "PAID"
            note = "Approved"
            user_msg = f"✅ Pembayaran diterima. Order *#{order_id}* sekarang *PAID*."
        elif action.startswith("adm_rej"):
            new_status = "REJECTED"
            note = "Rejected"
            user_msg = f"❌ Bukti bayar ditolak. Order *#{order_id}* => *REJECTED*. Kirim ulang bukti yang jelas."
        else:
            new_status = "DONE"
            note = "Done"
            user_msg = f"🏁 Order *#{order_id}* sudah *DONE*. Makasih ya."

        with db() as conn:
            conn.execute(
                "UPDATE orders SET status=?, admin_note=?, updated_at=? WHERE id=?",
                (new_status, note, now_str(), order_id),
            )

        try:
            await context.bot.send_message(chat_id=int(row["user_id"]), text=user_msg, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            log.exception("Failed to notify user")

        if new_status == "DONE" and TESTI_CHANNEL_ID:
            try:
                caption = format_testimoni_card(row["product_name"], rupiah(int(row["amount"])), TESTI_CONTACT)
                await context.bot.send_message(chat_id=TESTI_CHANNEL_ID, text=caption)
            except Exception:
                log.exception("Failed to post testi")

        return await safe_edit_text(
            q,
            f"OK. Order #{order_id} => {new_status}\nProduk: {row['product_name']}\nTotal: {rupiah(int(row['amount']))}",
            reply_markup=kb_main(True),
        )

# =========================
# MESSAGE HANDLERS
# =========================
async def msg_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # 1) INPUT AKUN/CATATAN (conditional)
    if context.user_data.get("await_account_info"):
        ck = context.user_data.get("checkout")
        if not ck:
            context.user_data.pop("await_account_info", None)
            return await update.message.reply_text("Session checkout hilang. Balik ke /start.")

        ck["note"] = text.strip()
        context.user_data.pop("await_account_info", None)

        subtotal = ck["price"] * ck["qty"]
        return await update.message.reply_text(
            f"✅ Data dicatat.\n"
            f"🛍 Produk: *{ck['pname']}*\n"
            f"Qty: *{ck['qty']}*\n"
            f"Subtotal: *{rupiah(subtotal)}*\n\n"
            "Mau pakai voucher?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_voucher_or_skip(),
        )

    # 2) INPUT VOUCHER
    if context.user_data.get("await_voucher_code"):
        ck = context.user_data.get("checkout")
        if not ck:
            context.user_data.pop("await_voucher_code", None)
            return await update.message.reply_text("Session checkout hilang. Balik ke /start.")

        if text.strip().upper() == "SKIP":
            context.user_data.pop("await_voucher_code", None)
            ck["voucher_code"] = ""
            ck["discount"] = 0
            await update.message.reply_text("OK, voucher dilewati. Pilih metode pembayaran:", reply_markup=kb_payment_methods())
            return

        code = text.strip().upper()
        subtotal = ck["price"] * ck["qty"]
        ok, reason, disc, _ = validate_voucher(code, subtotal)
        if not ok:
            return await update.message.reply_text(f"❌ {reason}\nCoba lagi, atau ketik `SKIP`.", parse_mode=ParseMode.MARKDOWN)

        ck["voucher_code"] = code
        ck["discount"] = disc
        context.user_data.pop("await_voucher_code", None)

        await update.message.reply_text(
            f"✅ Voucher dipakai: `{code}`\n"
            f"Diskon: *- {rupiah(disc)}*\n\n"
            "Sekarang pilih metode pembayaran:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_payment_methods(),
        )
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
            "Aku butuh order_id.\n"
            "Kirim foto lagi pakai caption `#<order_id>` (contoh `#12`), atau `/confirm <id>` dulu."
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
        f"OK. Bukti untuk order *#{order_id}* sudah masuk. Tunggu admin verifikasi.",
        parse_mode=ParseMode.MARKDOWN,
    )

    with db() as conn:
        info = conn.execute(
            "SELECT o.*, p.name AS product_name FROM orders o JOIN products p ON p.id=o.product_id WHERE o.id=?",
            (order_id,),
        ).fetchone()

    # hitung subtotal dari amount/fee/discount (karena unit_price tidak disimpan)
    fee = int(info["fee"] or 0)
    discount = int(info["discount"] or 0)
    amount = int(info["amount"] or 0)
    base = max(0, amount - fee)            # subtotal - discount
    subtotal = base + discount             # subtotal asli

    note_txt = (info["note"] or "").strip() or "-"

    admin_text = (
        f"🧾 *Bukti Bayar Masuk*\n"
        f"Order: *#{order_id}*\n"
        f"User: `{info['user_id']}` @{info['username'] or '-'}\n"
        f"Produk: *{info['product_name']}* x{info['qty']}\n"
        f"Akun/Catatan: `{note_txt}`\n"
        f"Metode: *{info['payment_method'] or '-'}*\n"
        f"Subtotal: *{rupiah(subtotal)}*\n"
        f"Diskon: *- {rupiah(discount)}*\n"
        f"Fee: *{rupiah(fee)}*\n"
        f"Total: *{rupiah(amount)}*\n"
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
    app.add_error_handler(on_error)

    # user commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("confirm", confirm_cmd))
    app.add_handler(CommandHandler("testi", testi_cmd))

    # admin product commands
    app.add_handler(CommandHandler("addprod", addprod_cmd))
    app.add_handler(CommandHandler("setprod", setprod_cmd))
    app.add_handler(CommandHandler("delprod", delprod_cmd))
    app.add_handler(CommandHandler("setqris", setqris_cmd))

    # admin voucher commands
    app.add_handler(CommandHandler("voucheradd", voucheradd_cmd))
    app.add_handler(CommandHandler("voucherdel", voucherdel_cmd))
    app.add_handler(CommandHandler("voucherlist", voucherlist_cmd))

    # callbacks + messages
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_text_handler))

    log.info("Bot running. DB=%s", DB_PATH)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
