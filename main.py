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
import requests
from pathlib import Path
from threading import Thread

from flask import Flask
import img2pdf
import pymupdf as fitz

sys.modules["fitz"] = fitz

import pandas as pd  # Added for CSV conversion
from fpdf import FPDF
from pdf2docx import Converter
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters

# Setup basic logging to see issues in Railway logs
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- DATABASE SETUP ---
DB_FILE = "bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, last_seen TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS usage (user_id INTEGER, day TEXT, bytes_sent INTEGER, PRIMARY KEY (user_id, day))''')
    # Referral Tracking Table
    c.execute('''CREATE TABLE IF NOT EXISTS referrals (referrer_id INTEGER, referee_id INTEGER PRIMARY KEY)''')
    conn.commit()
    conn.close()

def track_user_db(user_id, referrer_id=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    today = str(datetime.date.today())
    c.execute("INSERT OR REPLACE INTO users (user_id, last_seen) VALUES (?, ?)", (user_id, today))
    
    if referrer_id and int(referrer_id) != user_id:
        try:
            c.execute("INSERT INTO referrals (referrer_id, referee_id) VALUES (?, ?)", (int(referrer_id), user_id))
        except sqlite3.IntegrityError:
            pass  # Already referred by someone or tracked
            
    conn.commit()
    conn.close()

def get_referral_count(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,))
    count = c.fetchone()[0]
    conn.close()
    return count

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
    # Documents
    "pdf2docx": {"label": "PDF to Word", "input": "PDF", "output": "DOCX", "extensions": {".pdf"}, "cat": "doc"},
    "docx2pdf": {"label": "Word to PDF", "input": "DOCX", "output": "PDF", "extensions": {".docx"}, "cat": "doc"},
    "txt2pdf": {"label": "Text to PDF", "input": "TXT", "output": "PDF", "extensions": {".txt"}, "cat": "doc"},
    "csv2xlsx": {"label": "CSV to Excel", "input": "CSV", "output": "XLSX", "extensions": {".csv"}, "cat": "doc"},

    # Images
    "jpg2png": {"label": "JPG to PNG", "input": "JPG/JPEG", "output": "PNG", "extensions": {".jpg", ".jpeg"}, "cat": "img"},
    "png2jpg": {"label": "PNG to JPG", "input": "PNG", "output": "JPG", "extensions": {".png"}, "cat": "img"},
    "img2pdf": {"label": "Image to PDF", "input": "JPG/PNG", "output": "PDF", "extensions": {".jpg", ".jpeg", ".png"}, "cat": "img"},
    "heic2jpg": {"label": "HEIC to JPG", "input": "HEIC", "output": "JPG", "extensions": {".heic"}, "cat": "img"},
    "gif2png": {"label": "GIF to PNG", "input": "GIF", "output": "PNG", "extensions": {".gif"}, "cat": "img"},
    "pdf2img": {"label": "PDF to Image", "input": "PDF", "output": "PNGs", "extensions": {".pdf"}, "cat": "img"},
    "ocr": {"label": "Image to Text (OCR) 🔒(1 Invite)", "input": "Image", "output": "TXT", "extensions": {".jpg", ".jpeg", ".png"}, "cat": "img"},

    # Audio
    "mp32wav": {"label": "MP3 to WAV", "input": "MP3", "output": "WAV", "extensions": {".mp3"}, "cat": "audio"},
    "wav2mp3": {"label": "WAV to MP3", "input": "WAV", "output": "MP3", "extensions": {".wav"}, "cat": "audio"},
    "m4a2mp3": {"label": "M4A to MP3", "input": "M4A", "output": "MP3", "extensions": {".m4a"}, "cat": "audio"},
    "flac2mp3": {"label": "FLAC to MP3", "input": "FLAC", "output": "MP3", "extensions": {".flac"}, "cat": "audio"},
    "ogg2mp3": {"label": "OGG to MP3", "input": "OGG", "output": "MP3", "extensions": {".ogg"}, "cat": "audio"},

    # Video
    "mp42mp3": {"label": "Video to Audio 🔒(2 Invites)", "input": "MP4", "output": "MP3", "extensions": {".mp4"}, "cat": "video"},
}
MAX_FILE_SIZE = 20 * 1024 * 1024 # 20MB

app = Flask("")
@app.route("/")
def home(): return "Bot Online"

# --- UI KEYBOARD CREATORS ---
def get_categories_keyboard():
    keyboard = [
        [InlineKeyboardButton("📄 Documents", callback_data="cat_doc"), InlineKeyboardButton("🖼 Images", callback_data="cat_img")],
        [InlineKeyboardButton("🎵 Audio", callback_data="cat_audio"), InlineKeyboardButton("🎥 Video tools", callback_data="cat_video")],
        [InlineKeyboardButton("📦 Archive Utilities", callback_data="cat_zip"), InlineKeyboardButton("🔍 Search Tools", callback_data="cat_search")],
        [InlineKeyboardButton("👥 My Referral Link", callback_data="ui_invite")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_category_tools_keyboard(category):
    keyboard = []
    if category in ["doc", "img", "audio", "video"]:
        keys = [k for k, v in COMMANDS.items() if v["cat"] == category]
        for i in range(0, len(keys), 2):
            row = [InlineKeyboardButton(COMMANDS[keys[i]]["label"], callback_data=f"mode_{keys[i]}")]
            if i + 1 < len(keys):
                row.append(InlineKeyboardButton(COMMANDS[keys[i+1]]["label"], callback_data=f"mode_{keys[i+1]}"))
            keyboard.append(row)
    elif category == "zip":
        keyboard.append([InlineKeyboardButton("📦 Create ZIP", callback_data="mode_zip"), InlineKeyboardButton("🔓 Extract ZIP", callback_data="mode_unzip")])
    elif category == "search":
        keyboard.append([InlineKeyboardButton("🌐 Wikipedia Search", callback_data="ui_wiki")])

    keyboard.append([InlineKeyboardButton("⬅️ Back to Categories", callback_data="cat_back")])
    return InlineKeyboardMarkup(keyboard)

# --- CORE FUNCTIONS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Handle incoming referral parameter from link
    referrer_id = None
    if context.args and context.args[0].isdigit():
        referrer_id = context.args[0]
        
    track_user_db(user.id, referrer_id)
    invites = get_referral_count(user.id)

    intro_text = (
        f"🚀 **Welcome to your ultimate File Converter Bot, {user.first_name}!**\n\n"
        "Transform documents, conversions, and files instantly using the structured dashboard below.\n\n"
        "⚙️ **System Limits:**\n"
        "• Max file upload: **20MB**\n"
        "• Daily bandwidth cap: **30MB**\n"
        f"• Your total referrals: **{invites}**\n\n"
        "👇 *Please select a category to view supported conversions:* "
    )

    await update.message.reply_text(
        text=intro_text,
        parse_mode="Markdown",
        reply_markup=get_categories_keyboard()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg = (
        "📖 *Help Menu*\nSelect a primary category to access specific operations from the visual dashboard, then upload your file.\n\n"
        "💡 *Features Available Summary:*\n"
        "• Document Conversions (Word, PDF, TXT, CSV)\n"
        "• Image Formats & OCR Text Extraction\n"
        "• Audio processing engine\n"
        "• Video extraction tools\n"
        "• Zip / Unzip tools\n"
        "• /wiki <query> — Instantly search Wikipedia items\n"
    )

    if user_id == int(os.getenv("ADMIN_ID", 0)):
        msg
