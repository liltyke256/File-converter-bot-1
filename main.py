import asyncio
import datetime
import io
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
import sqlite3
from pathlib import Path
from threading import Thread

from flask import Flask
import img2pdf
import pymupdf as fitz

sys.modules["fitz"] = fitz

from fpdf import FPDF
from pdf2docx import Converter
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters

# --- DATABASE SETUP ---
DB_FILE = "bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, last_seen TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS usage (user_id INTEGER, day TEXT, bytes_sent INTEGER, PRIMARY KEY (user_id, day))''')
    conn.commit()
    conn.close()

def track_user_db(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    today = str(datetime.date.today())
    c.execute("INSERT OR REPLACE INTO users (user_id, last_seen) VALUES (?, ?)", (user_id, today))
    conn.commit()
    conn.close()

def check_quota(user_id, file_size_bytes):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    today = str(datetime.date.today())
    c.execute("SELECT bytes_sent FROM usage WHERE user_id = ? AND day = ?", (user_id, today))
    row = c.fetchone()
    current_usage = row[0] if row else 0
    MAX_DAILY = 30 * 1024 * 1024 # 30MB
    if current_usage + file_size_bytes > MAX_DAILY:
        conn.close()
        return False, current_usage
    new_usage = current_usage + file_size_bytes
    c.execute("INSERT OR REPLACE INTO usage (user_id, day, bytes_sent) VALUES (?, ?, ?)", (user_id, today, new_usage))
    conn.commit()
    conn.close()
    return True, new_usage

# --- CONFIG ---
COMMANDS = {
    "pdf2docx": {"label": "PDF to Word", "input": "PDF", "output": "DOCX", "extensions": {".pdf"}},
    "docx2pdf": {"word to PDF": "DOCX", "input": "DOCX", "output": "PDF", "extensions": {".docx"}},
    "jpg2png": {"label": "JPG to PNG", "input": "JPG/JPEG", "output": "PNG", "extensions": {".jpg", ".jpeg"}},
    "png2jpg": {"label": "PNG to JPG", "input": "PNG", "output": "JPG", "extensions": {".png"}},
    "img2pdf": {"label": "Image to PDF", "input": "JPG/PNG", "output": "PDF", "extensions": {".jpg", ".jpeg", ".png"}},
    "pdf2img": {"label": "PDF to Image", "input": "PDF", "output": "PNGs", "extensions": {".pdf"}},
    "txt2pdf": {"label": "Text to PDF", "input": "TXT", "output": "PDF", "extensions": {".txt"}},

    # New Image Extensions
    "heic2jpg": {"label": "HEIC to JPG", "input": "HEIC", "output": "JPG", "extensions": {".heic"}},
    "gif2png": {"label": "GIF to PNG", "input": "GIF", "output": "PNG", "extensions": {".gif"}},

    # New Audio Extensions
    "mp32wav": {"label": "MP3 to WAV", "input": "MP3", "output": "WAV", "extensions": {".mp3"}},
    "wav2mp3": {"label": "WAV to MP3", "input": "WAV", "output": "MP3", "extensions": {".wav"}},
    "m4a2mp3": {"label": "M4A to MP3", "input": "M4A", "output": "MP3", "extensions": {".m4a"}},
    "flac2mp3": {"label": "FLAC to MP3", "input": "FLAC", "output": "MP3", "extensions": {".flac"}},
    "ogg2mp3": {"label": "OGG to MP3", "input": "OGG", "output": "MP3", "extensions": {".ogg"}},

    # Video Extensions
    "mp42mp3": {"label": "Video to Audio", "input": "MP4", "output": "MP3", "extensions": {".mp4"}},
}
MAX_FILE_SIZE = 20 * 1024 * 1024 # 20MB

app = Flask("")
@app.route("/")
def home(): return "Bot Online"

# --- UI KEYBOARD CREATOR ---
def get_main_keyboard():
    keyboard = []
    keys = list(COMMANDS.keys())
    for i in range(0, len(keys), 2):
        row = [InlineKeyboardButton(COMMANDS[keys[i]]["label"], callback_data=f"mode_{keys[i]}")]
        if i + 1 < len(keys):
            row.append(InlineKeyboardButton(COMMANDS[keys[i+1]]["label"], callback_data=f"mode_{keys[i+1]}"))
        keyboard.append(row)

    keyboard.append([
        InlineKeyboardButton("📦 Create ZIP", callback_data="mode_zip"),
        InlineKeyboardButton("🔓 Extract ZIP", callback_data="mode_unzip")
    ])
    return InlineKeyboardMarkup(keyboard)

# --- CORE FUNCTIONS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    track_user_db(user.id)

    intro_text = (
        f"🚀 **Welcome to your ultimate File Converter Bot, {user.first_name}!**\n\n"
        "Transform documents, conversions, and files instantly using the responsive dashboard menu below.\n\n"
        "⚙️ **System Limits:**\n"
        "• Max file upload: **20MB**\n"
        "• Daily bandwidth cap: **30MB**\n\n"
        "👇 *Please select your desired file conversion protocol:* "
    )

    await update.message.reply_text(
        text=intro_text,
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg = "📖 *Help Menu*\nSelect an action from the main interface button layout, then upload your file.\n\n" + "\n".join([f"• {v['label']}" for v in COMMANDS.values()])
    msg += "\n• Create ZIP\n• Extract ZIP"

    if user_id == int(os.getenv("ADMIN_ID", 0)):
        msg += "\n\n🛠 *Admin Commands Available:* \n👉 /stats — View overall bot metrics\n👉 /users — View list of database users\n👉 /broadcast <msg> — Broadcast to users\n👉 /shutdown — Power down bot"

    if update.message:
        await update.message.reply_text(msg, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.reply_text(msg, parse_mode="Markdown")

async def inline_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer(text="🔄 Processing action...", show_alert=False)

    data = query.data
    if data.startswith("mode_"):
        chosen_mode = data.replace("mode_", "")
        context.user_data["mode"] = chosen_mode

        if chosen_mode in COMMANDS:
            label = COMMANDS[chosen_mode]["label"]
            input_fmt = COMMANDS[chosen_mode]["input"]
            text = f"📥 *Selected:* {label}\n\nPlease attach your **{input_fmt}** file right now. I am listening..."
        elif chosen_mode == "zip":
            text = "📥 *Selected:* ZIP Archive Creation Utility\n\nPlease send the file you want to compress into a ZIP file."
        elif chosen_mode == "unzip":
            text = "📥 *Selected:* UNZIP Utility\n\nPlease send your **.zip** file now."
        else:
            text = f"📥 *Selected:* {chosen_mode.upper()} Utility\n\nPlease send your file now."

        await query.message.reply_text(text=text, parse_mode="Markdown")

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")
    if not mode:
        await update.message.reply_text("❌ Select a command operation using the menu options first!", reply_markup=get_main_keyboard())
        return

    file_obj = (
        update.message.document or 
        update.message.audio or 
        update.message.voice or 
        update.message.video or 
        (update.message.photo[-1] if update.message.photo else None)
    )
    if not file_obj: return

    if file_obj.file_size > MAX_FILE_SIZE:
        await update.message.reply_text("⚠️ File size exceeds boundaries (Max 20MB allowed).")
        return

    allowed, _ = check_quota(update.effective_user.id, file_obj.file_size)
    if not allowed:
        await update.message.reply_text("🚫 Daily limit constraint reached! (30MB daily limit max).")
        return

    status_msg = await update.message.reply_text("⏳ `[▓░░░░░░░░░] 10%` *Downloading target file from cloud servers...*", parse_mode="Markdown")

    tg_file = await file_obj.get_file()
    fname = getattr(file_obj, "file_name", "file.mp4" if update.message.video else "photo.jpg")

    await status_msg.edit_text("⚙️ `[▓▓▓▓▓▓░░░░] 60%` *Running conversion protocols...*", parse_mode="Markdown")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / fname
            await tg_file.download_to_drive(custom_path=input_path)

            output_paths = await asyncio.to_thread(convert_file, mode, input_path, Path(tmp))

            await status_msg.edit_text("📤 `[▓▓▓▓▓▓▓▓▓▓] 100%` *Uploading outputs to Telegram...*", parse_mode="Markdown")

            for out in output_paths:
                with out.open("rb") as f:
                    await update.message.reply_document(document=f, filename=out.name)

            await status_msg.delete()
            await update.message.reply_text("✅ *Conversion complete and successfully processed!*", parse_mode="Markdown", reply_markup=get_main_keyboard())
    except Exception as e:
        await status_msg.edit_text(f"❌ *Engine Error raised during conversion operation:* \n`{str(e)}`", parse_mode="Markdown")

# --- CONVERSION HELPERS ---
def convert_file(mode, input_path, tmp_dir):
    # Standard document conversions
    if mode == "pdf2docx":
        out = tmp_dir / "converted.docx"
        cv = Converter(str(input_path))
        cv.convert(str(out))
        cv.close()
        return [out]
    if mode == "docx2pdf":
        office = shutil.which("libreoffice") or shutil.which("soffice")
        if not office:
            raise Exception("LibreOffice dependencies not found on server engine system path.")
        subprocess.run([office, "--headless", "--convert-to", "pdf", "--outdir", str(tmp_dir), str(input_path)], check=True)
        return [tmp_dir / f"{input_path.stem}.pdf"]
    if mode == "img2pdf":
        out = tmp_dir / "converted.pdf"
        out.write_bytes(img2pdf.convert(str(input_path)))
        return [out]

    # Inside the convert_file(mode, input_path, tmp_dir)
    if mode == "txt2pdf":
        out = tmp_dir / "converted.pdf"
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Arial", size=12)
        with open(input_path, 'r', encoding='utf-8') as f:
            for line in f:
                pdf.cell(200, 10, txt=line, ln=True, align='L')
        pdf.output(str(out))
        return [out]

    # ZIP / UNZIP Logic Implementation
    if mode == "zip":
        out = tmp_dir / f"{input_path.stem}.zip"
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(input_path, arcname=input_path.name)
        return [out]

    if mode == "unzip":
        if not zipfile.is_zipfile(input_path):
            raise Exception("The provided file is not a valid zip compression archive.")
        extract_dir = tmp_dir / "extracted_files"
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(input_path, 'r') as zipf:
            zipf.extractall(extract_dir)
        return [p for p in extract_dir.rglob('*') if p.is_file()]

    # Universal Image & Audio conversion handling using Pillow and FFMPEG
    if mode in COMMANDS:
        target_ext = list(COMMANDS[mode]["extensions"])[0] if "extensions" in COMMANDS[mode] else None
        output_fmt = COMMANDS[mode]["output"].lower()
        out = tmp_dir / f"converted.{output_fmt}"

        # Image Engine conversions
        if mode in ["jpg2png", "png2jpg", "heic2jpg", "gif2png", "pdf2img"]:
            if mode == "pdf2img":
                doc = fitz.open(input_path)
                images = []
                for i, page in enumerate(doc):
                    pix = page.get_pixmap()
                    p_out = tmp_dir / f"page_{i+1}.png"
                    pix.save(str(p_out))
                    images.append(p_out)
                return images
            else:
                if input_path.suffix.lower() == '.heic':
                    from pillow_heif import register_heif_opener
                    register_heif_opener()
                img = Image.open(input_path)
                if output_fmt == "jpg" and img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                img.save(out, format=output_fmt.upper() if output_fmt != "jpg" else "JPEG")
                return [out]

        # Audio Engine & Video extraction conversions (requires ffmpeg executable binary dependencies)
        if output_fmt in ["wav", "mp3"]:
            subprocess.run(["ffmpeg", "-y", "-i", str(input_path), "-vn", str(out)], check=True)
            return [out]

    return []

# --- ADMIN PANEL FUNCTIONS ---
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != int(os.getenv("ADMIN_ID", 0)): return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    count = c.fetchone()[0]
    conn.close()
    await update.message.reply_text(f"📊 *Admin Metrics:* Total Registered Users = `{count}`", parse_mode="Markdown")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != int(os.getenv("ADMIN_ID", 0)): return

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, last_seen FROM users ORDER BY last_seen DESC")
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("📁 No registered user tracking logs discovered inside local database.")
        return

    msg = "👥 *Database User Directory Logs:*\n\n"
    for idx, row in enumerate(rows, 1):
        msg += f"{idx}. ID: `{row[0]}` | Last Seen: `{row[1]}`\n"
        if len(msg) > 3500:
            msg += "\n...Truncated due to limits..."
            break

    await update.message.reply_text(msg, parse_mode="Markdown")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast system messages to all registered users inside DB"""
    if update.effective_user.id != int(os.getenv("ADMIN_ID", 0)): return

    if not context.args:
        await update.message.reply_text("❌ Please format message string payload: `/broadcast Your text content here`", parse_mode="Markdown")
        return

    broadcast_msg = " ".join(context.args)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = c.fetchall()
    conn.close()

    success, failure = 0, 0
    await update.message.reply_text(f"📢 Starting broadcast sequence to {len(users)} users...")

    for user in users:
        try:
            await context.bot.send_message(chat_id=user[0], text=broadcast_msg, parse_mode="Markdown")
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failure += 1

    await update.message.reply_text(f"✅ *Broadcast completed!*\n• Successful deliveries: `{success}`\n• Failed deliveries: `{failure}`", parse_mode="Markdown")

async def shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Safely terminate polling sequences remotely"""
    if update.effective_user.id != int(os.getenv("ADMIN_ID", 0)): return

    await update.message.reply_text("🛑 *Power down execution payload received. Stopping application loops...*", parse_mode="Markdown")
    os._exit(0)

# --- MAIN RUNNER ---
def main():
    init_db()
    token = os.getenv("BOT_TOKEN")
    admin = os.getenv("ADMIN_ID")
    if not token or not admin:
        print("CRITICAL LOG ERROR: Missing BOT_TOKEN or ADMIN_ID environment entries!")
        return

    Thread(target=lambda: app.run(host="0.0.0.0", port=8080), daemon=True).start()

    bot_app = Application.builder().token(token).build()

    # Handlers Configuration
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("help", help_cmd))
    bot_app.add_handler(CommandHandler("stats", stats))
    bot_app.add_handler(CommandHandler("users", users_cmd))
    bot_app.add_handler(CommandHandler("broadcast", broadcast))
    bot_app.add_handler(CommandHandler("shutdown", shutdown))

    bot_app.add_handler(CallbackQueryHandler(inline_button_handler))

    # Process documents, photos, audio, voices, and videos
    bot_app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.AUDIO | filters.VOICE | filters.VIDEO, handle_file))

    print("Bot service initialization sequence success... Polling telegram API.")
    bot_app.run_polling()

if __name__ == "__main__":
    main()
