import asyncio
import base64
import datetime
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
import requests

from pathlib import Path
from threading import Thread

# Try importing psutil safely for host RAM checks
try:
    import psutil
except ImportError:
    psutil = None

from flask import Flask
import img2pdf
import pymupdf as fitz

sys.modules["fitz"] = fitz

import pandas as pd
from fpdf import FPDF
from pdf2docx import Converter
from PIL import Image

# Try importing PdfReader safely
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters

# Setup basic logging to see issues in logs
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- CLOUD SQLITE (TURSO HTTP PIPELINE) SETUP ---
TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

def get_turso_http_url():
    if not TURSO_URL:
        raise Exception("Missing TURSO_DATABASE_URL in system environment!")
    # Normalize libsql:// or wss:// protocols to https:// for direct HTTP execution
    url = TURSO_URL
    if url.startswith("libsql://"):
        url = url.replace("libsql://", "https://", 1)
    elif url.startswith("wss://"):
        url = url.replace("wss://", "https://", 1)
    
    if not url.endswith("/v2/pipeline"):
        url = url.rstrip("/") + "/v2/pipeline"
    return url

def execute_turso_query(stmt_sql, args=None):
    """Executes a SQL query directly via Turso HTTP REST API with zero native dependencies."""
    if not TURSO_TOKEN:
        raise Exception("Missing TURSO_AUTH_TOKEN in system environment!")
    
    endpoint = get_turso_http_url()
    
    # Format positional arguments for Turso Pipeline API
    formatted_args = []
    if args:
        for arg in args:
            if isinstance(arg, int):
                formatted_args.append({"type": "integer", "value": str(arg)})
            elif isinstance(arg, str):
                formatted_args.append({"type": "text", "value": arg})
            else:
                formatted_args.append({"type": "text", "value": str(arg)})

    payload = {
        "requests": [
            {
                "type": "execute",
                "stmt": {
                    "sql": stmt_sql,
                    "args": formatted_args
                }
            },
            {"type": "close"}
        ]
    }
    
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {TURSO_TOKEN}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("results", [])
            if results and results[0].get("type") == "ok":
                response_stmt = results[0]["response"]["result"]
                rows = []
                for row in response_stmt.get("rows", []):
                    parsed_row = [cell.get("value") for cell in row]
                    rows.append(parsed_row)
                return rows
            return []
    except Exception as e:
        logging.error(f"Turso HTTP Execution Error: {e}")
        raise e

def init_db():
    try:
        execute_turso_query("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, last_seen TEXT);")
        execute_turso_query("CREATE TABLE IF NOT EXISTS usage (user_id INTEGER, day TEXT, bytes_sent INTEGER, PRIMARY KEY (user_id, day));")
        logging.info("Cloud SQLite schema verified successfully via HTTP pipeline.")
    except Exception as e:
        logging.error(f"Database initialization deferred or failed: {e}")

def track_user_db(user_id):
    try:
        today = str(datetime.date.today())
        execute_turso_query("INSERT OR REPLACE INTO users (user_id, last_seen) VALUES (?, ?);", [user_id, today])
    except Exception as e:
        logging.error(f"Failed to track user in Cloud DB: {e}")

def check_quota(user_id, file_size_bytes):
    try:
        today = str(datetime.date.today())
        rows = execute_turso_query("SELECT bytes_sent FROM usage WHERE user_id = ? AND day = ?;", [user_id, today])
        current_usage = int(rows[0][0]) if rows and rows[0][0] is not None else 0
        MAX_DAILY = 100 * 1024 * 1024 # 100MB daily limit buffer
        
        if current_usage + file_size_bytes > MAX_DAILY:
            return False, current_usage
            
        new_usage = current_usage + file_size_bytes
        execute_turso_query("INSERT OR REPLACE INTO usage (user_id, day, bytes_sent) VALUES (?, ?, ?);", [user_id, today, new_usage])
        return True, new_usage
    except Exception as e:
        logging.error(f"Quota validation error: {e}")
        return True, 0  # Fail-safe to let users convert if DB experiences latency spikes

# --- CLOUDCONVERT & CONVERTIO HYBRID API ENGINE ---
CLOUDCONVERT_KEY = os.getenv("CLOUDCONVERT_API_KEY")
CONVERTIO_KEY = os.getenv("CONVERTIO_API_KEY")

CLOUDCONVERT_BASE_URL = "https://api.cloudconvert.com/v2"
CONVERTIO_BASE_URL = "https://api.convertio.co"

LOCAL_MAX_SIZE_MB = 5.0
SAFE_RAM_THRESHOLD_PERCENT = 70.0
MIN_AVAILABLE_RAM_MB = 100.0  # Offload to APIs if free server RAM is under 100MB

def get_cloudconvert_credits() -> int:
    """Queries CloudConvert account to inspect remaining daily credits."""
    if not CLOUDCONVERT_KEY:
        logging.warning("CLOUDCONVERT_API_KEY is missing from environment variables.")
        return 0
    headers = {"Authorization": f"Bearer {CLOUDCONVERT_KEY}"}
    try:
        response = requests.get(f"{CLOUDCONVERT_BASE_URL}/users/me", headers=headers, timeout=5)
        response.raise_for_status()
        credits = response.json()["data"].get("credits", 0)
        logging.info(f"CloudConvert daily credits available: {credits}")
        return credits
    except Exception as e:
        logging.error(f"Failed to fetch CloudConvert balance: {e}")
        return 0

def convert_via_cloudconvert(input_path: Path, output_format: str, tmp_dir: Path) -> Path:
    """Offloads heavy file conversion tasks to CloudConvert REST API."""
    if not CLOUDCONVERT_KEY:
        raise Exception("Missing CLOUDCONVERT_API_KEY environment variable!")

    credits_left = get_cloudconvert_credits()
    if credits_left < 1:
        raise Exception("Daily CloudConvert API conversion quota exhausted (25/25 used today).")

    headers = {"Authorization": f"Bearer {CLOUDCONVERT_KEY}"}
    out_file = tmp_dir / f"converted.{output_format.lower()}"

    # Step 1: Create Job
    job_payload = {
        "tasks": {
            "upload-file": {"operation": "import/upload"},
            "convert-file": {
                "operation": "convert",
                "input": "upload-file",
                "output_format": output_format.lower()
            },
            "export-file": {
                "operation": "export/url",
                "input": "convert-file"
            }
        }
    }
    resp = requests.post(f"{CLOUDCONVERT_BASE_URL}/jobs", json=job_payload, headers=headers, timeout=10)
    resp.raise_for_status()
    job_data = resp.json()["data"]

    # Step 2: Upload local file to CloudConvert
    upload_task = next(t for t in job_data["tasks"] if t["name"] == "upload-file")
    upload_url = upload_task["result"]["form"]["url"]
    upload_params = upload_task["result"]["form"]["parameters"]

    with open(input_path, "rb") as f:
        requests.post(upload_url, data=upload_params, files={"file": f}, timeout=60)

    # Step 3: Wait for job processing completion
    job_id = job_data["id"]
    status_resp = requests.get(f"{CLOUDCONVERT_BASE_URL}/jobs/{job_id}/wait", headers=headers, timeout=120)
    status_resp.raise_for_status()

    # Step 4: Stream resulting output file back
    export_task = next(t for t in status_resp.json()["data"]["tasks"] if t["name"] == "export-file")
    file_download_url = export_task["result"]["files"][0]["url"]

    with requests.get(file_download_url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(out_file, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

    return out_file

def convert_via_convertio(input_path: Path, output_format: str, tmp_dir: Path) -> Path:
    """Fail-safe conversion offloader using Convertio REST API."""
    if not CONVERTIO_KEY:
        raise Exception("Missing CONVERTIO_API_KEY environment variable!")

    out_file = tmp_dir / f"converted.{output_format.lower()}"

    # Step 1: Base64 encode the file for safe transmission
    with open(input_path, "rb") as f:
        encoded_file = base64.b64encode(f.read()).decode("utf-8")

    payload = {
        "apikey": CONVERTIO_KEY,
        "input": "base64",
        "file": encoded_file,
        "filename": input_path.name,
        "outputformat": output_format.lower()
    }

    logging.info("Initiating Convertio fail-safe conversion request...")
    resp = requests.post(f"{CONVERTIO_BASE_URL}/convert", json=payload, timeout=30)
    resp.raise_for_status()
    res_data = resp.json()

    if res_data.get("status") != "ok":
        raise Exception(f"Convertio API error: {res_data.get('error')}")

    conv_id = res_data["data"]["id"]

    # Step 2: Poll Convertio for job completion
    status_url = f"{CONVERTIO_BASE_URL}/convert/{conv_id}/status"
    for _ in range(30): # Poll up to 60 seconds
        time.sleep(2)
        status_resp = requests.get(status_url, timeout=10)
        status_data = status_resp.json()

        if status_data.get("status") == "ok":
            step = status_data["data"]["step"]
            if step == "finish":
                download_url = status_data["data"]["output"]["url"]
                # Step 3: Stream converted file
                with requests.get(download_url, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    with open(out_file, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                return out_file
            elif step == "error":
                raise Exception(f"Convertio conversion failed: {status_data['data'].get('error')}")

    raise Exception("Convertio conversion timed out.")

def convert_via_cloud_apis(input_path: Path, output_format: str, tmp_dir: Path) -> Path:
    """Master Cloud Router: Tries CloudConvert first; falls back to Convertio if it fails or runs out of credits."""
    try:
        logging.info(f"Attempting cloud conversion to {output_format} via CloudConvert...")
        return convert_via_cloudconvert(input_path, output_format, tmp_dir)
    except Exception as cc_err:
        logging.warning(f"CloudConvert failed or quota exhausted: {cc_err}. Switching to Convertio fail-safe...")
        try:
            return convert_via_convertio(input_path, output_format, tmp_dir)
        except Exception as conv_err:
            logging.error(f"Convertio fail-safe also failed: {conv_err}")
            raise Exception("All cloud conversion APIs are currently unavailable or out of credits. Please try again tomorrow!")

def convert_pdf_to_txt_locally(input_path: Path, tmp_dir: Path) -> Path:
    """Performs lightweight pure-Python PDF text extraction on-server."""
    if not PdfReader:
        raise Exception("pypdf module not available locally.")
    reader = PdfReader(str(input_path))
    pages_text = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages_text.append(text)
    out_file = tmp_dir / "extracted_text.txt"
    out_file.write_text("\n\n--- Page Break ---\n\n".join(pages_text), encoding="utf-8")
    return out_file

# --- CONFIG ---
COMMANDS = {
    # Documents (Standard & Expanded Student Formats)
    "pdf2docx": {"label": "PDF to Word", "input": "PDF", "output": "DOCX", "extensions": {".pdf"}, "cat": "doc"},
    "docx2pdf": {"label": "Word to PDF", "input": "DOCX", "output": "PDF", "extensions": {".docx"}, "cat": "doc"},
    "doc2pdf": {"label": "Legacy DOC to PDF", "input": "DOC", "output": "PDF", "extensions": {".doc"}, "cat": "doc"},
    "doc2docx": {"label": "DOC to DOCX", "input": "DOC", "output": "DOCX", "extensions": {".doc"}, "cat": "doc"},
    "odt2pdf": {"label": "ODT to PDF", "input": "ODT", "output": "PDF", "extensions": {".odt"}, "cat": "doc"},
    "odt2docx": {"label": "ODT to Word", "input": "ODT", "output": "DOCX", "extensions": {".odt"}, "cat": "doc"},
    "pages2pdf": {"label": "Pages to PDF", "input": "PAGES", "output": "PDF", "extensions": {".pages"}, "cat": "doc"},
    "pages2docx": {"label": "Pages to Word", "input": "PAGES", "output": "DOCX", "extensions": {".pages"}, "cat": "doc"},
    "rtf2pdf": {"label": "RTF to PDF", "input": "RTF", "output": "PDF", "extensions": {".rtf"}, "cat": "doc"},
    "rtf2docx": {"label": "RTF to Word", "input": "RTF", "output": "DOCX", "extensions": {".rtf"}, "cat": "doc"},
    "txt2pdf": {"label": "Text to PDF", "input": "TXT", "output": "PDF", "extensions": {".txt"}, "cat": "doc"},
    "pdf2txt": {"label": "PDF to Text", "input": "PDF", "output": "TXT", "extensions": {".pdf"}, "cat": "doc"},
    
    # Presentations & Slides
    "pptx2pdf": {"label": "PPTX to PDF", "input": "PPTX", "output": "PDF", "extensions": {".pptx"}, "cat": "doc"},
    "ppt2pdf": {"label": "PPT to PDF", "input": "PPT", "output": "PDF", "extensions": {".ppt"}, "cat": "doc"},
    "key2pdf": {"label": "Keynote to PDF", "input": "KEY", "output": "PDF", "extensions": {".key"}, "cat": "doc"},

    # Spreadsheets
    "csv2xlsx": {"label": "CSV to Excel", "input": "CSV", "output": "XLSX", "extensions": {".csv"}, "cat": "doc"},
    "xls2xlsx": {"label": "XLS to XLSX", "input": "XLS", "output": "XLSX", "extensions": {".xls"}, "cat": "doc"},
    "xls2pdf": {"label": "XLS/XLSX to PDF", "input": "XLS/XLSX", "output": "PDF", "extensions": {".xls", ".xlsx"}, "cat": "doc"},
    "ods2xlsx": {"label": "ODS to XLSX", "input": "ODS", "output": "XLSX", "extensions": {".ods"}, "cat": "doc"},
    "ods2pdf": {"label": "ODS to PDF", "input": "ODS", "output": "PDF", "extensions": {".ods"}, "cat": "doc"},

    # E-Books & Study Materials
    "epub2pdf": {"label": "EPUB to PDF", "input": "EPUB", "output": "PDF", "extensions": {".epub"}, "cat": "doc"},
    "epub2txt": {"label": "EPUB to Text", "input": "EPUB", "output": "TXT", "extensions": {".epub"}, "cat": "doc"},
    "mobi2pdf": {"label": "MOBI to PDF", "input": "MOBI", "output": "PDF", "extensions": {".mobi"}, "cat": "doc"},
    "pdf2epub": {"label": "PDF to EPUB", "input": "PDF", "output": "EPUB", "extensions": {".pdf"}, "cat": "doc"},

    # Images
    "jpg2png": {"label": "JPG to PNG", "input": "JPG/JPEG", "output": "PNG", "extensions": {".jpg", ".jpeg"}, "cat": "img"},
    "png2jpg": {"label": "PNG to JPG", "input": "PNG", "output": "JPG", "extensions": {".png"}, "cat": "img"},
    "img2pdf": {"label": "Image to PDF", "input": "JPG/PNG", "output": "PDF", "extensions": {".jpg", ".jpeg", ".png"}, "cat": "img"},
    "heic2jpg": {"label": "HEIC to JPG", "input": "HEIC", "output": "JPG", "extensions": {".heic"}, "cat": "img"},
    "gif2png": {"label": "GIF to PNG", "input": "GIF", "output": "PNG", "extensions": {".gif"}, "cat": "img"},
    "pdf2img": {"label": "PDF to Image", "input": "PDF", "output": "PNGs", "extensions": {".pdf"}, "cat": "img"},
    "ocr": {"label": "Image to Text (OCR)", "input": "Image", "output": "TXT", "extensions": {".jpg", ".jpeg", ".png"}, "cat": "img"},

    # Audio
    "text2speech": {"label": "Text to Speech", "input": "TEXT", "output": "MP3", "extensions": set(), "cat": "audio"},
    "mp32wav": {"label": "MP3 to WAV", "input": "MP3", "output": "WAV", "extensions": {".mp3"}, "cat": "audio"},
    "wav2mp3": {"label": "WAV to MP3", "input": "WAV", "output": "MP3", "extensions": {".wav"}, "cat": "audio"},
    "m4a2mp3": {"label": "M4A to MP3", "input": "M4A", "output": "MP3", "extensions": {".m4a"}, "cat": "audio"},
    "flac2mp3": {"label": "FLAC to MP3", "input": "FLAC", "output": "MP3", "extensions": {".flac"}, "cat": "audio"},
    "ogg2mp3": {"label": "OGG to MP3", "input": "OGG", "output": "MP3", "extensions": {".ogg"}, "cat": "audio"},
}
MAX_FILE_SIZE = 50 * 1024 * 1024 # 50MB maximum (Telegram Bot API limit)

app = Flask("")
@app.route("/")
def home(): return "Bot Online"

# --- UI KEYBOARD CREATORS ---
def get_categories_keyboard():
    keyboard = [
        [InlineKeyboardButton("📄 Documents", callback_data="cat_doc"), InlineKeyboardButton("🖼 Images", callback_data="cat_img")],
        [InlineKeyboardButton("🎵 Audio", callback_data="cat_audio"), InlineKeyboardButton("📦 Archive Utilities", callback_data="cat_zip")],
        [InlineKeyboardButton("✍️ Feedback", callback_data="mode_feedback")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_category_tools_keyboard(category):
    keyboard = []
    if category in ["doc", "img", "audio"]:
        keys = [k for k, v in COMMANDS.items() if v["cat"] == category]
        for i in range(0, len(keys), 2):
            row = [InlineKeyboardButton(COMMANDS[keys[i]]["label"], callback_data=f"mode_{keys[i]}")]
            if i + 1 < len(keys):
                row.append(InlineKeyboardButton(COMMANDS[keys[i+1]]["label"], callback_data=f"mode_{keys[i+1]}"))
            keyboard.append(row)
    elif category == "zip":
        keyboard.append([InlineKeyboardButton("📦 Create ZIP", callback_data="mode_zip"), InlineKeyboardButton("🔓 Extract ZIP", callback_data="mode_unzip")])

    keyboard.append([InlineKeyboardButton("⬅️ Back to Categories", callback_data="cat_back")])
    return InlineKeyboardMarkup(keyboard)

def get_tts_speed_keyboard():
    keyboard = [
        [InlineKeyboardButton("🐢 Slow Speed", callback_data="ttsspeed_-15%"), InlineKeyboardButton("🏃 Normal Speed", callback_data="ttsspeed_+0%")]
    ]
    return InlineKeyboardMarkup(keyboard)

# --- CORE FUNCTIONS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    track_user_db(user.id)

    intro_text = (
        f"🚀 **Welcome to your ultimate File Converter Bot, {user.first_name}!**\n\n"
        "Transform documents, conversions, and files instantly using the structured dashboard below.\n\n"
        "⚙️ **System Limits:**\n"
        "• Max file upload: **50MB** (Powered by CloudConvert & Convertio APIs)\n"
        "• High performance cloud conversion processing\n\n"
        "👇 *Please select a category to view supported conversions:* "
    )
    await update.message.reply_text(text=intro_text, parse_mode="Markdown", reply_markup=get_categories_keyboard())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg = (
        "📖 *Help Menu*\nSelect a primary category to access specific operations from the visual dashboard, then upload your file.\n\n"
        "💡 *Features Available Summary:*\n"
        "• Document Conversions (Word, PPTX, PDF, EPUB, Pages, ODT, CSV, Excel)\n"
        "• Image Formats & OCR Text Extraction\n"
        "• Audio processing engine & Text to Speech\n"
        "• Zip / Unzip tools\n"
    )

    if user_id == int(os.getenv("ADMIN_ID", 0)):
        msg += "\n🛠 *Admin Commands Available:* \n👉 /stats — View overall bot metrics\n👉 /users — View list of database users\n👉 /broadcast <msg> — Broadcast to users\n👉 /shutdown — Power down bot"

    if update.message:
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=get_categories_keyboard())
    elif update.callback_query:
        await update.callback_query.message.reply_text(msg, parse_mode="Markdown", reply_markup=get_categories_keyboard())

async def inline_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer(text="🔄 Processing layout...", show_alert=False)

    user_id = query.from_user.id
    track_user_db(user_id)

    data = query.data
    if data.startswith("cat_"):
        cat = data.replace("cat_", "")
        if cat == "back":
            await query.message.edit_text("👇 *Please select your desired file conversion protocol:*", reply_markup=get_categories_keyboard(), parse_mode="Markdown")
        else:
            titles = {"doc": "📄 Document Tools", "img": "🖼 Image Tools", "audio": "🎵 Audio Tools", "zip": "📦 Archive Utilities"}
            await query.message.edit_text(f"🛠 *{titles[cat]}*\nSelect the operational tool you wish to deploy:", reply_markup=get_category_tools_keyboard(cat), parse_mode="Markdown")

    elif data.startswith("mode_"):
        chosen_mode = data.replace("mode_", "")
        context.user_data["mode"] = chosen_mode

        if chosen_mode == "text2speech":
            await query.message.edit_text(text="📣 *Text to Speech Configuration*\n\nPlease select the desired speed for your generated audio:", parse_mode="Markdown", reply_markup=get_tts_speed_keyboard())
            return

        if chosen_mode == "feedback":
            await query.message.reply_text(text="✍️ *Submit System Feedback*\n\nPlease type your suggestions, feature requests, or issues directly into the chat now. I will immediately forward it securely to the bot admin team.", parse_mode="Markdown")
            return

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

    elif data.startswith("ttsspeed_"):
        speed_val = data.replace("ttsspeed_", "")
        context.user_data["tts_speed"] = speed_val

        speed_label = "Slow" if speed_val == "-15%" else "Normal"
        label = COMMANDS["text2speech"]["label"]
        input_fmt = COMMANDS["text2speech"]["input"]

        text = f"📥 *Selected:* {label} ({speed_label} Speed)\n\nPlease type or paste your raw **{input_fmt}** message directly into the chat. I will compile it into audio..."
        await query.message.reply_text(text=text, parse_mode="Markdown")

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")
    user_id = update.effective_user.id
    track_user_db(user_id)

    if not mode:
        await update.message.reply_text("❌ Select a command operation using the menu options first!", reply_markup=get_categories_keyboard())
        return

    if mode == "feedback":
        feedback_text = update.message.text
        if not feedback_text or feedback_text.startswith("/"):
            await update.message.reply_text("⚠️ Action canceled. Please provide a valid textual feedback message thread.")
            context.user_data["mode"] = None
            return

        admin_id = int(os.getenv("ADMIN_ID", 0))
        if admin_id != 0:
            user_info = f"👤 *New Feedback Received!*\n• From User: {update.effective_user.first_name}\n• User ID: `{user_id}`\n• Username: @{update.effective_user.username or 'None'}\n\n💬 *Message Body:*\n{feedback_text}"
            await context.bot.send_message(chat_id=admin_id, text=user_info, parse_mode="Markdown")
            await update.message.reply_text("✅ *Thank you! Your feedback message has been securely sent directly to the administrator.*", parse_mode="Markdown", reply_markup=get_categories_keyboard())
        else:
            await update.message.reply_text("❌ Configuration Error: Admin routing endpoint not connected on this server container instance.")
        context.user_data["mode"] = None
        return

    is_text_tts = (mode == "text2speech" and update.message.text and not update.message.text.startswith("/"))

    file_obj = None
    if not is_text_tts:
        file_obj = (
            update.message.document or 
            update.message.audio or 
            update.message.voice or 
            (update.message.photo[-1] if update.message.photo else None)
        )
        if not file_obj: return

        if file_obj.file_size > MAX_FILE_SIZE:
            await update.message.reply_text("⚠️ File size exceeds boundaries (Max 50MB allowed by Telegram Bot API).")
            return

        allowed, _ = check_quota(user_id, file_obj.file_size)
        if not allowed:
            await update.message.reply_text("🚫 Daily limit constraint reached! (100MB daily limit max).")
            return

    status_msg = await update.message.reply_text("⏳ `[▓░░░░░░░░░] 10%` *Downloading target data from cloud servers...*", parse_mode="Markdown")

    fname = None
    if is_text_tts:
        fname = "input_text.txt"
    else:
        tg_file = await file_obj.get_file()
        fname = getattr(file_obj, "file_name", None)
        if not fname:
            if update.message.audio or update.message.voice:
                fname = "audio.mp3"
            else:
                fname = "photo.jpg"

    await status_msg.edit_text("⚙️ `[▓▓▓▓▓▓░░░░] 60%` *Running conversion protocols...*", parse_mode="Markdown")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / fname
            if is_text_tts:
                input_path.write_text(update.message.text, encoding="utf-8")
            else:
                await tg_file.download_to_drive(custom_path=input_path)

            output_paths = await convert_file_async(mode, input_path, Path(tmp), context.user_data.get("tts_speed", "+0%"))
            await status_msg.edit_text("📤 `[▓▓▓▓▓▓▓▓▓▓] 100%` *Uploading outputs to Telegram...*", parse_mode="Markdown")

            for out in output_paths:
                with out.open("rb") as f:
                    if mode == "text2speech":
                        await update.message.reply_audio(audio=f, filename=out.name, title="Synthesized Audio")
                    else:
                        await update.message.reply_document(document=f, filename=out.name)

            await status_msg.delete()
            await update.message.reply_text("✅ *Conversion complete and successfully processed!*", parse_mode="Markdown", reply_markup=get_categories_keyboard())
    except Exception as e:
        await status_msg.edit_text(f"❌ *Engine Error raised during conversion operation:* \n`{str(e)}`", parse_mode="Markdown")

async def convert_file_async(mode, input_path, tmp_dir, tts_speed="+0%"):
    if mode == "text2speech":
        import edge_tts
        out = tmp_dir / "synthesized_speech.mp3"
        text_content = input_path.read_text(encoding="utf-8")
        communicate = edge_tts.Communicate(text_content, "en-US-GuyNeural", rate=tts_speed)
        await communicate.save(str(out))
        return [out]
    return await asyncio.to_thread(convert_file, mode, input_path, tmp_dir)

# --- HYBRID CONVERSION ROUTER ENGINE ---
def convert_file(mode, input_path, tmp_dir):
    file_size_mb = os.path.getsize(input_path) / (1024 * 1024)
    current_ram = psutil.virtual_memory().percent if psutil else 0.0
    available_ram_mb = (psutil.virtual_memory().available / (1024 * 1024)) if psutil else 500.0

    # 1. HEAVY FORMATS: Always offload heavy office/e-book rendering to Cloud APIs to avoid server OOM crash (>100MB RAM needed)
    HEAVY_CLOUD_MODES = {
        "docx2pdf", "doc2pdf", "doc2docx", "odt2pdf", "odt2docx",
        "pages2pdf", "pages2docx", "pptx2pdf", "ppt2pdf", "key2pdf",
        "xls2xlsx", "xls2pdf", "ods2xlsx", "ods2pdf", "epub2pdf",
        "mobi2pdf", "pdf2epub"
    }

    if mode in HEAVY_CLOUD_MODES:
        output_fmt = COMMANDS[mode]["output"].lower()
        logging.info(f"Routing heavy document mode '{mode}' -> '{output_fmt}' directly to Cloud API Router...")
        return [convert_via_cloud_apis(input_path, output_fmt, tmp_dir)]

    # 2. PDF to Text: Lightweight pure-python local extraction (<10MB RAM)
    if mode == "pdf2txt":
        try:
            return [convert_pdf_to_txt_locally(input_path, tmp_dir)]
        except Exception as err:
            logging.warning(f"Local PDF text extraction failed ({err}). Offloading to Cloud API Router...")
            return [convert_via_cloud_apis(input_path, "txt", tmp_dir)]

    # 3. EPUB to TXT: Lightweight local extraction (<15MB RAM)
    if mode == "epub2txt":
        try:
            out = tmp_dir / f"{input_path.stem}.txt"
            doc = fitz.open(input_path)
            extracted = []
            for page in doc:
                text = page.get_text()
                if text:
                    extracted.append(text)
            out.write_text("\n\n".join(extracted), encoding="utf-8")
            return [out]
        except Exception as err:
            logging.warning(f"Local EPUB text extraction failed ({err}). Offloading to Cloud API Router...")
            return [convert_via_cloud_apis(input_path, "txt", tmp_dir)]

    # 4. SAFETY DYNAMIC OFFLOAD: Large files, high RAM percent, or free RAM dropping under 100MB
    if file_size_mb > LOCAL_MAX_SIZE_MB or current_ram > SAFE_RAM_THRESHOLD_PERCENT or available_ram_mb < MIN_AVAILABLE_RAM_MB:
        if mode in COMMANDS and COMMANDS[mode]["output"] != "PNGs":
            output_fmt = COMMANDS[mode]["output"].lower()
            try:
                logging.info(f"Offloading task '{mode}' ({file_size_mb:.1f}MB, Free RAM: {available_ram_mb:.1f}MB) to Cloud API Router...")
                return [convert_via_cloud_apis(input_path, output_fmt, tmp_dir)]
            except Exception as api_err:
                logging.warning(f"Cloud API Router offload failed: {api_err}. Attempting local fallback...")

    # --- LOCAL CONVERSION FALLBACKS (<100MB RAM safe) ---
    if mode == "csv2xlsx":
        out = tmp_dir / f"{input_path.stem}.xlsx"
        df = pd.read_csv(input_path)
        df.to_excel(out, index=False, engine='openpyxl')
        return [out]

    if mode == "ocr":
        out = tmp_dir / "extracted_text.txt"
        try:
            with open(input_path, 'rb') as f:
                response = requests.post(
                    "https://api.ocr.space/parse/image",
                    files={"image": f},
                    data={"apikey": "helloworld", "language": "eng"}
                ).json()
            parsed_results = response.get("ParsedResults", [])
            text_result = parsed_results[0].get("ParsedText", "No readable text found via Engine API.") if parsed_results else "OCR API Execution error response."
        except Exception as api_err:
            text_result = f"Failed to reach target Text Extraction API: {str(api_err)}"
        out.write_text(text_result, encoding="utf-8")
        return [out]

    if mode == "pdf2docx":
        out = tmp_dir / "converted.docx"
        try:
            cv = Converter(str(input_path))
            cv.convert(str(out))
            cv.close()
            return [out]
        except Exception as local_err:
            logging.warning(f"Local pdf2docx failed ({local_err}). Offloading to Cloud API Router...")
            return [convert_via_cloud_apis(input_path, "docx", tmp_dir)]

    if mode == "img2pdf":
        out = tmp_dir / "converted.pdf"
        out.write_bytes(img2pdf.convert(str(input_path)))
        return [out]

    if mode in ["txt2pdf", "rtf2pdf"]:
        out = tmp_dir / "converted.pdf"
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        with open(input_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                pdf.cell(200, 10, text=line.strip(), new_x="LMARGIN", new_y="NEXT", align='L')
        pdf.output(str(out))
        return [out]

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
        
        # Security Fix: Prevent Path Traversal (Zip Slip)
        resolved_extract_dir = extract_dir.resolve()
        
        with zipfile.ZipFile(input_path, 'r') as zipf:
            for member in zipf.infolist():
                target_path = (extract_dir / member.filename).resolve()
                if not str(target_path).startswith(str(resolved_extract_dir)):
                    raise Exception("Security Error: Archive contains unsafe relative paths.")
            zipf.extractall(extract_dir)
            
        return [p for p in extract_dir.rglob('*') if p.is_file()]

    if mode in COMMANDS:
        output_fmt = COMMANDS[mode]["output"].lower()
        out = tmp_dir / f"converted.{output_fmt}"

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

        if output_fmt in ["wav", "mp3"]:
            subprocess.run(["ffmpeg", "-y", "-i", str(input_path), "-vn", str(out)], check=True)
            return [out]
            
    return []

# --- ADMIN PANEL FUNCTIONS ---
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != int(os.getenv("ADMIN_ID", 0)): return
    try:
        rows = execute_turso_query("SELECT COUNT(*) FROM users;")
        count = rows[0][0] if rows else 0
        await update.message.reply_text(f"📊 *Admin Metrics:* Total Registered Users = `{count}`", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to retrieve stats: {e}")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != int(os.getenv("ADMIN_ID", 0)): return
    try:
        rows = execute_turso_query("SELECT user_id, last_seen FROM users ORDER BY last_seen DESC;")
        if not rows:
            await update.message.reply_text("📁 No registered user tracking logs discovered inside cloud database.")
            return

        msg = "👥 *Database User Directory Logs:*\n\n"
        for idx, row in enumerate(rows, 1):
            msg += f"{idx}. ID: `{row[0]}` | Last Seen: `{row[1]}`\n"
            if len(msg) > 3500:
                msg += "\n...Truncated due to limits..."
                break
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to retrieve user logs: {e}")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != int(os.getenv("ADMIN_ID", 0)): return
    if not context.args:
        await update.message.reply_text("❌ Please format message string payload: `/broadcast Your text content here`", parse_mode="Markdown")
        return

    broadcast_msg = " ".join(context.args)
    try:
        users = execute_turso_query("SELECT user_id FROM users;")
        success, failure = 0, 0
        await update.message.reply_text(f"📢 Starting broadcast sequence to {len(users)} users...")

        for user in users:
            try:
                await context.bot.send_message(chat_id=int(user[0]), text=broadcast_msg, parse_mode="Markdown")
                success += 1
                await asyncio.sleep(0.05)
            except Exception:
                failure += 1
        await update.message.reply_text(f"✅ *Broadcast completed!*\n• Successful deliveries: `{success}`\n• Failed deliveries: `{failure}`", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to initiate broadcast query: {e}")

async def shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != int(os.getenv("ADMIN_ID", 0)): return
    await update.message.reply_text("🛑 *Power down execution payload received. Stopping application loops...*", parse_mode="Markdown")
    os._exit(0)

# --- MAIN RUNNER ---
def main():
    init_db()
    token = os.getenv("BOT_TOKEN")
    admin = os.getenv("ADMIN_ID")
    if not token or not admin:
        print("CRITICAL LOG ERROR: Missing BOT_TOKEN or ADMIN_ID environment variables!")
        return

    # Security/Hosting Fix: Dynamically bind to the PORT variable Render assigns.
    port = int(os.getenv("PORT", 8080))
    Thread(target=lambda: app.run(host="0.0.0.0", port=port), daemon=True).start()

    bot_app = Application.builder().token(token).build()

    # Handlers Configuration
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("help", help_cmd))
    bot_app.add_handler(CommandHandler("stats", stats))
    bot_app.add_handler(CommandHandler("users", users_cmd))
    bot_app.add_handler(CommandHandler("broadcast", broadcast))
    bot_app.add_handler(CommandHandler("shutdown", shutdown))
    bot_app.add_handler(CallbackQueryHandler(inline_button_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT | filters.Document.ALL | filters.PHOTO | filters.AUDIO | filters.VOICE, handle_file))

    print("Bot service initialization sequence success... Polling telegram API.")
    bot_app.run_polling()

if __name__ == "__main__":
    main()
