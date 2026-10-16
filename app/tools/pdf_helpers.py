"""
app/tools/pdf_helpers.py

Helper functions for the "Combine to PDF" tool. Each `*_to_pdf` helper turns
one uploaded file into a single (or multi-page) intermediate PDF; the
orchestrator `combine_files_to_pdf` stitches all of those together in the
user's chosen order and optionally stamps a header/footer on the result.

Design notes:
  - Images use img2pdf (lossless, very fast - no re-encoding of the image).
  - Video frames are pulled with ffmpeg (same tool the rest of SNPro relies
    on for video work) and then piped through the same image_to_pdf path.
  - Text files are rendered with reportlab so long files wrap and paginate
    properly instead of being crammed onto one page.
  - Existing PDFs are merged as-is via pypdf (page order/rotation preserved).
  - Header/footer is applied as a final pass over the merged PDF using a
    one-page reportlab overlay per page, merged with pypdf - this is the
    standard, dependency-light way to stamp text onto an existing PDF.
"""

import io
import json
import os
import subprocess

import img2pdf
from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4, LETTER, LEGAL, landscape, portrait
from reportlab.pdfgen import canvas

# ---------------------------------------------------------------------------
# Page size / orientation helpers
# ---------------------------------------------------------------------------

PAGE_SIZES = {
    "A4": A4,
    "Letter": LETTER,
    "Legal": LEGAL,
}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv"}
TEXT_EXTS = {".txt", ".md", ".csv", ".json"}
PDF_EXTS = {".pdf"}


def resolve_page_size(page_size_name, orientation, custom_size=None):
    """
    page_size_name: one of PAGE_SIZES keys, or "Custom".
    orientation: "portrait" or "landscape".
    custom_size: (width_pt, height_pt) tuple, required if page_size_name == "Custom".
    Returns a (width, height) tuple in points.
    """
    if page_size_name == "Custom":
        if not custom_size:
            raise ValueError("custom_size is required when page_size_name is 'Custom'.")
        base = tuple(custom_size)
    else:
        base = PAGE_SIZES.get(page_size_name, A4)

    return landscape(base) if orientation == "landscape" else portrait(base)


def get_file_type(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in TEXT_EXTS:
        return "text"
    if ext in PDF_EXTS:
        return "pdf"
    return None


# ---------------------------------------------------------------------------
# Image -> PDF
# ---------------------------------------------------------------------------

def image_to_pdf(image_path, page_size, output_path, fit_mode="contain"):
    """
    Convert a single image into a one-page PDF sized to `page_size`
    (width_pt, height_pt), preserving aspect ratio ("contain" - the whole
    image fits inside the page with margins, no cropping).

    GIFs (possibly animated) and images with unusual modes are normalized
    to RGB first since img2pdf can't embed every PIL mode directly.
    """
    width_pt, height_pt = page_size
    normalized_path = None

    try:
        with Image.open(image_path) as im:
            # Use first frame for animated GIFs, normalize mode for img2pdf.
            if getattr(im, "is_animated", False):
                im.seek(0)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
                normalized_path = image_path + "._normalized.jpg"
                im.save(normalized_path, "JPEG", quality=95)

        source_path = normalized_path or image_path

        layout_fun = img2pdf.get_layout_fun(
            (img2pdf.mm_to_pt(1) * 0 + width_pt, height_pt)  # explicit page size in pt
        )
        pdf_bytes = img2pdf.convert(source_path, layout_fun=layout_fun)

        with open(output_path, "wb") as f:
            f.write(pdf_bytes)

        return output_path
    finally:
        if normalized_path and os.path.exists(normalized_path):
            os.remove(normalized_path)


# ---------------------------------------------------------------------------
# Video -> PDF (thumbnail frame)
# ---------------------------------------------------------------------------

def extract_video_frame(video_path, out_jpg_path, timestamp=1.0):
    """
    Grab a single representative frame from the video with ffmpeg.
    Falls back to the very first frame (timestamp=0) if seeking past the
    requested timestamp fails (e.g. very short clips).
    """
    def _run(ts):
        cmd = [
            "ffmpeg", "-y", "-ss", str(ts), "-i", video_path,
            "-frames:v", "1", "-qscale:v", "2", out_jpg_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)

    try:
        _run(timestamp)
        if not os.path.exists(out_jpg_path):
            raise RuntimeError("ffmpeg produced no frame.")
    except Exception:
        # Fallback: first frame of the video.
        _run(0)
        if not os.path.exists(out_jpg_path):
            raise RuntimeError(f"Could not extract a frame from {os.path.basename(video_path)}.")

    return out_jpg_path


def video_to_pdf_thumbnail(video_path, page_size, output_path, tmp_dir, timestamp=1.0):
    """
    Extract a frame from the video and lay it out as a single PDF page,
    with a small caption noting this page represents a video file.
    """
    frame_path = os.path.join(tmp_dir, f"_frame_{os.path.basename(video_path)}.jpg")
    extract_video_frame(video_path, frame_path, timestamp=timestamp)

    try:
        width_pt, height_pt = page_size
        c = canvas.Canvas(output_path, pagesize=page_size)

        with Image.open(frame_path) as im:
            img_w, img_h = im.size

        margin = 36  # 0.5"
        caption_h = 20
        avail_w = width_pt - 2 * margin
        avail_h = height_pt - 2 * margin - caption_h

        scale = min(avail_w / img_w, avail_h / img_h)
        draw_w, draw_h = img_w * scale, img_h * scale
        x = (width_pt - draw_w) / 2
        y = margin + caption_h + (avail_h - draw_h) / 2

        c.drawImage(frame_path, x, y, width=draw_w, height=draw_h, preserveAspectRatio=True)
        c.setFont("Helvetica-Oblique", 8)
        c.drawCentredString(width_pt / 2, margin / 2, f"Video: {os.path.basename(video_path)}")
        c.showPage()
        c.save()

        return output_path
    finally:
        if os.path.exists(frame_path):
            os.remove(frame_path)


# ---------------------------------------------------------------------------
# Text file -> PDF
# ---------------------------------------------------------------------------

def _read_text_content(text_path):
    """Read .txt/.md/.csv/.json as plain text. JSON is pretty-printed;
    everything else is shown verbatim, monospaced."""
    ext = os.path.splitext(text_path)[1].lower()
    with open(text_path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()

    if ext == ".json":
        try:
            raw = json.dumps(json.loads(raw), indent=2)
        except (ValueError, TypeError):
            pass  # not valid JSON - fall back to showing it as-is

    return raw


def text_to_pdf(text_path, page_size, output_path, font_size=10, title=None):
    """
    Render a text-like file as a paginated, monospaced PDF, wrapping long
    lines so nothing runs off the page.
    """
    content = _read_text_content(text_path)
    width_pt, height_pt = page_size
    margin = 40

    c = canvas.Canvas(output_path, pagesize=page_size)
    font_name = "Courier"
    line_height = font_size * 1.25
    max_width = width_pt - 2 * margin
    chars_per_line = max(10, int(max_width / (font_size * 0.6)))

    y = height_pt - margin

    if title:
        c.setFont("Helvetica-Bold", font_size + 2)
        c.drawString(margin, y, title)
        y -= line_height * 1.5

    c.setFont(font_name, font_size)

    def new_page():
        nonlocal y
        c.showPage()
        c.setFont(font_name, font_size)
        y = height_pt - margin

    for raw_line in content.splitlines() or [""]:
        # Wrap long lines instead of clipping them.
        segments = [raw_line[i:i + chars_per_line] for i in range(0, len(raw_line), chars_per_line)] or [""]
        for seg in segments:
            if y < margin:
                new_page()
            c.drawString(margin, y, seg)
            y -= line_height

    c.save()
    return output_path


# ---------------------------------------------------------------------------
# Merge existing PDFs
# ---------------------------------------------------------------------------

def merge_pdfs(pdf_list, output_path):
    """Concatenate a list of PDF file paths, in order, into one PDF."""
    writer = PdfWriter()
    for pdf_path in pdf_list:
        reader = PdfReader(pdf_path)
        for page in reader.pages:
            writer.add_page(page)

    with open(output_path, "wb") as f:
        writer.write(f)

    return output_path


# ---------------------------------------------------------------------------
# Header / footer stamping
# ---------------------------------------------------------------------------

def create_header_footer(pdf_path, output_path, header_text=None, footer_text=None,
                          page_numbers=True, date_text=None):
    """
    Stamp a header and/or footer (and optional page numbers / date) onto
    every page of an existing PDF, preserving each page's own size.
    """
    reader = PdfReader(pdf_path)
    writer = PdfWriter()
    total_pages = len(reader.pages)

    for i, page in enumerate(reader.pages, start=1):
        page_w = float(page.mediabox.width)
        page_h = float(page.mediabox.height)

        overlay_buf = io.BytesIO()
        c = canvas.Canvas(overlay_buf, pagesize=(page_w, page_h))

        if header_text:
            c.setFont("Helvetica-Bold", 10)
            c.drawString(36, page_h - 28, header_text)
            if date_text:
                c.setFont("Helvetica", 8)
                c.drawRightString(page_w - 36, page_h - 28, date_text)
            c.setLineWidth(0.5)
            c.line(36, page_h - 34, page_w - 36, page_h - 34)

        if footer_text or page_numbers:
            c.setFont("Helvetica", 8)
            c.line(36, 30, page_w - 36, 30)
            if footer_text:
                c.drawString(36, 18, footer_text)
            if page_numbers:
                c.drawRightString(page_w - 36, 18, f"Page {i} of {total_pages}")

        c.save()
        overlay_buf.seek(0)

        overlay_reader = PdfReader(overlay_buf)
        page.merge_page(overlay_reader.pages[0])
        writer.add_page(page)

    with open(output_path, "wb") as f:
        writer.write(f)

    return output_path


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def combine_files_to_pdf(ordered_file_paths, options, work_dir, output_path, progress_cb=None):
    """
    Convert every file in `ordered_file_paths` (already in the user's chosen
    order) into an intermediate PDF, merge them, then optionally stamp a
    header/footer. Returns the final output PDF path.

    options: {
        "page_size": "A4" | "Letter" | "Legal" | "Custom",
        "custom_size": (w_pt, h_pt),        # only if page_size == "Custom"
        "orientation": "portrait" | "landscape",
        "header_text": str | None,
        "footer_text": str | None,
        "page_numbers": bool,
        "date_text": str | None,
    }
    progress_cb: optional callable(done_count, total_count) for progress updates.
    """
    os.makedirs(work_dir, exist_ok=True)

    page_size = resolve_page_size(
        options.get("page_size", "A4"),
        options.get("orientation", "portrait"),
        options.get("custom_size"),
    )

    intermediate_pdfs = []
    total = len(ordered_file_paths)
    skipped = []

    for idx, file_path in enumerate(ordered_file_paths, start=1):
        file_type = get_file_type(file_path)
        base = os.path.splitext(os.path.basename(file_path))[0]
        intermediate_path = os.path.join(work_dir, f"_part_{idx:03d}_{base}.pdf")

        try:
            if file_type == "image":
                image_to_pdf(file_path, page_size, intermediate_path)
            elif file_type == "video":
                video_to_pdf_thumbnail(file_path, page_size, intermediate_path, work_dir)
            elif file_type == "text":
                text_to_pdf(file_path, page_size, intermediate_path, title=os.path.basename(file_path))
            elif file_type == "pdf":
                intermediate_path = file_path  # merge directly, no conversion needed
            else:
                skipped.append(os.path.basename(file_path))
                continue

            intermediate_pdfs.append(intermediate_path)
        except Exception as e:
            skipped.append(f"{os.path.basename(file_path)} ({e})")

        if progress_cb:
            progress_cb(idx, total)

    if not intermediate_pdfs:
        raise RuntimeError("None of the uploaded files could be converted. " +
                            ("Skipped: " + "; ".join(skipped) if skipped else ""))

    merged_path = os.path.join(work_dir, "_merged.pdf")
    merge_pdfs(intermediate_pdfs, merged_path)

    header_text = options.get("header_text")
    footer_text = options.get("footer_text")
    page_numbers = options.get("page_numbers", True)
    date_text = options.get("date_text")

    if header_text or footer_text or page_numbers:
        create_header_footer(
            merged_path, output_path,
            header_text=header_text, footer_text=footer_text,
            page_numbers=page_numbers, date_text=date_text,
        )
    else:
        os.replace(merged_path, output_path)

    return output_path, skipped