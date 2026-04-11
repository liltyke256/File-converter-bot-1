# File Converter Bot

## Overview
A Python Telegram bot that converts uploaded files after the user selects a conversion command. It supports PDF to DOCX, DOCX to PDF, JPG to PNG, PNG to JPG, image to PDF, and PDF to PNG images.

## Project Structure
- `main.py` - Telegram bot command handlers, upload handling, and conversion functions.
- `pyproject.toml` - Python dependency list.
- `.replit` - Python entrypoint and deployment command.

## Runtime Configuration
- Requires a `BOT_TOKEN` secret containing the Telegram bot token from BotFather.
- The bot runs as a long-lived console workflow with `python3 main.py`.

## Dependencies
- `python-telegram-bot` for Telegram polling and handlers.
- `Pillow`, `img2pdf`, `pdf2docx`, and `pymupdf` for file conversion.
- LibreOffice system package for DOCX to PDF conversion.
