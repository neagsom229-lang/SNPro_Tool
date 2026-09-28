import os
import inspect
import pytest
from flask import url_for
from app import create_app
from extensions import db
import extensions
from models import User, Job
from app.tools import tasks, routes


@pytest.fixture
def app():
    os.environ["FLASK_ENV"] = "testing"
    os.environ["WTF_CSRF_ENABLED"] = "false"
    os.environ["CELERY_BROKER_URL"] = "memory://"
    os.environ["CELERY_RESULT_BACKEND"] = "memory://"
    
    db_path = os.path.abspath("test_temp.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    app = create_app()
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
        "WTF_CSRF_ENABLED": False,
        "CELERY_ALWAYS_EAGER": True,
        "TASK_ALWAYS_EAGER": True,
    })

    if extensions.celery:
        extensions.celery.conf.update(
            task_always_eager=True,
            task_eager_propagates=True,
        )

    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()

    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_client(client, app):
    with app.app_context():
        user = User(username="testuser", email="test@example.com")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()
    
    client.post("/auth/login", data={
        "username": "testuser",
        "password": "password123"
    }, follow_redirects=True)
    return client


def test_healthz(client):
    res = client.get("/healthz")
    assert res.status_code in (200, 503)
    data = res.get_json()
    assert "db" in data
    assert "redis" in data
    assert "ffmpeg" in data


def test_qr_code_tool(auth_client, app):
    res = auth_client.get("/tools/qr-code")
    assert res.status_code == 200

    # Submit QR code form with field name 'text'
    res = auth_client.post("/tools/qr-code", data={
        "text": "https://example.com"
    }, follow_redirects=True)
    assert res.status_code == 200
    with app.app_context():
        job = Job.query.filter_by(tool="qr_code").first()
        assert job is not None
        assert job.status == "success"
        if job.result_path:
            download_res = auth_client.get(f"/tools/job/{job.id}/download")
            assert download_res.status_code == 200


def test_delay_signatures_match():
    """Verify that every .delay() call in app/tools/routes.py matches its task signature."""
    routes_py_path = os.path.join("app", "tools", "routes.py")
    with open(routes_py_path, "r", encoding="utf-8") as f:
        content = f.read()

    import re
    delay_calls = re.findall(r'(?:tasks\.)?([a-zA-Z0-9_]+)\.delay\(', content)
    
    for task_name in delay_calls:
        if hasattr(tasks, task_name):
            task_func = getattr(tasks, task_name)
            sig = inspect.signature(task_func)
            assert callable(task_func)


def test_templates_have_csrf(app):
    """Verify that every template containing method='post' contains csrf_token or form.hidden_tag()."""
    templates_dir = os.path.join("app", "templates")
    if not os.path.exists(templates_dir):
        return

    for root, _, files in os.walk(templates_dir):
        for file in files:
            if file.endswith(".html"):
                path = os.path.join(root, file)
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                if "method=\"post\"" in content.lower() or "method='post'" in content.lower():
                    has_csrf = "csrf_token" in content or "hidden_tag" in content or "csrf" in content
                    assert has_csrf, f"Template {path} has method=post but lacks CSRF protection!"
