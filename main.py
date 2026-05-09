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

from pdf2docx import Converter
from PIL import Image
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

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
    "docx2pdf": {"label": "Word to PDF", "input": "DOCX", "output": "PDF", "extensions": {".docx"}},
    "jpg2png": {"label": "JPG to PNG", "input": "JPG/JPEG", "output": "PNG", "extensions": {".jpg", ".jpeg"}},
    "png2jpg": {"label": "PNG to JPG", "input": "PNG", "output": "JPG", "extensions": {".png"}},
    "img2pdf": {"label": "Image to PDF", "input": "JPG/PNG", "output": "PDF", "extensions": {".jpg", ".jpeg", ".png"}},
    "pdf2img": {"label": "PDF to Image", "input": "PDF", "output": "PNGs", "extensions": {".pdf"}},
}
MAX_FILE_SIZE = 20 * 1024 * 1024 # 20MB

app = Flask("")
@app.route("/")
def home(): return "Bot Online"

# --- CORE FUNCTIONS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    track_user_db(user.id)
    await update.message.reply_text(
        f"👋 Hello {user.first_name}!\n\n"
        "I'm your **File Converter Bot**.\n"
        "• Max file: 20MB\n"
        "• Daily limit: 30MB\n\n"
        "Choose a command:\n/pdf2docx, /docx2pdf, /img2pdf, /zip, /help",
        parse_mode="Markdown"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "📖 *Help Menu*\nSelect a command, then upload your file.\n\n" + "\n".join([f"/{k} - {v['label']}" for k,v in COMMANDS.items()])
    msg += "\n/zip - Create ZIP\n/unzip - Extract ZIP"
    if user_id := update.effective_user.id:
        if user_id == int(os.getenv("ADMIN_ID", 0)):
            msg += "\n\n🛠 *Admin:* /stats, /users"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def set_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    command = update.message.text.removeprefix("/").split()[0].lower()
    context.user_data["mode"] = command
    await update.message.reply_text(f"📥 Send the {COMMANDS[command]['input']} file.")

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")
    if not mode:
        await update.message.reply_text("❌ Select a command first!")
        return

    file_obj = update.message.document or (update.message.photo[-1] if update.message.photo else None)
    if not file_obj: return

    if file_obj.file_size > MAX_FILE_SIZE:
        await update.message.reply_text("⚠️ File too large (Max 20MB).")
        return

    allowed, _ = check_quota(update.effective_user.id, file_obj.file_size)
    if not allowed:
        await update.message.reply_text("🚫 Daily limit (30MB) reached!")
        return

    await update.message.reply_text("⚙️ Processing...")
    
    # Logic for file retrieval
    tg_file = await file_obj.get_file()
    fname = getattr(file_obj, "file_name", "photo.jpg")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / f"input{Path(fname).suffix or '.jpg'}"
            await tg_file.download_to_drive(custom_path=input_path)
            
            # Conversion logic
            output_paths = await asyncio.to_thread(convert_file, mode, input_path, Path(tmp))
            for out in output_paths:
                with out.open("rb") as f:
                    await update.message.reply_document(document=f, filename=out.name)
        await update.message.reply_text("✅ Done!")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

# --- CONVERSION HELPERS ---
def convert_file(mode, input_path, tmp_dir):
    if mode == "pdf2docx":
        out = tmp_dir / "converted.docx"
        cv = Converter(str(input_path))
        cv.convert(str(out)); cv.close()
        return [out]
    if mode == "docx2pdf":
        office = shutil.which("libreoffice") or shutil.which("soffice")
        subprocess.run([office, "--headless", "--convert-to", "pdf", "--outdir", str(tmp_dir), str(input_path)], check=True)
        return [tmp_dir / f"{input_path.stem}.pdf"]
    if mode == "img2pdf":
        out = tmp_dir / "converted.pdf"
        out.write_bytes(img2pdf.convert(str(input_path)))
        return [out]
    # Add other simple image conversions here as needed
    return []

# --- ADMIN ---
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != int(os.getenv("ADMIN_ID", 0)): return
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    count = c.fetchone()[0]; conn.close()
    await update.message.reply_text(f"📊 Total Users: {count}")

# --- MAIN ---
def main():
    init_db()
    token = os.getenv("BOT_TOKEN")
    admin = os.getenv("ADMIN_ID")
    if not token or not admin:
        print("Missing BOT_TOKEN or ADMIN_ID!")
        return

    Thread(target=lambda: app.run(host="0.0.0.0", port=8080), daemon=True).start()
    
    bot_app = Application.builder().token(token).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("help", help_cmd))
    bot_app.add_handler(CommandHandler("stats", stats))
    
    for cmd in COMMANDS:
        bot_app.add_handler(CommandHandler(cmd, set_mode))
        
    bot_app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))
    
    print("Bot is Starting...")
    bot_app.run_polling()

if __name__ == "__main__":
    main()
