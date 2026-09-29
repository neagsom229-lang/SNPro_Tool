from datetime import datetime, timedelta
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from extensions import db


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(150), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(120), nullable=True, default="")
    is_admin = db.Column(db.Boolean, default=False, nullable=False)

    # Verification & Security fields
    is_verified = db.Column(db.Boolean, default=False, nullable=False)
    verification_token = db.Column(db.String(100), unique=True, nullable=True, index=True)
    verification_token_expires = db.Column(db.DateTime, nullable=True)

    # Password Reset fields
    reset_password_token = db.Column(db.String(100), unique=True, nullable=True, index=True)
    reset_password_expires = db.Column(db.DateTime, nullable=True)

    # Brute-force protection & activity tracking
    failed_login_attempts = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime, nullable=True)
    last_login_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    jobs = db.relationship("Job", backref="user", lazy="dynamic", cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_locked(self):
        if self.locked_until and self.locked_until > datetime.utcnow():
            return True
        return False

    def __repr__(self):
        return f"<User {self.username} (verified={self.is_verified})>"


class Job(db.Model):
    """Tracks a background (or instant) tool run for a user."""

    __tablename__ = "jobs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    tool = db.Column(db.String(50), nullable=False)
    task_id = db.Column(db.String(155), index=True)
    status = db.Column(db.String(20), default="pending")  # pending/running/success/failure
    progress = db.Column(db.Integer, default=0)
    message = db.Column(db.String(500), default="")

    input_label = db.Column(db.String(300), default="")
    result_path = db.Column(db.String(500))  # relative path under STORAGE_ROOT

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    stage = db.Column(db.String(50), default="")
    result_meta = db.Column(db.JSON, default=dict)          # fixed mutable default
    error_message = db.Column(db.String(500), default="")

    # Composite index for faster history queries (batch processing will stress this)
    __table_args__ = (
        db.Index("ix_jobs_user_status", "user_id", "status"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "tool": self.tool,
            "status": self.status,
            "progress": self.progress,
            "message": self.message or "",
            "input_label": self.input_label or "",
            "result_meta": self.result_meta or {},
            "has_result": bool(self.result_path),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "stage": self.stage or "",
            "error_message": self.error_message or "",
        }
