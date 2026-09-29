from datetime import datetime, timedelta
import secrets
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import or_
from urllib.parse import urlparse
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db, limiter
from models import User
from app.auth.forms import RegisterForm, LoginForm, ForgotPasswordForm, ResetPasswordForm
from app.auth.email import send_async_email

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

# Dummy hash for timing‑side‑channel defence (same cost as real bcrypt)
_DUMMY_HASH = generate_password_hash("dummy-password-for-timing")


def _safe_next_url(next_url):
    """Return a safe redirect URL or fallback to dashboard."""
    if not next_url:
        return url_for("main.dashboard")
    if urlparse(next_url).netloc != "":
        return url_for("main.dashboard")
    return next_url


@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("6 per minute")
def register():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    form = RegisterForm()
    if form.validate_on_submit():
        existing = User.query.filter(
            or_(User.username == form.username.data.strip(), User.email == form.email.data.strip().lower())
        ).first()
        if existing:
            flash("Username or email already taken.", "danger")
        else:
            token = secrets.token_urlsafe(32)
            token_expires = datetime.utcnow() + timedelta(hours=24)

            user = User(
                username=form.username.data.strip(),
                email=form.email.data.strip().lower(),
                full_name=form.full_name.data.strip() if form.full_name.data else "",
                is_verified=False,
                verification_token=token,
                verification_token_expires=token_expires
            )
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.commit()

            # Send verification email asynchronously via Celery
            verify_url = url_for("auth.verify_email", token=token, _external=True)
            try:
                send_async_email.delay(
                    to_email=user.email,
                    subject="Verify Your SNPro System Account",
                    template_name="verify_email",
                    context={"username": user.username, "verify_url": verify_url}
                )
            except Exception as e:
                current_app_logger = getattr(current_app, "logger", None)
                if current_app_logger:
                    current_app_logger.warning("Failed to queue verification email: %s", e)

            flash("Account created successfully! Please check your email inbox to verify your account before logging in.", "success")
            return redirect(url_for("auth.login"))

    return render_template("auth/register.html", form=form)


@auth_bp.route("/verify-email/<token>")
@limiter.limit("10 per minute")
def verify_email(token):
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    user = User.query.filter_by(verification_token=token).first()
    if not user or not user.verification_token_expires or user.verification_token_expires < datetime.utcnow():
        flash("Invalid or expired verification link.", "danger")
        return redirect(url_for("auth.login"))

    user.is_verified = True
    user.verification_token = None
    user.verification_token_expires = None
    db.session.commit()

    flash("Email verified successfully! You can now sign in.", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("6 per minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        ident = form.username.data.strip()
        user = User.query.filter(or_(User.username == ident, User.email == ident.lower())).first()

        # Check account lockout
        if user and user.is_locked:
            remaining_mins = int((user.locked_until - datetime.utcnow()).total_seconds() / 60) + 1
            flash(f"Account is temporarily locked due to multiple failed login attempts. Please try again in {remaining_mins} minutes.", "danger")
            return render_template("auth/login.html", form=form)

        ok = False
        if user:
            ok = user.check_password(form.password.data)
        else:
            check_password_hash(_DUMMY_HASH, form.password.data)
            ok = False

        if ok and user:
            if not user.is_verified:
                flash("Please verify your email address before logging in. Check your inbox for the verification link.", "warning")
                return render_template("auth/login.html", form=form)

            # Reset failed attempts & update last login
            user.failed_login_attempts = 0
            user.locked_until = None
            user.last_login_at = datetime.utcnow()
            db.session.commit()

            login_user(user, remember=form.remember.data)
            next_page = request.args.get("next")
            safe_next = _safe_next_url(next_page)
            flash(f"Welcome back, {user.username}!", "success")
            return redirect(safe_next)
        else:
            if user:
                user.failed_login_attempts += 1
                if user.failed_login_attempts >= 5:
                    user.locked_until = datetime.utcnow() + timedelta(minutes=15)
                    db.session.commit()
                    flash("Too many failed login attempts. Your account has been locked for 15 minutes.", "danger")
                else:
                    db.session.commit()
                    flash("Invalid credentials.", "danger")
            else:
                flash("Invalid credentials.", "danger")

    return render_template("auth/login.html", form=form)


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    form = ForgotPasswordForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = User.query.filter_by(email=email).first()

        # Anti-enumeration: always display the same success message regardless of whether user exists
        if user:
            token = secrets.token_urlsafe(32)
            user.reset_password_token = token
            user.reset_password_expires = datetime.utcnow() + timedelta(hours=1)
            db.session.commit()

            reset_url = url_for("auth.reset_password", token=token, _external=True)
            try:
                send_async_email.delay(
                    to_email=user.email,
                    subject="Password Reset Request - SNPro System",
                    template_name="reset_password",
                    context={"username": user.username, "reset_url": reset_url}
                )
            except Exception as e:
                pass

        flash("If an account with that email exists, a password reset link has been sent.", "info")
        return redirect(url_for("auth.login"))

    return render_template("auth/forgot_password.html", form=form)


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def reset_password(token):
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    user = User.query.filter_by(reset_password_token=token).first()
    if not user or not user.reset_password_expires or user.reset_password_expires < datetime.utcnow():
        flash("Invalid or expired password reset link.", "danger")
        return redirect(url_for("auth.login"))

    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        user.reset_password_token = None
        user.reset_password_expires = None
        user.failed_login_attempts = 0
        user.locked_until = None
        db.session.commit()

        flash("Your password has been successfully reset. You can now sign in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", form=form)


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("main.index"))
