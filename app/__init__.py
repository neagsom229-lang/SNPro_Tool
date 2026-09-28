import os
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix
from config import Config, BASE_DIR
from extensions import db, login_manager, csrf, migrate, limiter, make_celery
from datetime import datetime, timezone
import click


def create_app(config_class=Config):
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.from_object(config_class)
    config_class.check_production_secrets()

    # Trust reverse proxy headers
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # Create required directories
    os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)
    os.makedirs(app.config["STORAGE_ROOT"], exist_ok=True)

    # Init extensions
    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    migrate.init_app(app, db)
    limiter.init_app(app)
    make_celery(app)  # Celery must be configured before importing tasks (blueprints)

    # Import all models so SQLAlchemy knows about them
    from models import User, Job  # noqa: F401

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # Register blueprints (imported after Celery init)
    from app.auth.routes import auth_bp
    from app.main.routes import main_bp
    from app.tools.routes import tools_bp
    from app.admin.routes import admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(tools_bp)
    app.register_blueprint(admin_bp)

    # =============== DATABASE INITIALIZATION ===============
    # Multiple Gunicorn workers may start at the same time. When they all
    # call create_all() concurrently, one wins and the others raise
    # "table already exists". Catch that and continue.
    with app.app_context():
        from sqlalchemy import inspect
        from sqlalchemy.exc import OperationalError

        try:
            inspector = inspect(db.engine)
            if not inspector.has_table("users"):
                db.create_all()
                app.logger.info("Database tables created successfully.")
            else:
                app.logger.info("Database tables already exist.")
        except OperationalError as e:
            if "already exists" in str(e).lower():
                app.logger.info("Tables already created by another worker; continuing.")
            else:
                raise

    # =============== ADMIN BOOTSTRAP ===============
    # Promotes the user whose username matches ADMIN_USERNAME (if set).
    # Runs on every startup; safe because it only sets is_admin=True.
    admin_username = os.environ.get("ADMIN_USERNAME", "").strip()
    if admin_username:
        with app.app_context():
            try:
                user = User.query.filter_by(username=admin_username).first()
                if user and not user.is_admin:
                    user.is_admin = True
                    db.session.commit()
                    app.logger.info(f"Promoted {admin_username} to admin.")
                elif user:
                    app.logger.info(f"{admin_username} is already admin.")
                else:
                    app.logger.info(f"ADMIN_USERNAME='{admin_username}' not found yet.")
            except Exception as e:
                # Table may not exist yet during a race; safe to skip this pass.
                app.logger.warning(f"Admin bootstrap skipped: {e}")

    # =============== CONTEXT PROCESSORS ===============
    @app.context_processor
    def inject_globals():
        from flask_login import current_user
        return {"current_user": current_user}

    @app.context_processor
    def inject_now():
        return {"now": datetime.now(timezone.utc)}

    # =============== CLI COMMANDS ===============
    @app.cli.command("make-admin")
    @click.argument("username")
    def make_admin_command(username):
        """Promote a user to admin."""
        user = User.query.filter_by(username=username).first()
        if not user:
            click.echo(f"No user named '{username}' found.")
            return
        user.is_admin = True
        db.session.commit()
        click.echo(f"{username} is now an admin.")

    # =============== LOGGING (basic, for production) ===============
    if not app.debug:
        import logging
        from logging.handlers import RotatingFileHandler
        log_dir = os.path.join(BASE_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)
        handler = RotatingFileHandler(
            os.path.join(log_dir, "snpro.log"),
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
        )
        handler.setLevel(logging.INFO)
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        handler.setFormatter(formatter)
        app.logger.addHandler(handler)
        app.logger.setLevel(logging.INFO)

    return app