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

# --- UI KEYBOARD CREATOR ---
def get_main_keyboard():
    keyboard = []
    # Dynamic 2-column layout for standard tools
    keys = list(COMMANDS.keys())
    for i in range(0, len(keys), 2):
        row = [InlineKeyboardButton(COMMANDS[keys[i]]["label"], callback_data=f"mode_{keys[i]}")]
        if i + 1 < len(keys):
            row.append(InlineKeyboardButton(COMMANDS[keys[i+1]]["label"], callback_data=f"mode_{keys[i+1]}"))
        keyboard.append(row)
    
    # Extra feature buttons row
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
        msg += "\n\n🛠 *Admin Commands Available:* \n👉 /stats — View overall bot metrics\n👉 /users — View list of database users"
        
    if update.message:
        await update.message.reply_text(msg, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.reply_text(msg, parse_mode="Markdown")

async def inline_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer(text="🔄 Processing action...", show_alert=False) # Visual pop top flash
    
    data = query.data
    if data.startswith("mode_"):
        chosen_mode = data.replace("mode_", "")
        context.user_data["mode"] = chosen_mode
        
        # Displaying instructions cleanly to user
        if chosen_mode in COMMANDS:
            label = COMMANDS[chosen_mode]["label"]
            input_fmt = COMMANDS[chosen_mode]["input"]
            text = f"📥 *Selected:* {label}\n\nPlease attach your **{input_fmt}** file right now. I am listening..."
        else:
            text = f"📥 *Selected:* {chosen_mode.upper()} Utility\n\nPlease send your file now."
            
        await query.message.reply_text(text=text, parse_mode="Markdown")

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")
    if not mode:
        await update.message.reply_text("❌ Select a command operation using the menu options first!", reply_markup=get_main_keyboard())
        return

    file_obj = update.message.document or (update.message.photo[-1] if update.message.photo else None)
    if not file_obj: return

    if file_obj.file_size > MAX_FILE_SIZE:
        await update.message.reply_text("⚠️ File size exceeds boundaries (Max 20MB allowed).")
        return

    allowed, _ = check_quota(update.effective_user.id, file_obj.file_size)
    if not allowed:
        await update.message.reply_text("🚫 Daily limit constraint reached! (30MB daily limit max).")
        return

    # Visual representation of a long running load state
    status_msg = await update.message.reply_text("⏳ `[▓░░░░░░░░░] 10%` *Downloading target file from cloud servers...*", parse_mode="Markdown")

    tg_file = await file_obj.get_file()
    fname = getattr(file_obj, "file_name", "photo.jpg")

    await status_msg.edit_text("⚙️ `[▓▓▓▓▓▓░░░░] 60%` *Running conversion protocols...*", parse_mode="Markdown")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / f"input{Path(fname).suffix or '.jpg'}"
            await tg_file.download_to_drive(custom_path=input_path)

            output_paths = await asyncio.to_thread(convert_file, mode, input_path, Path(tmp))
            
            await status_msg.edit_text("📤 `[▓▓▓▓▓▓▓▓▓▓] 100%` *Uploading outputs to Telegram...*", parse_mode="Markdown")
            
            for out in output_paths:
                with out.open("rb") as f:
                    await update.message.reply_document(document=f, filename=out.name)
            
            await status_msg.delete() # Cleans up visual logs out of history cleanly
            await update.message.reply_text("✅ *Conversion complete and successfully processed!*", parse_mode="Markdown", reply_markup=get_main_keyboard())
    except Exception as e:
        await status_msg.edit_text(f"❌ *Engine Error raised during conversion operation:* \n`{str(e)}`", parse_mode="Markdown")

# --- CONVERSION HELPERS ---
def convert_file(mode, input_path, tmp_dir):
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
    return []

# --- ADMIN PANEL REPAIRS ---
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != int(os.getenv("ADMIN_ID", 0)): return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    count = c.fetchone()[0]
    conn.close()
    await update.message.reply_text(f"📊 *Admin Metrics:* Total Registered Users = `{count}`", parse_mode="Markdown")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fixed non-functioning /users command callback"""
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
        if len(msg) > 3500: # Breaks iteration loops to prevent crashing against maximum message size limits
            msg += "\n...Truncated due to limits..."
            break
            
    await update.message.reply_text(msg, parse_mode="Markdown")

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
    bot_app.add_handler(CommandHandler("users", users_cmd)) # Registered the previously missing hook!
    
    # Process inline keyboard interactions
    bot_app.add_handler(CallbackQueryHandler(inline_button_handler))
    
    # Processing global attachments pipeline
    bot_app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))

    print("Bot service initialization sequence success... Polling telegram API.")
    bot_app.run_polling()

if __name__ == "__main__":
    main()
