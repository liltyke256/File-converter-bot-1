import asyncio
import io
import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import fitz
import img2pdf
from pdf2docx import Converter
from PIL import Image
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

COMMANDS = {
    "pdf2docx": {
        "label": "PDF to Word",
        "input": "PDF",
        "output": "DOCX",
        "extensions": {".pdf"},
    },
    "docx2pdf": {
        "label": "Word to PDF",
        "input": "DOCX",
        "output": "PDF",
        "extensions": {".docx"},
    },
    "jpg2png": {
        "label": "JPG to PNG",
        "input": "JPG/JPEG image",
        "output": "PNG",
        "extensions": {".jpg", ".jpeg"},
    },
    "png2jpg": {
        "label": "PNG to JPG",
        "input": "PNG image",
        "output": "JPG",
        "extensions": {".png"},
    },
    "img2pdf": {
        "label": "Image to PDF",
        "input": "JPG, JPEG, or PNG image",
        "output": "PDF",
        "extensions": {".jpg", ".jpeg", ".png"},
    },
    "pdf2img": {
        "label": "PDF to Image",
        "input": "PDF",
        "output": "PNG image(s)",
        "extensions": {".pdf"},
    },
}

MAX_ZIP_FILES = 25
MAX_UNZIP_FILES = 25

logging.basicConfig(level=logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("mode", None)
    context.user_data.pop("zip_files", None)
    await update.message.reply_text(
        "👋 *File Converter Bot*\n\n"
        "Commands:\n"
        "/pdf2docx - PDF to Word\n"
        "/docx2pdf - Word to PDF\n"
        "/jpg2png - JPG to PNG\n"
        "/png2jpg - PNG to JPG\n"
        "/img2pdf - Image to PDF\n"
        "/pdf2img - PDF to Image\n"
        "/zip - Compress files into ZIP\n"
        "/unzip - Extract ZIP files\n"
        "/help - All formats\n\n"
        "Send a command first, then upload your file.",
        parse_mode="Markdown",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*All supported formats:*\n\n"
        "/pdf2docx - PDF → DOCX / Word\n"
        "/docx2pdf - DOCX / Word → PDF\n"
        "/jpg2png - JPG or JPEG → PNG\n"
        "/png2jpg - PNG → JPG\n"
        "/img2pdf - JPG, JPEG, or PNG → PDF\n"
        "/pdf2img - PDF → PNG image(s)\n"
        "/zip - Any uploaded files → ZIP archive\n"
        "/unzip - ZIP archive → extracted files\n\n"
        "For ZIP: tap /zip, upload one or more files, then tap /donezip.\n"
        "Usage: Tap a command first, then upload your file.",
        parse_mode="Markdown",
    )


async def set_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    command = update.message.text.removeprefix("/").split()[0].lower()
    context.user_data["mode"] = command
    context.user_data.pop("zip_files", None)
    details = COMMANDS[command]
    await update.message.reply_text(
        f"Send me the {details['input']} file to convert to {details['output']}."
    )


async def zip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "zip"
    context.user_data["zip_files"] = []
    await update.message.reply_text(
        "Send me the files you want to compress. When finished, send /donezip to receive the ZIP file."
    )


async def done_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = context.user_data.get("zip_files", [])
    if not files:
        await update.message.reply_text("No files added yet. Send /zip, upload files, then send /donezip.")
        return

    try:
        zip_bytes = await asyncio.to_thread(create_zip_archive, files)
        await update.message.reply_document(
            document=io.BytesIO(zip_bytes),
            filename="compressed-files.zip",
        )
        context.user_data.pop("mode", None)
        context.user_data.pop("zip_files", None)
        await update.message.reply_text("ZIP file created. Choose another command if you want to do more.")
    except Exception as exc:
        await update.message.reply_text(f"ZIP creation failed: {exc}")


async def unzip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "unzip"
    context.user_data.pop("zip_files", None)
    await update.message.reply_text("Send me the ZIP file you want to extract.")


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")
    if not mode:
        await update.message.reply_text("Please choose a command first, such as /pdf2docx, /zip, or /unzip.")
        return

    source = await get_uploaded_file(update)
    if source is None:
        await update.message.reply_text("Please upload a file or image after choosing a conversion command.")
        return

    telegram_file, filename = source

    if mode == "zip":
        await add_file_to_zip(update, context, telegram_file, filename)
        return

    if mode == "unzip":
        await extract_uploaded_zip(update, context, telegram_file, filename)
        return

    details = COMMANDS[mode]
    suffix = Path(filename).suffix.lower()

    if suffix and suffix not in details["extensions"]:
        expected = ", ".join(sorted(details["extensions"]))
        await update.message.reply_text(f"That file type is not supported for /{mode}. Expected: {expected}")
        return

    await update.message.reply_text("Converting your file now...")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / safe_filename(filename, mode)
            await telegram_file.download_to_drive(custom_path=input_path)
            output_paths = await asyncio.to_thread(convert_file, mode, input_path, Path(tmp))

            for output_path in output_paths:
                with output_path.open("rb") as converted_file:
                    await update.message.reply_document(document=converted_file, filename=output_path.name)

        context.user_data.pop("mode", None)
        await update.message.reply_text("Done. Choose another command if you want to convert more files.")
    except Exception as exc:
        await update.message.reply_text(f"Conversion failed: {exc}")


async def get_uploaded_file(update: Update):
    message = update.message
    if message.document:
        return await message.document.get_file(), message.document.file_name or "upload"
    if message.photo:
        photo = message.photo[-1]
        return await photo.get_file(), "photo.jpg"
    return None


async def add_file_to_zip(update: Update, context: ContextTypes.DEFAULT_TYPE, telegram_file, filename: str):
    files = context.user_data.setdefault("zip_files", [])
    if len(files) >= MAX_ZIP_FILES:
        await update.message.reply_text(f"You can add up to {MAX_ZIP_FILES} files to one ZIP. Send /donezip now.")
        return

    with tempfile.TemporaryDirectory() as tmp:
        input_path = Path(tmp) / safe_received_filename(filename)
        await telegram_file.download_to_drive(custom_path=input_path)
        files.append(
            {
                "filename": unique_archive_name([item["filename"] for item in files], input_path.name),
                "content": input_path.read_bytes(),
            }
        )

    await update.message.reply_text(
        f"Added {files[-1]['filename']} to ZIP ({len(files)} file(s)). Send more files or /donezip."
    )


async def extract_uploaded_zip(update: Update, context: ContextTypes.DEFAULT_TYPE, telegram_file, filename: str):
    if Path(filename).suffix.lower() != ".zip":
        await update.message.reply_text("Please upload a .zip file for /unzip.")
        return

    await update.message.reply_text("Extracting your ZIP file now...")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "upload.zip"
            await telegram_file.download_to_drive(custom_path=input_path)
            output_paths = await asyncio.to_thread(extract_zip_archive, input_path, Path(tmp))

            if not output_paths:
                await update.message.reply_text("That ZIP file did not contain any files to extract.")
                return

            for output_path in output_paths:
                with output_path.open("rb") as extracted_file:
                    await update.message.reply_document(document=extracted_file, filename=output_path.name)

        context.user_data.pop("mode", None)
        await update.message.reply_text("ZIP extracted. Choose another command if you want to do more.")
    except Exception as exc:
        await update.message.reply_text(f"Unzip failed: {exc}")


def safe_received_filename(filename: str) -> str:
    name = Path(filename or "upload").name
    return name or "upload"


def unique_archive_name(existing_names: list[str], filename: str) -> str:
    name = safe_received_filename(filename)
    if name not in existing_names:
        return name

    path = Path(name)
    stem = path.stem or "file"
    suffix = path.suffix
    counter = 2
    while True:
        candidate = f"{stem}-{counter}{suffix}"
        if candidate not in existing_names:
            return candidate
        counter += 1


def create_zip_archive(files: list[dict]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in files:
            archive.writestr(item["filename"], item["content"])
    return output.getvalue()


def extract_zip_archive(input_path: Path, tmp_dir: Path) -> list[Path]:
    output_dir = tmp_dir / "extracted"
    output_dir.mkdir()
    output_paths = []
    names = []

    with zipfile.ZipFile(input_path) as archive:
        file_members = [info for info in archive.infolist() if not info.is_dir()]
        if len(file_members) > MAX_UNZIP_FILES:
            raise RuntimeError(f"This ZIP contains {len(file_members)} files. The limit is {MAX_UNZIP_FILES} files.")

        for info in file_members:
            filename = unique_archive_name(names, Path(info.filename.replace("\\", "/")).name)
            if not filename:
                continue

            output_path = output_dir / filename
            with archive.open(info) as source, output_path.open("wb") as target:
                shutil.copyfileobj(source, target)
            output_paths.append(output_path)
            names.append(filename)

    return output_paths


def safe_filename(filename: str, mode: str) -> str:
    path = Path(filename)
    suffix = path.suffix.lower()
    if suffix:
        return f"input{suffix}"
    default_suffixes = {
        "pdf2docx": ".pdf",
        "docx2pdf": ".docx",
        "jpg2png": ".jpg",
        "png2jpg": ".png",
        "img2pdf": ".jpg",
        "pdf2img": ".pdf",
    }
    return f"input{default_suffixes[mode]}"


def convert_file(mode: str, input_path: Path, tmp_dir: Path) -> list[Path]:
    if mode == "pdf2docx":
        return [convert_pdf_to_docx(input_path, tmp_dir)]
    if mode == "docx2pdf":
        return [convert_docx_to_pdf(input_path, tmp_dir)]
    if mode == "jpg2png":
        return [convert_image(input_path, tmp_dir / "converted.png", "PNG")]
    if mode == "png2jpg":
        return [convert_image(input_path, tmp_dir / "converted.jpg", "JPEG")]
    if mode == "img2pdf":
        return [convert_image_to_pdf(input_path, tmp_dir / "converted.pdf")]
    if mode == "pdf2img":
        return convert_pdf_to_images(input_path, tmp_dir)
    raise ValueError("Unknown conversion command")


def convert_pdf_to_docx(input_path: Path, tmp_dir: Path) -> Path:
    output_path = tmp_dir / "converted.docx"
    converter = Converter(str(input_path))
    try:
        converter.convert(str(output_path), start=0, end=None)
    finally:
        converter.close()
    return output_path


def convert_docx_to_pdf(input_path: Path, tmp_dir: Path) -> Path:
    office = shutil.which("libreoffice") or shutil.which("soffice")
    if not office:
        raise RuntimeError("LibreOffice is not installed, so DOCX to PDF is unavailable.")

    result = subprocess.run(
        [office, "--headless", "--convert-to", "pdf", "--outdir", str(tmp_dir), str(input_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
        check=False,
    )
    output_path = tmp_dir / f"{input_path.stem}.pdf"
    if result.returncode != 0 or not output_path.exists():
        message = result.stderr.strip() or result.stdout.strip() or "LibreOffice conversion failed."
        raise RuntimeError(message)
    final_path = tmp_dir / "converted.pdf"
    output_path.rename(final_path)
    return final_path


def convert_image(input_path: Path, output_path: Path, output_format: str) -> Path:
    with Image.open(input_path) as image:
        if output_format == "JPEG":
            background = Image.new("RGB", image.size, "white")
            if image.mode in ("RGBA", "LA"):
                background.paste(image, mask=image.getchannel("A"))
            else:
                background.paste(image.convert("RGB"))
            background.save(output_path, output_format, quality=95)
        else:
            image.save(output_path, output_format)
    return output_path


def convert_image_to_pdf(input_path: Path, output_path: Path) -> Path:
    with Image.open(input_path) as image:
        if image.mode in ("RGBA", "LA"):
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            normalized_path = input_path.with_suffix(".normalized.jpg")
            background.save(normalized_path, "JPEG", quality=95)
            source = normalized_path
        else:
            source = input_path
    output_path.write_bytes(img2pdf.convert(str(source)))
    return output_path


def convert_pdf_to_images(input_path: Path, tmp_dir: Path) -> list[Path]:
    document = fitz.open(input_path)
    image_paths = []
    try:
        for page_number in range(document.page_count):
            page = document.load_page(page_number)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            image_path = tmp_dir / f"page-{page_number + 1}.png"
            pixmap.save(str(image_path))
            image_paths.append(image_path)
    finally:
        document.close()

    if len(image_paths) <= 10:
        return image_paths

    zip_path = tmp_dir / "converted-images.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for image_path in image_paths:
            archive.write(image_path, arcname=image_path.name)
    return [zip_path]


def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN secret is required. Add your Telegram bot token before starting the bot.")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("zip", zip_cmd))
    app.add_handler(CommandHandler("donezip", done_zip))
    app.add_handler(CommandHandler("unzip", unzip_cmd))

    for command in COMMANDS:
        app.add_handler(CommandHandler(command, set_mode))

    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))

    print("Bot running...", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()
