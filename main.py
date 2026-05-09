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
    # Table for tracking users
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (user_id INTEGER PRIMARY KEY, last_seen TEXT)''')
    # Table for daily usage (size in bytes)
    c.execute('''CREATE TABLE IF NOT EXISTS usage 
                 (user_id INTEGER, day TEXT, bytes_sent INTEGER, PRIMARY KEY (user_id, day))''')
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
    
    # 30MB limit = 30 * 1024 * 1024 bytes
    MAX_DAILY = 30 * 1024 * 1024
    if current_usage + file_size_bytes > MAX_DAILY:
        conn.close()
        return False, current_usage
    
    new_usage = current_usage + file_size_bytes
    c.execute("INSERT OR REPLACE INTO usage (user_id, day, bytes_sent) VALUES (?, ?, ?)", 
              (user_id, today, new_usage))
    conn.commit()
    conn.close()
    return True, new_usage

# --- CONFIGURATION ---
COMMANDS = {
    "pdf2docx": {"label": "PDF to Word", "input": "PDF", "output": "DOCX", "extensions": {".pdf"}},
    "docx2pdf": {"label": "Word to PDF", "input": "DOCX", "output": "PDF", "extensions": {".docx"}},
    "jpg2png": {"label": "JPG to PNG", "input": "JPG/JPEG", "output": "PNG", "extensions": {".jpg", ".jpeg"}},
    "png2jpg": {"label": "PNG to JPG", "input": "PNG", "output": "JPG", "extensions": {".png"}},
    "img2pdf": {"label": "Image to PDF", "input": "JPG/PNG", "output": "PDF", "extensions": {".jpg", ".jpeg", ".png"}},
    "pdf2img": {"label": "PDF to Image", "input": "PDF", "output": "PNGs", "extensions": {".pdf"}},
}

FILE_LIMIT_MB = 20
MAX_FILE_SIZE = FILE_LIMIT_MB * 1024 * 1024  # 20MB in bytes

logging.basicConfig(level=logging.WARNING)
app = Flask("")

@app.route("/")
def home(): return "Bot is running perfectly!"

def run(): app.run(host="0.0.0.0", port=8080)

# --- BOT LOGIC ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    track_user_db(user.id)
    
    welcome_text = (
        f"Hello {user.first_name}! 🤖\n\n"
        "I am your File Converter Bot. I can handle PDFs, Images, and ZIPs.\n\n"
        "*Limits:*\n"
        "• Max file size: 20MB\n"
        "• Daily quota: 30MB total\n\n"
        "*Commands:*\n"
        "/pdf2docx, /docx2pdf, /img2pdf, /pdf2img\n"
        "/zip, /unzip, /help\n\n"
        "Select a command first, then send your file!"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")
    if not mode:
        await update.message.reply_text("❌ Please select a command first (e.g., /pdf2docx)")
        return

    # Check File Size
    file_obj = update.message.document or (update.message.photo[-1] if update.message.photo else None)
    if not file_obj: return

    actual_size = file_obj.file_size
    if actual_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"⚠️ File too large! Maximum allowed is {FILE_LIMIT_MB}MB.")
        return

    # Check Daily Quota
    allowed, total_used = check_quota(update.effective_user.id, actual_size)
    if not allowed:
        await update.message.reply_text("🚫 Daily limit reached! You can only process 30MB per day.")
        return

    # Proceed to conversion (same logic as your previous script)
    await update.message.reply_text("⚙️ Processing... please wait.")
    
    # [Internal logic for conversion continues here as in your source...]
    # For brevity, calling your existing conversion handlers here
    source = await get_uploaded_file(update)
    if source:
        telegram_file, filename = source
        if mode == "zip": await add_file_to_zip(update, context, telegram_file, filename)
        elif mode == "unzip": await extract_uploaded_zip(update, context, telegram_file, filename)
        else: await perform_conversion(update, context, mode, telegram_file, filename)

async def perform_conversion(update, context, mode, telegram_file, filename):
    details = COMMANDS.get(mode)
    if not details: return
    suffix = Path(filename).suffix.lower()
    if suffix not in details["extensions"]:
        await update.message.reply_text(f"❌ Invalid format for {mode}")
        return
        
    try:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / safe_filename(filename, mode)
            await telegram_file.download_to_drive(custom_path=input_path)
            output_paths = await asyncio.to_thread(convert_file, mode, input_path, Path(tmp))
            for out in output_paths:
                with out.open("rb") as f:
                    await update.message.reply_document(document=f, filename=out.name)
        await update.message.reply_text("✅ Done!")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

# --- ADMIN COMMANDS ---
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total = c.fetchone()[0]
    conn.close()
    await update.message.reply_text(f"📊 *Bot Statistics*\nTotal Users: {total}", parse_mode="Markdown")

async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, last_seen FROM users LIMIT 20")
    rows = c.fetchall()
    conn.close()
    msg = "\n".join([f"ID: `{r[0]}` | Last: {r[1]}" for r in rows])
    await update.message.reply_text(f"👥 *Recent Users:*\n{msg}", parse_mode="Markdown")

# [Helper functions: get_uploaded_file, convert_file, etc. remain the same as your source]
# (Include your existing helper functions here to complete the script)

def get_admin_id() -> int:
    return int(os.getenv("ADMIN_ID", 0))

def is_admin(user_id: int) -> bool:
    return user_id == get_admin_id()

# --- RE-INSERT YOUR REMAINING HELPER FUNCTIONS HERE ---
# (convert_pdf_to_docx, convert_docx_to_pdf, etc.)

def main():
    init_db()
    token = os.getenv("BOT_TOKEN")
    Thread(target=run, daemon=True).start()
    
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("users", users_list))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("zip", zip_cmd))
    app.add_handler(CommandHandler("donezip", done_zip))
    app.add_handler(CommandHandler("unzip", unzip_cmd))
    
    for cmd in COMMANDS:
        app.add_handler(CommandHandler(cmd, set_mode))
        
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))
    
    print("Railway Bot Online...")
    app.run_polling()

if __name__ == "__main__":
    main()
