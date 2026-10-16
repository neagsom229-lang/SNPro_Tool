from functools import wraps
import os

from flask import Blueprint, render_template, abort, request, redirect, url_for, flash
from flask_login import login_required, current_user
from sqlalchemy import func

from extensions import db
from models import User, Job

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


@admin_bp.route("/")
@login_required
@admin_required
def dashboard():
    total_users = User.query.count()
    total_jobs = Job.query.count()

    # Grouped query – one round‑trip instead of four separate `.count()` calls
    status_counts = dict(
        db.session.query(Job.status, func.count(Job.id))
        .group_by(Job.status)
        .all()
    )
    jobs_by_status = {
        status: status_counts.get(status, 0)
        for status in ("pending", "running", "success", "failure")
    }

    jobs_by_tool = (
        db.session.query(Job.tool, func.count(Job.id))
        .group_by(Job.tool)
        .all()
    )
    recent_users = User.query.order_by(User.created_at.desc()).limit(10).all()

    return render_template(
        "admin/dashboard.html",
        total_users=total_users,
        total_jobs=total_jobs,
        jobs_by_status=jobs_by_status,
        jobs_by_tool=jobs_by_tool,
        recent_users=recent_users,
    )


@admin_bp.route("/jobs")
@login_required
@admin_required
def jobs():
    page = request.args.get("page", 1, type=int)
    user_id = request.args.get("user_id", type=int)
    status = request.args.get("status", "")

    q = Job.query
    if user_id:
        q = q.filter_by(user_id=user_id)
    if status in {"pending", "running", "success", "failure"}:
        q = q.filter_by(status=status)

    pagination = q.order_by(Job.created_at.desc()).paginate(
        page=page, per_page=25, error_out=False
    )
    return render_template(
        "admin/jobs.html",
        pagination=pagination,
        user_id=user_id,
        status=status,
    )


@admin_bp.route("/users")
@login_required
@admin_required
def users():
    page = request.args.get("page", 1, type=int)
    pagination = User.query.order_by(User.created_at.desc()).paginate(
        page=page, per_page=25, error_out=False
    )
    return render_template("admin/users.html", pagination=pagination)


@admin_bp.route("/users/<int:user_id>/toggle-admin", methods=["POST"])
@login_required
@admin_required
def toggle_admin(user_id):
    user = db.get_or_404(User, user_id)

    if user.id == current_user.id:
        flash("You can't change your own admin status.", "warning")
        return redirect(url_for("admin.users"))

    # Require confirmation from the form (defense in depth)
    confirm = request.form.get("confirm") == "yes"
    if not confirm:
        flash("Please confirm this action by ticking the confirmation box.", "warning")
        return redirect(url_for("admin.users"))

    user.is_admin = not user.is_admin
    db.session.commit()

    flash(
        f"{user.username} is now {'an admin' if user.is_admin else 'a regular user'}.",
        "success",
    )
    return redirect(url_for("admin.users"))


@admin_bp.route("/system")
@login_required
@admin_required
def system_info():
    import platform
    import shutil as _shutil
    from flask import current_app

    uname = platform.uname()
    info = {
        "system": uname.system,
        "node": uname.node,
        "release": uname.release,
        "version": uname.version,
        "machine": uname.machine,
        "processor": uname.processor or "n/a",
        "python_version": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }

    psutil_stats = None
    try:
        import psutil
        mem = psutil.virtual_memory()
        psutil_stats = {
            "cpu_percent": psutil.cpu_percent(interval=0.3),
            "mem_percent": mem.percent,
            "mem_used_gb": round(mem.used / (1024 ** 3), 1),
            "mem_total_gb": round(mem.total / (1024 ** 3), 1),
            "boot_time": psutil.boot_time(),
        }
    except Exception:
        pass  # psutil not installed – degrade gracefully

    storage_root = current_app.config["STORAGE_ROOT"]
    disk_total, disk_used, disk_free = _shutil.disk_usage(storage_root)
    disk = {
        "total_gb": round(disk_total / (1024 ** 3), 1),
        "used_gb": round(disk_used / (1024 ** 3), 1),
        "free_gb": round(disk_free / (1024 ** 3), 1),
        "percent": round(disk_used / disk_total * 100, 1) if disk_total else 0,
    }

    redis_ok, redis_error = False, None
    try:
        import redis as redis_lib
        client = redis_lib.from_url(
            current_app.config["CELERY_BROKER_URL"],
            socket_connect_timeout=2,
        )
        redis_ok = client.ping()
    except Exception:
        # Do NOT expose the raw error – it may contain credentials
        redis_ok = False
        redis_error = "Failed to connect to Redis. Check broker configuration."

    return render_template(
        "admin/system.html",
        info=info,
        psutil_stats=psutil_stats,
        disk=disk,
        redis_ok=redis_ok,
        redis_error=redis_error,
    )