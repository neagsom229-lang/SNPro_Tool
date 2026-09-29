import os
from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


class Config:
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

    # ---- Security: CSRF ----
    WTF_CSRF_ENABLED = os.environ.get("WTF_CSRF_ENABLED", "true").lower() in ("1", "true", "yes")

    # ---- HTTPS and secure cookies ----
    _force_https = os.environ.get("FORCE_HTTPS", "").strip().lower() in ("1", "true", "yes")
    SESSION_COOKIE_SECURE = _force_https
    REMEMBER_COOKIE_SECURE = _force_https
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_HTTPONLY = True

    # ---- Database ----
    _default_db_path = os.path.join(BASE_DIR, "instance", "snpro.db").replace("\\", "/")
    _db_url_env = os.environ.get("DATABASE_URL", "").strip()
    if not _db_url_env or _db_url_env.startswith("sqlite:///instance"):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{_default_db_path}"
    else:
        SQLALCHEMY_DATABASE_URI = _db_url_env
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # ---- Celery / Redis ----
    CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
    CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")

    # ---- Mail / SMTP ----
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", 587))
    MAIL_USE_TLS = os.environ.get("MAIL_USE_TLS", "true").lower() in ("1", "true", "yes")
    MAIL_USE_SSL = os.environ.get("MAIL_USE_SSL", "false").lower() in ("1", "true", "yes")
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER", MAIL_USERNAME)

    # ---- Storage ----
    _storage_env = os.environ.get("STORAGE_ROOT", "").strip()
    if not _storage_env or _storage_env == "storage":
        STORAGE_ROOT = os.path.join(BASE_DIR, "storage")
    elif os.path.isabs(_storage_env):
        STORAGE_ROOT = _storage_env
    else:
        STORAGE_ROOT = os.path.join(BASE_DIR, _storage_env)
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH_MB", 500)) * 1024 * 1024

    # ---- Rate limiting ----
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_DEFAULT = "200 per hour"

    # ---- Allowed file extensions (used by _ext_ok) ----
    ALLOWED_AUDIO_EXT = {
        "mp3", "wav", "m4a", "ogg", "flac",
        "aac", "wma", "aiff", "alac", "opus",
        "mp4",  # for videos that contain audio (extract later)
    }
    ALLOWED_VIDEO_EXT = {"mp4", "mov", "mkv", "avi", "webm"}
    ALLOWED_IMAGE_EXT = {"png", "jpg", "jpeg", "webp", "bmp"}
    ALLOWED_PDF_EXT = {"pdf"}

    # ---- Production‑safety check ----
    @classmethod
    def check_production_secrets(cls):
        if (not os.environ.get("FLASK_SECRET_KEY") and
                cls.SECRET_KEY == "dev-secret-change-me" and
                os.environ.get("FLASK_ENV") == "production"):
            raise RuntimeError(
                "SECRET_KEY is still the default value in production! "
                "Set FLASK_SECRET_KEY in your .env file."
            )


def user_dir(root, user_id, *parts):
    """Build a per‑user storage path and ensure it exists."""
    path = os.path.join(root, "users", str(user_id), *parts)
    os.makedirs(path, exist_ok=True)
    return path