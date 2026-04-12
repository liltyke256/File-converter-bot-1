# File Converter Bot

## Overview
A Python Telegram bot that converts uploaded files after the user selects a conversion command. It supports PDF to DOCX, DOCX to PDF, JPG to PNG, PNG to JPG, image to PDF, PDF to PNG images, compressing multiple uploaded files into ZIP archives, extracting ZIP archives back into files, admin-only user stats/user lists/broadcasts/restarts, and a Flask keep-alive endpoint.

## Project Structure
- `main.py` - Telegram bot command handlers, upload handling, conversion functions, ZIP creation, ZIP extraction, user tracking, and admin commands.
- `pyproject.toml` - Python dependency list.
- `.replit` - Python entrypoint and deployment command.

## Runtime Configuration
- Requires a `BOT_TOKEN` secret containing the Telegram bot token from BotFather.
- Requires an `ADMIN_ID` secret containing the numeric Telegram user ID allowed to use admin commands.
- The bot runs as a long-lived console workflow with `python3 main.py`.
- A Flask keep-alive server runs on port 8080 and returns `Bot is alive!` at `/`.

## Bot Commands
- `/pdf2docx` - PDF to DOCX.
- `/docx2pdf` - DOCX to PDF.
- `/jpg2png` - JPG/JPEG to PNG.
- `/png2jpg` - PNG to JPG.
- `/img2pdf` - JPG/JPEG/PNG to PDF.
- `/pdf2img` - PDF to PNG image(s).
- `/zip` - Start collecting uploaded files into a ZIP archive.
- `/donezip` - Finish ZIP collection and receive the archive.
- `/unzip` - Upload a ZIP archive and receive extracted files.
- `/stats` - Admin-only total tracked users.
- `/users` - Admin-only list of tracked user IDs and last `/start` dates.
- `/broadcast <message>` - Admin-only message broadcast to tracked users.
- `/restart` - Admin-only bot process restart.

## Dependencies
- `python-telegram-bot` for Telegram polling and handlers.
- `Flask` for the keep-alive HTTP endpoint.
- `Pillow`, `img2pdf`, `pdf2docx`, and `pymupdf` for file conversion.
- `replit` for key-value user tracking.
- LibreOffice system package for DOCX to PDF conversion.
