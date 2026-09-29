import os
import shutil
import uuid
import io
import qrcode
import json
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    jsonify, send_from_directory, current_app, abort, send_file
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from urllib.parse import urlparse

from app.tools.editing_helpers import TEMPLATES_DIR
from app.tools.pdf_helpers import IMAGE_EXTS, PDF_EXTS, TEXT_EXTS, VIDEO_EXTS
from extensions import db, limiter
from models import Job
from config import user_dir
from app.tools import tasks
from app.main.routes import TOOLS
from app.tools.speech_helpers import (
    SUPPORTED_LANGUAGES,
    allowed_audio_file,
    MAX_UPLOAD_MB,
)
from app.tools.tasks import translate_speech_task

tools_bp = Blueprint("tools", __name__, url_prefix="/tools")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ext_ok(filename, allowed):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed


def _save_upload(file_storage, tool, subfolder="uploads"):
    root = current_app.config["STORAGE_ROOT"]
    dest_dir = user_dir(root, current_user.id, subfolder, tool)
    fname = f"{uuid.uuid4().hex}_{secure_filename(file_storage.filename)}"
    path = os.path.join(dest_dir, fname)
    file_storage.save(path)
    return path


def _new_job(tool, label=""):
    job = Job(user_id=current_user.id, tool=tool, status="pending", input_label=label)
    db.session.add(job)
    db.session.commit()
    return job


def _active_job_limit_ok():
    limit = int(os.environ.get("MAX_ACTIVE_JOBS_PER_USER", 3))
    count = Job.query.filter(
        Job.user_id == current_user.id,
        Job.status.in_(("pending", "running"))
    ).count()
    return count < limit


def _safe_redirect_url(url, fallback):
    """Prevent open redirects by rejecting absolute URLs."""
    if not url:
        return fallback
    parsed = urlparse(url)
    if parsed.netloc:
        return fallback
    return url


# ---------------------------------------------------------------------------
# 1. Video downloader
# ---------------------------------------------------------------------------
@tools_bp.route("/video-downloader", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per minute", methods=["POST"])
def video_downloader():
    if request.method == "POST":
        if not _active_job_limit_ok():
            flash("You already have N active jobs. Please wait for one to finish.", "warning")
            return redirect(url_for("tools.video_downloader"))
        url = request.form.get("url", "").strip()
        mode = request.form.get("mode", "video")
        if not url:
            flash("Please paste a valid video URL.", "warning")
            return redirect(url_for("tools.video_downloader"))

        job = _new_job("video_download", label=url[:120])
        out_dir = user_dir(current_app.config["STORAGE_ROOT"], current_user.id, "outputs", "video_download", str(job.id))
        result = tasks.task_download_video.delay(job.id, url, mode, out_dir)
        job.task_id = result.id
        db.session.commit()
        return redirect(url_for("tools.job_view", job_id=job.id))

    return render_template("tools/video_downloader.html")


# ---------------------------------------------------------------------------
# 2. Speech to text
# ---------------------------------------------------------------------------
@tools_bp.route("/speech-to-text", methods=["GET", "POST"])
@login_required
def speech_to_text():
    if request.method == "POST":
        if not _active_job_limit_ok():
            flash("You already have N active jobs. Please wait for one to finish.", "warning")
            return redirect(url_for("tools.speech_to_text"))
        file = request.files.get("audio")
        language = request.form.get("language", "en-US")

        if not file or file.filename == "":
            flash("Please choose an audio file.", "warning")
            return redirect(url_for("tools.speech_to_text"))

        allowed_exts = current_app.config.get("ALLOWED_AUDIO_EXT", {"mp3", "wav", "m4a", "ogg", "flac"})
        ext = file.filename.rsplit(".", 1)[1].lower() if "." in file.filename else ""
        if ext not in allowed_exts:
            flash(f"Unsupported audio format: '{ext}'. Allowed: {', '.join(sorted(allowed_exts))}", "danger")
            return redirect(url_for("tools.speech_to_text"))

        path = _save_upload(file, "speech_to_text")
        job = _new_job("speech_to_text", label=file.filename)
        out_dir = user_dir(current_app.config["STORAGE_ROOT"], current_user.id, "outputs", "speech_to_text", str(job.id))
        result = tasks.task_speech_to_text.delay(job.id, path, language, out_dir)
        job.task_id = result.id
        db.session.commit()
        return redirect(url_for("tools.job_view", job_id=job.id))

    languages = {"English": "en-US", "Khmer": "km-KH", "French": "fr-FR", "Spanish": "es-ES", "German": "de-DE"}
    return render_template("tools/speech_to_text.html", languages=languages)


# ---------------------------------------------------------------------------
# 3. Video to frames
# ---------------------------------------------------------------------------
@tools_bp.route("/video-to-frames", methods=["GET", "POST"])
@login_required
def video_to_frames():
    if request.method == "POST":
        if not _active_job_limit_ok():
            flash("You already have N active jobs. Please wait for one to finish.", "warning")
            return redirect(url_for("tools.video_to_frames"))
        file = request.files.get("video")
        frame_rate = float(request.form.get("frame_rate", 1))
        if not file or file.filename == "":
            flash("Please choose a video file.", "warning")
            return redirect(url_for("tools.video_to_frames"))
        if not _ext_ok(file.filename, current_app.config["ALLOWED_VIDEO_EXT"]):
            flash("Unsupported video format.", "danger")
            return redirect(url_for("tools.video_to_frames"))

        path = _save_upload(file, "video_to_frames")
        job = _new_job("video_to_frames", label=file.filename)
        out_dir = user_dir(current_app.config["STORAGE_ROOT"], current_user.id, "outputs", "video_to_frames", str(job.id))
        result = tasks.task_video_to_frames.delay(job.id, path, frame_rate, out_dir)
        job.task_id = result.id
        db.session.commit()
        return redirect(url_for("tools.job_view", job_id=job.id))

    return render_template("tools/video_to_frames.html")


# ---------------------------------------------------------------------------
# 4. PDF to Word
# ---------------------------------------------------------------------------
@tools_bp.route("/pdf-to-word", methods=["GET", "POST"])
@login_required
def pdf_to_word():
    if request.method == "POST":
        if not _active_job_limit_ok():
            flash("You already have N active jobs. Please wait for one to finish.", "warning")
            return redirect(url_for("tools.pdf_to_word"))
        file = request.files.get("pdf")
        if not file or file.filename == "":
            flash("Please choose a PDF file.", "warning")
            return redirect(url_for("tools.pdf_to_word"))
        if not _ext_ok(file.filename, current_app.config["ALLOWED_PDF_EXT"]):
            flash("Please upload a .pdf file.", "danger")
            return redirect(url_for("tools.pdf_to_word"))

        path = _save_upload(file, "pdf_to_word")
        job = _new_job("pdf_to_word", label=file.filename)
        out_dir = user_dir(current_app.config["STORAGE_ROOT"], current_user.id, "outputs", "pdf_to_word", str(job.id))
        result = tasks.task_pdf_to_word.delay(job.id, path, out_dir)
        job.task_id = result.id
        db.session.commit()
        return redirect(url_for("tools.job_view", job_id=job.id))

    return render_template("tools/pdf_to_word.html")


# ---------------------------------------------------------------------------
# 5. Images to video (AI‑enhanced)
# ---------------------------------------------------------------------------
@tools_bp.route("/images-to-video", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per minute", methods=["POST"])
def images_to_video():
    if request.method == "POST":
        if not _active_job_limit_ok():
            flash("You already have N active jobs. Please wait for one to finish.", "warning")
            return redirect(url_for("tools.images_to_video"))
        files = request.files.getlist("images")
        audio_file = request.files.get("audio")
        fps = int(request.form.get("fps", 1))
        use_ken_burns = request.form.get("ken_burns") == "on"
        script = request.form.get("script", "").strip()
        voice_id = request.form.get("voice_id") or None
        voice_lang = request.form.get("voice_lang", "en")

        files = [f for f in files if f and f.filename]
        if len(files) < 2:
            flash("Please choose at least 2 images.", "warning")
            return redirect(url_for("tools.images_to_video"))

        job = _new_job("images_to_video", label=f"{len(files)} images")
        images_dir = user_dir(
            current_app.config["STORAGE_ROOT"], current_user.id, "uploads", "images_to_video", str(job.id)
        )
        for f in files:
            if _ext_ok(f.filename, current_app.config["ALLOWED_IMAGE_EXT"]):
                f.save(os.path.join(images_dir, secure_filename(f.filename)))

        audio_path = None
        if audio_file and audio_file.filename:
            audio_path = os.path.join(images_dir, "_audio_" + secure_filename(audio_file.filename))
            audio_file.save(audio_path)

        out_dir = user_dir(
            current_app.config["STORAGE_ROOT"], current_user.id, "outputs", "images_to_video", str(job.id)
        )

        result = tasks.task_images_to_video.delay(
            job.id, images_dir, fps, audio_path, out_dir,
            use_ken_burns=use_ken_burns,
            script=script,
            voice_id=voice_id,
            voice_lang=voice_lang
        )
        job.task_id = result.id
        db.session.commit()
        return redirect(url_for("tools.job_view", job_id=job.id))

    return render_template("tools/images_to_video.html")


# ---------------------------------------------------------------------------
# 6. QR code generator (synchronous, creates a job)
# ---------------------------------------------------------------------------
@tools_bp.route("/qr-code", methods=["GET", "POST"])
@login_required
def qr_code():
    if request.method == "POST":
        if not _active_job_limit_ok():
            flash("You already have N active jobs. Please wait for one to finish.", "warning")
            return redirect(url_for("tools.qr_code"))
        text = request.form.get("text", "").strip()
        if not text:
            flash("Please enter some text or a URL.", "warning")
            return redirect(url_for("tools.qr_code"))

        job = _new_job("qr_code", label=text[:120])
        out_dir = user_dir(current_app.config["STORAGE_ROOT"], current_user.id, "outputs", "qr_code", str(job.id))
        out_file = os.path.join(out_dir, "qrcode.png")

        try:
            qr = qrcode.QRCode(version=3, box_size=12, border=6, error_correction=qrcode.constants.ERROR_CORRECT_H)
            qr.add_data(text)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            img.save(out_file)

            job.status = "success"
            job.progress = 100
            job.message = "Done"
            job.result_path = os.path.relpath(out_file, current_app.config["STORAGE_ROOT"])
        except Exception as e:
            job.status = "failure"
            job.message = f"Error: {e}"
        db.session.commit()

        return redirect(url_for("tools.job_view", job_id=job.id))

    return render_template("tools/qr_code.html")


# ---------------------------------------------------------------------------
# 7. Text to speech (synchronous, creates a job)
# ---------------------------------------------------------------------------
@tools_bp.route("/text-to-speech", methods=["GET", "POST"])
@login_required
def text_to_speech():
    if request.method == "POST":
        if not _active_job_limit_ok():
            flash("You already have N active jobs. Please wait for one to finish.", "warning")
            return redirect(url_for("tools.text_to_speech"))
        text = request.form.get("text", "").strip()
        lang = request.form.get("lang", "en")
        if not text:
            flash("Please enter some text.", "warning")
            return redirect(url_for("tools.text_to_speech"))

        from gtts import gTTS

        job = _new_job("text_to_speech", label=text[:120])
        out_dir = user_dir(current_app.config["STORAGE_ROOT"], current_user.id, "outputs", "text_to_speech", str(job.id))
        out_file = os.path.join(out_dir, "speech.mp3")

        try:
            tts = gTTS(text=text, lang=lang, slow=False)
            tts.save(out_file)

            job.status = "success"
            job.progress = 100
            job.message = "Done"
            job.result_path = os.path.relpath(out_file, current_app.config["STORAGE_ROOT"])
        except Exception as e:
            job.status = "failure"
            job.message = f"Error: {e}"
        db.session.commit()

        return redirect(url_for("tools.job_view", job_id=job.id))

    return render_template("tools/text_to_speech.html")


# ---------------------------------------------------------------------------
# 8. AI Background Remover
# ---------------------------------------------------------------------------
@tools_bp.route("/background-remover", methods=["GET", "POST"])
@login_required
def background_remover():
    if request.method == "POST":
        if not _active_job_limit_ok():
            flash("You already have N active jobs. Please wait for one to finish.", "warning")
            return redirect(url_for("tools.background_remover"))
        file = request.files.get("image")
        if not file or file.filename == "":
            flash("Please choose an image.", "warning")
            return redirect(url_for("tools.background_remover"))
        if not _ext_ok(file.filename, current_app.config["ALLOWED_IMAGE_EXT"]):
            flash("Unsupported image format.", "danger")
            return redirect(url_for("tools.background_remover"))

        path = _save_upload(file, "background_remover")
        job = _new_job("background_remover", label=file.filename)
        out_dir = user_dir(current_app.config["STORAGE_ROOT"], current_user.id, "outputs", "background_remover", str(job.id))
        result = tasks.task_remove_background.delay(job.id, path, out_dir)
        job.task_id = result.id
        db.session.commit()
        return redirect(url_for("tools.job_view", job_id=job.id))

    return render_template("tools/background_remover.html")


# ---------------------------------------------------------------------------
# 9. AI Background Replacer
# ---------------------------------------------------------------------------
@tools_bp.route("/background-replacer", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per minute", methods=["POST"])
def background_replacer():
    if request.method == "POST":
        if not _active_job_limit_ok():
            flash("You already have N active jobs. Please wait for one to finish.", "warning")
            return redirect(url_for("tools.background_replacer"))
        subject_file = request.files.get("subject")
        bg_file = request.files.get("background")
        light_direction = request.form.get("light_direction", "none")

        if not subject_file or subject_file.filename == "" or not bg_file or bg_file.filename == "":
            flash("Please choose both a subject photo and a new background photo.", "warning")
            return redirect(url_for("tools.background_replacer"))
        for f in (subject_file, bg_file):
            if not _ext_ok(f.filename, current_app.config["ALLOWED_IMAGE_EXT"]):
                flash("Unsupported image format.", "danger")
                return redirect(url_for("tools.background_replacer"))

        subject_path = _save_upload(subject_file, "background_replacer")
        bg_path = _save_upload(bg_file, "background_replacer")
        job = _new_job("background_replacer", label=subject_file.filename)
        out_dir = user_dir(current_app.config["STORAGE_ROOT"], current_user.id, "outputs", "background_replacer", str(job.id))
        result = tasks.task_replace_background.delay(job.id, subject_path, bg_path, light_direction, out_dir)
        job.task_id = result.id
        db.session.commit()
        return redirect(url_for("tools.job_view", job_id=job.id))

    return render_template("tools/background_replacer.html")


# ---------------------------------------------------------------------------
# 10. Photo Repair
# ---------------------------------------------------------------------------
@tools_bp.route("/photo-repair", methods=["GET", "POST"])
@login_required
def photo_repair():
    if request.method == "POST":
        if not _active_job_limit_ok():
            flash("You already have N active jobs. Please wait for one to finish.", "warning")
            return redirect(url_for("tools.photo_repair"))
        file = request.files.get("image")
        if not file or file.filename == "":
            flash("Please choose an image.", "warning")
            return redirect(url_for("tools.photo_repair"))
        if not _ext_ok(file.filename, current_app.config["ALLOWED_IMAGE_EXT"]):
            flash("Unsupported image format.", "danger")
            return redirect(url_for("tools.photo_repair"))

        path = _save_upload(file, "photo_repair")
        job = _new_job("photo_repair", label=file.filename)
        out_dir = user_dir(current_app.config["STORAGE_ROOT"], current_user.id, "outputs", "photo_repair", str(job.id))
        result = tasks.task_repair_photo.delay(job.id, path, out_dir)
        job.task_id = result.id
        db.session.commit()
        return redirect(url_for("tools.job_view", job_id=job.id))

    return render_template("tools/photo_repair.html")


# ---------------------------------------------------------------------------
# 11. Remove / Replace Video Audio
# ---------------------------------------------------------------------------
@tools_bp.route("/audio-remover", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per minute", methods=["POST"])
def audio_remover():
    if request.method == "POST":
        if not _active_job_limit_ok():
            flash("You already have N active jobs. Please wait for one to finish.", "warning")
            return redirect(url_for("tools.audio_remover"))
        video_file = request.files.get("video")
        audio_file = request.files.get("audio")

        if not video_file or video_file.filename == "":
            flash("Please choose a video file.", "warning")
            return redirect(url_for("tools.audio_remover"))
        if not _ext_ok(video_file.filename, current_app.config["ALLOWED_VIDEO_EXT"]):
            flash("Unsupported video format.", "danger")
            return redirect(url_for("tools.audio_remover"))

        video_path = _save_upload(video_file, "audio_remover")
        audio_path = None
        if audio_file and audio_file.filename:
            if not _ext_ok(audio_file.filename, current_app.config["ALLOWED_AUDIO_EXT"]):
                flash("Unsupported replacement-audio format.", "danger")
                return redirect(url_for("tools.audio_remover"))
            audio_path = _save_upload(audio_file, "audio_remover")

        job = _new_job("audio_remover", label=video_file.filename)
        out_dir = user_dir(current_app.config["STORAGE_ROOT"], current_user.id, "outputs", "audio_remover", str(job.id))
        result = tasks.task_remove_audio.delay(job.id, video_path, audio_path, out_dir)
        job.task_id = result.id
        db.session.commit()
        return redirect(url_for("tools.job_view", job_id=job.id))

    return render_template("tools/audio_remover.html")


# ---------------------------------------------------------------------------
# 12. Video Enhancer
# ---------------------------------------------------------------------------
@tools_bp.route("/video-enhancer", methods=["GET", "POST"])
@login_required
@limiter.limit("6 per minute", methods=["POST"])
def video_enhancer():
    if request.method == "POST":
        if not _active_job_limit_ok():
            flash("You already have N active jobs. Please wait for one to finish.", "warning")
            return redirect(url_for("tools.video_enhancer"))
        file = request.files.get("video")
        target = request.form.get("target", "original")
        denoise = request.form.get("denoise", "off")
        sharpen = request.form.get("sharpen", "light")
        smooth_fps = request.form.get("smooth_fps") == "on"

        if not file or file.filename == "":
            flash("Please choose a video file.", "warning")
            return redirect(url_for("tools.video_enhancer"))
        if not _ext_ok(file.filename, current_app.config["ALLOWED_VIDEO_EXT"]):
            flash("Unsupported video format.", "danger")
            return redirect(url_for("tools.video_enhancer"))
        if target not in ("original", "720p", "1080p"):
            target = "original"

        path = _save_upload(file, "video_enhancer")
        job = _new_job("video_enhancer", label=file.filename)
        out_dir = user_dir(current_app.config["STORAGE_ROOT"], current_user.id, "outputs", "video_enhancer", str(job.id))

        # ---------------------- FIXED ----------------------
        # Call with keyword arguments matching the task signature.
        result = tasks.task_enhance_video.delay(
            job.id,
            video_path=path,
            out_dir=out_dir,
            target=target,
            denoise=denoise,
            sharpen=sharpen,
            smooth_fps=smooth_fps,
        )   
        job.task_id = result.id
        db.session.commit()
        return redirect(url_for("tools.job_view", job_id=job.id))

    return render_template("tools/video_enhancer.html")


# ---------------------------------------------------------------------------
# 13. Speech Translator
# ---------------------------------------------------------------------------
@tools_bp.route("/speech-translator", methods=["GET", "POST"])
@login_required
def speech_translator():
    if request.method == "GET":
        return render_template(
            "tools/speech_translator.html",
            languages=SUPPORTED_LANGUAGES,
        )

    # POST: validate and process
    if not _active_job_limit_ok():
        flash("You already have N active jobs. Please wait for one to finish.", "warning")
        return redirect(url_for("tools.speech_translator"))
    audio_file = request.files.get("audio_file")
    target_lang = request.form.get("target_lang")
    source_lang = request.form.get("source_lang") or None
    elevenlabs_voice_id = request.form.get("elevenlabs_voice_id") or None

    if not audio_file or audio_file.filename == "":
        flash("Please choose an audio file to translate.", "danger")
        return redirect(url_for("tools.speech_translator"))

    if not allowed_audio_file(audio_file.filename):
        flash("Unsupported file type. Please upload MP3, WAV, M4A, OGG, or FLAC.", "danger")
        return redirect(url_for("tools.speech_translator"))

    if target_lang not in SUPPORTED_LANGUAGES:
        flash("Please select a valid target language.", "danger")
        return redirect(url_for("tools.speech_translator"))

    # ---------------------- FIXED ----------------------
    # Use the standard _save_upload with user_dir, not a public static folder.
    path = _save_upload(audio_file, "speech_translator")

    job = _new_job("speech-translator", label=audio_file.filename)
    job.input_meta = {
        "original_filename": audio_file.filename,
        "target_lang": target_lang,
        "source_lang": source_lang,
    }
    db.session.commit()

    out_dir = user_dir(current_app.config["STORAGE_ROOT"], current_user.id, "outputs", "speech_translator", str(job.id))

    translate_speech_task.delay(
        job_id=job.id,
        source_audio_path=path,
        target_lang=target_lang,
        source_lang=source_lang,
        elevenlabs_voice_id=elevenlabs_voice_id,
    )

    return redirect(url_for("tools.job_view", job_id=job.id))


@tools_bp.route("/speech-translator/status/<int:job_id>")
@login_required
def speech_translator_status(job_id):
    job = Job.query.get_or_404(job_id)
    if job.user_id != current_user.id:
        return jsonify({"error": "not found"}), 404

    result_url = None
    if job.status == "success" and job.result_path:
        result_url = url_for("tools.job_download", job_id=job.id)

    return jsonify({
        "status": job.status,
        "progress": job.progress,
        "stage": getattr(job, "stage", None),
        "result_meta": getattr(job, "result_meta", None),
        "result_url": result_url,
        "error_message": getattr(job, "error_message", None),
    })



# ---------------------------------------------------------------------------
# Shared job status / result views
# ---------------------------------------------------------------------------
@tools_bp.route("/job/<int:job_id>")
@login_required
def job_view(job_id):
    job = Job.query.get_or_404(job_id)
    if job.user_id != current_user.id:
        abort(403)
    return render_template("tools/job_status.html", job=job)


@tools_bp.route("/job/<int:job_id>/status.json")
@login_required
@limiter.exempt
def job_status_json(job_id):
    job = Job.query.get_or_404(job_id)
    if job.user_id != current_user.id:
        abort(403)
    return jsonify(job.to_dict())


@tools_bp.route("/job/<int:job_id>/download")
@login_required
def job_download(job_id):
    job = Job.query.get_or_404(job_id)
    if job.user_id != current_user.id:
        abort(403)

    if not job.result_path:
        flash(
            "This job doesn't have a finished result yet. "
            "If it's stuck on 'pending', make sure the Celery worker is running.",
            "warning",
        )
        return redirect(url_for("tools.job_view", job_id=job.id))

    root = current_app.config["STORAGE_ROOT"]
    full_path = os.path.join(root, job.result_path)
    if not os.path.isfile(full_path):
        job.status = "failure"
        job.message = "Result file went missing on disk. Please try running this tool again."
        job.result_path = None
        db.session.commit()
        flash(job.message, "danger")
        return redirect(url_for("tools.job_view", job_id=job.id))

    directory, filename = os.path.split(full_path)
    return send_from_directory(directory, filename, as_attachment=True)


# ---------------------------------------------------------------------------
# History (all jobs, paginated, with delete)
# ---------------------------------------------------------------------------
@tools_bp.route("/history")
@login_required
def history():
    page = request.args.get("page", 1, type=int)
    status_filter = request.args.get("status", "")
    q = Job.query.filter_by(user_id=current_user.id)
    if status_filter in {"pending", "running", "success", "failure"}:
        q = q.filter_by(status=status_filter)
    pagination = q.order_by(Job.created_at.desc()).paginate(page=page, per_page=15, error_out=False)
    return render_template("tools/history.html", pagination=pagination, status_filter=status_filter)


@tools_bp.route("/job/<int:job_id>/delete", methods=["POST"])
@login_required
def job_delete(job_id):
    job = Job.query.get_or_404(job_id)
    if job.user_id != current_user.id:
        abort(403)

    if job.result_path:
        full_path = os.path.join(current_app.config["STORAGE_ROOT"], job.result_path)
        job_out_dir = os.path.dirname(full_path)
        shutil.rmtree(job_out_dir, ignore_errors=True)

    db.session.delete(job)
    db.session.commit()
    flash("Job deleted.", "success")
    # Safe redirect: use referrer only if it's a same‑site URL
    referrer = request.referrer
    safe_url = _safe_redirect_url(referrer, url_for("tools.history"))
    return redirect(safe_url)


# ---------------------------------------------------------------------------
# Generic fallback for tool pages (must be LAST)
# ---------------------------------------------------------------------------
@tools_bp.route('/<slug>')
@login_required
def tool_page(slug):
    tool = next((t for t in TOOLS if t['slug'] == slug), None)
    if not tool:
        abort(404)
    return render_template('tools/tool_page.html', tool=tool)


# ---------------------------------------------------------------------------
# 13b. Combine to PDF
# ---------------------------------------------------------------------------
_COMBINE_PDF_ALLOWED_EXT = IMAGE_EXTS | VIDEO_EXTS | TEXT_EXTS | PDF_EXTS


@tools_bp.route("/combine-pdf", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per minute", methods=["POST"])
def combine_pdf():
    if request.method == "POST":
        if not _active_job_limit_ok():
            flash("You already have N active jobs. Please wait for one to finish.", "warning")
            return redirect(url_for("tools.combine_pdf"))
        files = request.files.getlist("files")
        files = [f for f in files if f and f.filename]

        if len(files) < 1:
            flash("Please choose at least one file to combine.", "warning")
            return redirect(url_for("tools.combine_pdf"))

        bad_files = [
            f.filename for f in files
            if os.path.splitext(f.filename)[1].lower() not in _COMBINE_PDF_ALLOWED_EXT
        ]
        if bad_files:
            flash(f"Unsupported file type(s): {', '.join(bad_files)}", "danger")
            return redirect(url_for("tools.combine_pdf"))

        # Reordering (handled by frontend)
        order_raw = request.form.get("file_order", "")
        try:
            order = [int(i) for i in order_raw.split(",") if i.strip() != ""]
            if sorted(order) != list(range(len(files))):
                raise ValueError("order does not match uploaded file count")
            ordered_files = [files[i] for i in order]
        except (ValueError, IndexError):
            ordered_files = files

        options = {
            "page_size": request.form.get("page_size", "A4"),
            "orientation": request.form.get("orientation", "portrait"),
            "header_text": request.form.get("header_text", "").strip() or None,
            "footer_text": request.form.get("footer_text", "").strip() or None,
            "page_numbers": request.form.get("page_numbers") == "on",
        }
        if options["page_numbers"] or options["header_text"] or options["footer_text"]:
            from datetime import date
            options["date_text"] = date.today().strftime("%Y-%m-%d")

        job = _new_job("combine_pdf", label=f"{len(ordered_files)} files")
        saved_paths = [_save_upload(f, "combine_pdf") for f in ordered_files]

        out_dir = user_dir(current_app.config["STORAGE_ROOT"], current_user.id, "outputs", "combine_pdf", str(job.id))
        result = tasks.task_combine_to_pdf.delay(job.id, saved_paths, options, out_dir)
        job.task_id = result.id
        db.session.commit()
        return redirect(url_for("tools.job_view", job_id=job.id))

    return render_template("tools/combine_pdf.html")


# ---------------------------------------------------------------------------
# 14. Video Speech Translator
# ---------------------------------------------------------------------------
@tools_bp.route("/video-speech-translator", methods=["GET", "POST"])
@login_required
@limiter.limit("6 per minute", methods=["POST"])
def video_speech_translator():
    if request.method == "POST":
        if not _active_job_limit_ok():
            flash("You already have N active jobs. Please wait for one to finish.", "warning")
            return redirect(url_for("tools.video_speech_translator"))
        file = request.files.get("video")
        target_lang = request.form.get("target_lang")
        source_lang = request.form.get("source_lang") or None
        generate_subtitles = request.form.get("generate_subtitles") == "on"
        elevenlabs_voice_id = request.form.get("elevenlabs_voice_id") or None

        if not file or file.filename == "":
            flash("Please choose a video file.", "warning")
            return redirect(url_for("tools.video_speech_translator"))
        if not _ext_ok(file.filename, current_app.config["ALLOWED_VIDEO_EXT"]):
            flash("Unsupported video format.", "danger")
            return redirect(url_for("tools.video_speech_translator"))
        if target_lang not in SUPPORTED_LANGUAGES:
            flash("Please select a valid target language.", "danger")
            return redirect(url_for("tools.video_speech_translator"))

        path = _save_upload(file, "video_speech_translator")
        job = _new_job("video_speech_translator", label=file.filename)
        job.input_meta = {
            "original_filename": file.filename,
            "target_lang": target_lang,
            "source_lang": source_lang,
            "generate_subtitles": generate_subtitles,
        }
        db.session.commit()

        out_dir = user_dir(
            current_app.config["STORAGE_ROOT"], current_user.id,
            "outputs", "video_speech_translator", str(job.id),
        )
        result = tasks.task_translate_video_speech.delay(
            job.id, path, target_lang, out_dir,
            source_lang=source_lang,
            elevenlabs_voice_id=elevenlabs_voice_id,
            generate_subtitles=generate_subtitles,
        )
        job.task_id = result.id
        db.session.commit()
        return redirect(url_for("tools.job_view", job_id=job.id))

    return render_template(
        "tools/video_speech_translator.html",
        languages=SUPPORTED_LANGUAGES,
    )


# ---------------------------------------------------------------------------
# Optional: subtitles download
# ---------------------------------------------------------------------------
@tools_bp.route("/job/<int:job_id>/subtitles")
@login_required
def job_download_subtitles(job_id):
    job = Job.query.get_or_404(job_id)
    if job.user_id != current_user.id:
        abort(403)

    srt_rel_path = (job.result_meta or {}).get("subtitles_path")
    if not srt_rel_path:
        flash("No subtitles were generated for this job.", "warning")
        return redirect(url_for("tools.job_view", job_id=job.id))

    root = current_app.config["STORAGE_ROOT"]
    full_path = os.path.join(root, srt_rel_path)
    if not os.path.isfile(full_path):
        flash("Subtitle file went missing on disk.", "danger")
        return redirect(url_for("tools.job_view", job_id=job.id))

    directory, filename = os.path.split(full_path)
    return send_from_directory(directory, filename, as_attachment=True)


# ---------------------------------------------------------------------------
# 15. Auto-Edit Video
# ---------------------------------------------------------------------------
_AUTO_EDIT_BUILTIN_TEMPLATES = ["cinematic", "social_short", "slideshow", "podcast_highlights"]
_AUTO_EDIT_OUTPUT_FORMATS = {"1080p", "4k", "vertical", "square"}


def _load_template_choices():
    choices = []
    for slug in _AUTO_EDIT_BUILTIN_TEMPLATES:
        path = os.path.join(TEMPLATES_DIR, f"{slug}.json")
        name = slug.replace("_", " ").title()
        try:
            with open(path, "r", encoding="utf-8") as f:
                name = json.load(f).get("name", name)
        except Exception:
            pass
        choices.append({"slug": slug, "name": name})
    choices.append({"slug": "custom", "name": "Custom (upload your own JSON)"})
    return choices


@tools_bp.route("/auto-edit-video", methods=["GET", "POST"])
@login_required
@limiter.limit("4 per minute", methods=["POST"])
def auto_edit_video():
    if request.method == "POST":
        if not _active_job_limit_ok():
            flash("You already have N active jobs. Please wait for one to finish.", "warning")
            return redirect(url_for("tools.auto_edit_video"))
        files = request.files.getlist("clips")
        files = [f for f in files if f and f.filename]

        music_file = request.files.get("music")
        custom_template_file = request.files.get("custom_template")

        template_name = request.form.get("template", "cinematic")
        trim_silence = request.form.get("trim_silence") == "on"
        generate_captions = request.form.get("generate_captions") == "on"
        caption_language = request.form.get("caption_language") or None
        title_text = request.form.get("title_text", "").strip() or None
        output_formats = [f for f in request.form.getlist("output_formats") if f in _AUTO_EDIT_OUTPUT_FORMATS]

        # NEW: auto-highlights fields
        auto_highlights = request.form.get("auto_highlights") == "on"
        try:
            highlight_duration = float(request.form.get("highlight_duration", 30))
        except ValueError:
            highlight_duration = 30.0
        # Sanity bounds: 10 seconds to 5 minutes
        highlight_duration = max(10.0, min(highlight_duration, 300.0))

        if not files:
            flash("Please upload at least one video clip.", "warning")
            return redirect(url_for("tools.auto_edit_video"))

        allowed_video = current_app.config.get("ALLOWED_VIDEO_EXT", {"mp4", "mov", "mkv", "avi", "webm"})
        allowed_audio = current_app.config.get("ALLOWED_AUDIO_EXT", {"mp3", "wav", "m4a", "ogg", "flac"})

        for f in files:
            ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
            if ext not in allowed_video:
                if ext in allowed_audio:
                    flash(f"Audio files (like '{f.filename}') are not accepted as clips. Please upload a video file.", "danger")
                else:
                    flash(f"Unsupported file format: '{f.filename}'. Allowed: {', '.join(sorted(allowed_video))}", "danger")
                return redirect(url_for("tools.auto_edit_video"))

        if template_name not in _AUTO_EDIT_BUILTIN_TEMPLATES + ["custom"]:
            template_name = "cinematic"

        if template_name == "custom" and (not custom_template_file or custom_template_file.filename == ""):
            flash("Please upload a template JSON file for the Custom template.", "warning")
            return redirect(url_for("tools.auto_edit_video"))

        if not output_formats:
            output_formats = ["1080p"]

        # Validate custom template JSON (if provided) before saving
        if template_name == "custom" and custom_template_file:
            try:
                content = custom_template_file.read()
                json.loads(content)
                custom_template_file.seek(0)  # reset for saving
            except Exception:
                flash("Invalid custom template JSON. Please check the file format.", "danger")
                return redirect(url_for("tools.auto_edit_video"))

        # Save uploads
        clip_paths = [_save_upload(f, "auto_edit_video") for f in files]

        music_path = None
        if music_file and music_file.filename:
            if not _ext_ok(music_file.filename, allowed_audio):
                flash("Unsupported background music format.", "danger")
                return redirect(url_for("tools.auto_edit_video"))
            music_path = _save_upload(music_file, "auto_edit_video")

        custom_template_path = None
        if template_name == "custom":
            custom_template_path = _save_upload(custom_template_file, "auto_edit_video")

        # Create job
        job = _new_job("auto_edit_video", label=f"{len(clip_paths)} clip(s), {template_name}")
        job.input_meta = {
            "template": template_name,
            "trim_silence": trim_silence,
            "generate_captions": generate_captions,
            "output_formats": output_formats,
            "auto_highlights": auto_highlights,
            "highlight_duration": highlight_duration if auto_highlights else None,
        }
        db.session.commit()

        out_dir = user_dir(current_app.config["STORAGE_ROOT"], current_user.id, "outputs", "auto_edit_video", str(job.id))

        # Call Celery task with the new arguments
        result = tasks.task_auto_edit_video.delay(
            job.id,
            clip_paths=clip_paths,
            out_dir=out_dir,
            template_name=template_name,
            custom_template_path=custom_template_path,
            trim_silence_enabled=trim_silence,
            generate_captions=generate_captions,
            caption_language=caption_language,
            title_text=title_text,
            music_path=music_path,
            output_formats=output_formats,
            auto_highlights=auto_highlights,
            highlight_duration=highlight_duration,
        )
        job.task_id = result.id
        db.session.commit()

        return redirect(url_for("tools.job_view", job_id=job.id))

    # GET: show the form
    return render_template(
        "tools/auto_edit_video.html",
        templates=_load_template_choices(),
        languages=SUPPORTED_LANGUAGES,
    )