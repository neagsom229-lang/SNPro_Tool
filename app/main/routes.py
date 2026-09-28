from flask import Blueprint, render_template, redirect, url_for, request, flash, current_app
from flask_login import login_required, current_user
from sqlalchemy import func
from werkzeug.security import check_password_hash
from app.auth.forms import MIN_PASSWORD_LENGTH
from sqlalchemy.exc import IntegrityError
import os

from models import User, Job
from extensions import db
from config import user_dir
from app.auth.forms import ChangePasswordForm

main_bp = Blueprint("main", __name__)

 
TOOLS = [
    {
        "slug": "video-downloader",
        "name": "Video Downloader",
        "icon": "bi-cloud-arrow-down",
        "desc": "Download videos from YouTube, Facebook & TikTok via yt-dlp, with live progress.",
        # not AI - yt-dlp is a pure downloader
    },
    {
        "slug": "speech-to-text",
        "name": "Speech to Text",
        "icon": "bi-mic",
        "desc": "Upload an audio file and get an automatic transcription.",
        "ai": True,  # Whisper
    },
    {
        "slug": "video-to-frames",
        "name": "Video to Frames",
        "icon": "bi-film",
        "desc": "Extract frames from a video and download them as a ZIP.",
    },
    {
        "slug": "pdf-to-word",
        "name": "PDF to Word",
        "icon": "bi-file-earmark-word",
        "desc": "Convert a PDF document into an editable Word (.docx) file.",
    },
    {
        "slug": "images-to-video",
        "name": "Images to Video",
        "icon": "bi-images",
        "desc": "Turn a folder of images into a slideshow video, with optional background music.",
        "ai": True,  # optional Stable Video Diffusion motion + ElevenLabs/gTTS voiceover + Whisper captions
    },
    {
        "slug": "qr-code",
        "name": "QR Code Generator",
        "icon": "bi-qr-code",
        "desc": "Generate and download a QR code from any text or URL — instantly.",
    },
    {
        "slug": "text-to-speech",
        "name": "Text to Speech",
        "icon": "bi-volume-up",
        "desc": "Convert typed or pasted text into an MP3 voice file using gTTS.",
        "ai": True,  # ElevenLabs/gTTS
    },
    {
        "slug": "background-remover",
        "name": "AI Background Remover",
        "icon": "bi-person-bounding-box",
        "desc": "Cut a photo's subject out from its background automatically, using an on-device AI model.",
        "ai": True,
    },
    {
        "slug": "background-replacer",
        "name": "AI Background Replacer",
        "icon": "bi-image",
        "desc": "Extract a subject and composite it onto a new background, with a soft shadow and lighting match.",
        "ai": True,
    },
    {
        "slug": "photo-repair",
        "name": "Photo Repair",
        "icon": "bi-image-alt",
        "desc": "Denoise, restore contrast, and sharpen an old or damaged photo — colors preserved.",
        "ai": True,  # optional Real-ESRGAN restore pass
    },
    {
        "slug": "audio-remover",
        "name": "Remove / Replace Audio",
        "icon": "bi-mic-mute",
        "desc": "Strip a video's audio track, or swap it for your own MP3/WAV.",
    },
    {
        "slug": "video-enhancer",
        "name": "Video Enhancer",
        "icon": "bi-magic",
        "desc": "Denoise, sharpen, resize, and smooth the motion of a video, up to 1080p.",
        "ai": True,  # optional Real-ESRGAN upscale + RIFE interpolation
    },
    {
        "slug": "speech-translator",
        "name": "Speech Translator",
        "icon": "bi-translate",
        "desc": "Translate spoken audio into natural-sounding speech in any language.",
        "ai": True,  # Whisper + DeepL/Google/NLLB + ElevenLabs/gTTS
    },
    {
        "slug": "combine-pdf",
        "name": "Combine to PDF",
        "icon": "bi-file-pdf",
        "desc": "Merge images, videos, text files, and existing PDFs into one ordered PDF document.",
    },
    {
        "slug": "video-speech-translator",
        "name": "Video Speech Translator",
        "icon": "bi-file-play",
        "desc": "Translate the spoken audio of a video into another language with dubbed speech and optional subtitles.",
        "ai": True,  # Whisper + translation + ElevenLabs/gTTS
    },
    {
        "slug": "auto-edit-video",
        "name": "Auto-Edit Video",
        "icon": "bi-scissors",
        "desc": "Automatically edit raw footage into a polished video with AI-trimming, transitions, captions, and multiple export formats.",
        "ai": True,  # Whisper captions
    },
]
 
 
# ----- Search -----
@main_bp.route('/search')
def search():
    query = request.args.get('q', '').strip()
    results = []
    if query:
        lower_q = query.lower()
        results = [t for t in TOOLS if lower_q in t['name'].lower() or lower_q in t['desc'].lower()]
    return render_template('main/search.html', query=query, results=results)

# ----- Static pages -----
@main_bp.route("/about")
def about():
    return render_template("main/about.html", tools=TOOLS)

@main_bp.route("/contact")
def contact():
    return render_template("main/settings.html", title="Contact", content="Contact us...")

@main_bp.route("/terms")
def terms():
    return render_template("main/settings.html", title="Terms", content="Terms of Service...")

@main_bp.route("/privacy")
def privacy():
    return render_template("main/settings.html", title="Privacy", content="Privacy Policy...")

# ----- User account pages -----
@main_bp.route("/profile", methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        current_password = request.form.get('current_password', '').strip()
        new_password = request.form.get('password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()

        if not username or not email:
            flash('Username and email are required.', 'danger')
            return redirect(url_for('main.profile'))

        # Verify current password before any changes (defense in depth)
        if not current_password or not current_user.check_password(current_password):
            flash('Current password is required and must be correct.', 'danger')
            return redirect(url_for('main.profile'))

        # Check if username/email already taken by another user
        existing = User.query.filter(
            (User.username == username) | (User.email == email),
            User.id != current_user.id
        ).first()
        if existing:
            flash('Username or email already in use.', 'danger')
            return redirect(url_for('main.profile'))

        # Apply changes
        current_user.username = username
        current_user.email = email

        if new_password:
            # Use the shared constant from forms.py
            if len(new_password) < MIN_PASSWORD_LENGTH:
                flash(f'Password must be at least {MIN_PASSWORD_LENGTH} characters.', 'danger')
                return redirect(url_for('main.profile'))
            if new_password != confirm_password:
                flash('Passwords do not match.', 'danger')
                return redirect(url_for('main.profile'))
            current_user.set_password(new_password)

        # Atomic commit with uniqueness check
        try:
            db.session.commit()
            flash('Profile updated successfully!', 'success')
        except IntegrityError:
            db.session.rollback()
            flash('Username or email already taken (race condition). Please try again.', 'danger')

        return redirect(url_for('main.profile'))

    return render_template('main/profile.html', user=current_user)


@main_bp.route("/orders")
@login_required
def orders():
    # Redirect to the actual job history page
    return redirect(url_for('tools.history'))

@main_bp.route("/settings")
@login_required
def settings():
    return render_template("main/settings.html")

# ----- Landing & Dashboard -----
@main_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    return render_template("main/landing.html", tools=TOOLS)

@main_bp.route("/dashboard")
@login_required
def dashboard():
    # Recent jobs (limit 15)
    recent_jobs = (
        Job.query.filter_by(user_id=current_user.id)
        .order_by(Job.created_at.desc())
        .limit(15)
        .all()
    )

    # Aggregate job counts in one query (instead of three separate `.count()`)
    status_counts = (
        db.session.query(Job.status, func.count(Job.id))
        .filter_by(user_id=current_user.id)
        .group_by(Job.status)
        .all()
    )
    job_counts = {status: count for status, count in status_counts}
    total_jobs = sum(job_counts.values())
    completed_jobs = job_counts.get("success", 0)
    failed_jobs = job_counts.get("failure", 0)

    # Storage usage – currently O(number of files) per request.
    # For batch processing, consider caching this value (e.g., in Redis or as a User field).
    storage_bytes = 0
    user_root = user_dir(current_app.config["STORAGE_ROOT"], current_user.id)
    for dirpath, _dirnames, filenames in os.walk(user_root):
        for fname in filenames:
            try:
                storage_bytes += os.path.getsize(os.path.join(dirpath, fname))
            except OSError:
                pass

    stats = {
        "total_jobs": total_jobs,
        "completed_jobs": completed_jobs,
        "failed_jobs": failed_jobs,
        "storage_mb": round(storage_bytes / (1024 * 1024), 1),
    }

    return render_template(
        "main/dashboard.html", tools=TOOLS, jobs=recent_jobs, stats=stats
    )

@main_bp.route("/_debug/redis-url")
def _debug_redis_url():
    import os, re
    broker = os.environ.get("CELERY_BROKER_URL", "NOT SET")
    backend = os.environ.get("CELERY_RESULT_BACKEND", "NOT SET")
    masked_broker = re.sub(r':[^@]+@', ':***@', broker)
    masked_backend = re.sub(r':[^@]+@', ':***@', backend)
    return f"<pre>BROKER: {masked_broker}\nBACKEND: {masked_backend}</pre>"


@main_bp.route("/healthz")
def healthz():
    """
    Health check endpoint: checks DB (SELECT 1), Redis ping, and ffmpeg presence.
    Returns 200 JSON on success, 503 JSON on failure.
    """
    from flask import jsonify
    import subprocess
    status = {"db": "ok", "redis": "ok", "ffmpeg": "ok"}
    healthy = True

    try:
        db.session.execute(db.text("SELECT 1"))
    except Exception as e:
        status["db"] = f"error: {e}"
        healthy = False

    try:
        from redis import Redis
        broker_url = current_app.config.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
        r = Redis.from_url(broker_url)
        r.ping()
    except Exception as e:
        status["redis"] = f"error: {e}"
        healthy = False

    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except Exception as e:
        status["ffmpeg"] = f"error: {e}"
        healthy = False

    code = 200 if healthy else 503
    return jsonify(status), code