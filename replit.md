# File Converter Bot

## Overview
A Python Telegram bot that converts uploaded files after the user selects a conversion command. It supports PDF to DOCX, DOCX to PDF, JPG to PNG, PNG to JPG, image to PDF, PDF to PNG images, compressing multiple uploaded files into ZIP archives, and extracting ZIP archives back into files.

## Project Structure
- `main.py` - Telegram bot command handlers, upload handling, conversion functions, ZIP creation, and ZIP extraction.
- `pyproject.toml` - Python dependency list.
- `.replit` - Python entrypoint and deployment command.

## Runtime Configuration
- Requires a `BOT_TOKEN` secret containing the Telegram bot token from BotFather.
- The bot runs as a long-lived console workflow with `python3 main.py`.

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

## Dependencies
- `python-telegram-bot` for Telegram polling and handlers.
- `Pillow`, `img2pdf`, `pdf2docx`, and `pymupdf` for file conversion.
- LibreOffice system package for DOCX to PDF conversion.
