import os
import ast
import inspect
import pytest
from flask import url_for
from app import create_app
from extensions import db
import extensions
from models import User, Job
from app.tools import tasks


@pytest.fixture
def app():
    os.environ["FLASK_ENV"] = "testing"
    os.environ["WTF_CSRF_ENABLED"] = "true"
    os.environ["CELERY_BROKER_URL"] = "memory://"
    os.environ["CELERY_RESULT_BACKEND"] = "rpc://"
    
    db_path = os.path.abspath("test_temp.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    app = create_app()
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
        "WTF_CSRF_ENABLED": True,
        "CELERY_ALWAYS_EAGER": True,
        "TASK_ALWAYS_EAGER": True,
        "CELERY_RESULT_BACKEND": "rpc://",
    })

    if extensions.celery:
        extensions.celery.conf.update(
            task_always_eager=True,
            task_eager_propagates=True,
            result_backend="rpc://",
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
        user_id = user.id

    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
    return client


def test_healthz(client):
    res = client.get("/healthz")
    assert res.status_code in (200, 503)
    data = res.get_json()
    assert "db" in data
    assert "redis" in data
    assert "ffmpeg" in data


def test_delay_signatures_match():
    """
    Parse routes.py with ast, find every .delay(...) call, and use inspect.signature(...).bind()
    to ensure arguments match task signatures precisely.
    """
    routes_path = os.path.join("app", "tools", "routes.py")
    with open(routes_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=routes_path)

    class DelayVisitor(ast.NodeVisitor):
        def __init__(self):
            self.calls = []

        def visit_Call(self, node):
            self.generic_visit(node)
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "delay":
                task_name = None
                if isinstance(func.value, ast.Attribute):
                    task_name = func.value.attr
                elif isinstance(func.value, ast.Name):
                    task_name = func.value.id
                if task_name:
                    self.calls.append((task_name, node))

    visitor = DelayVisitor()
    visitor.visit(tree)

    assert len(visitor.calls) > 0, "No .delay() calls found in routes.py"

    for task_name, node in visitor.calls:
        assert hasattr(tasks, task_name), f"Task {task_name} not found in tasks.py"
        task_func = getattr(tasks, task_name)
        sig = inspect.signature(task_func)

        args_placeholders = [None] * len(node.args)
        kwargs_placeholders = {kw.arg: None for kw in node.keywords if kw.arg}

        try:
            sig.bind(*args_placeholders, **kwargs_placeholders)
        except TypeError as e:
            pytest.fail(f"Task signature mismatch for {task_name}: {e}")


def test_templates_have_csrf():
    """
    Every template containing <form method="post"> must contain literal csrf_token() or hidden_tag()
    in a non-comment line.
    """
    templates_dir = os.path.join("app", "templates")
    assert os.path.exists(templates_dir)

    for root, _, files in os.walk(templates_dir):
        for file in files:
            if file.endswith(".html"):
                path = os.path.join(root, file)
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()

                full_text = "".join(lines).lower()
                if "method=\"post\"" in full_text or "method='post'" in full_text:
                    has_csrf = False
                    in_comment = False
                    for line in lines:
                        stripped = line.strip()
                        if "<!--" in stripped:
                            in_comment = True
                        if "-->" in stripped:
                            in_comment = False
                            continue
                        if in_comment:
                            continue
                        if "csrf_token()" in line or "hidden_tag()" in line:
                            has_csrf = True
                            break
                    assert has_csrf, f"Template {path} has <form method=\"post\"> but lacks literal csrf_token() or hidden_tag() in non-comment lines."


def test_video_enhancer_csrf_success(auth_client):
    """
    With WTF_CSRF_ENABLED=True, GETs /tools/video-enhancer, extracts csrf token,
    and confirms POST succeeds (not rejected with 400 due to CSRF failure).
    """
    res = auth_client.get("/tools/video-enhancer")
    assert res.status_code == 200
    html = res.text

    import re
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    csrf_token = match.group(1) if match else ""

    from io import BytesIO
    data = {
        "csrf_token": csrf_token,
        "target_resolution": "original",
        "denoise": "off",
        "sharpen": "light",
    }
    data["video"] = (BytesIO(b"dummy video content"), "test.mp4")

    post_res = auth_client.post("/tools/video-enhancer", data=data, content_type="multipart/form-data", follow_redirects=True)
    # Status code will not be 400 (Bad Request from CSRF failure), it will process or fail at job creation/ffprobe, not 400.
    assert post_res.status_code != 400


def test_beat_schedule_signatures(app):
    """
    Ensure every entry in celery.conf.beat_schedule has args/kwargs that
    match the task's signature.
    """
    from extensions import celery
    if not celery:
        pytest.skip("Celery not configured")

    beat_schedule = celery.conf.beat_schedule
    for name, entry in beat_schedule.items():
        task_path = entry["task"]
        # task_path is like 'tools.watchdog_stuck_jobs'
        # In this project, tasks are in app.tools.tasks and registered with names like 'tools.watchdog_stuck_jobs'
        # Celery tasks are accessible via celery.tasks
        assert task_path in celery.tasks, f"Task {task_path} not found in celery.tasks"
        task_func = celery.tasks[task_path]
        
        # task_func is a Celery task object, the actual function is task_func.run
        # or we can use inspect.signature(task_func.run)
        sig = inspect.signature(task_func.run)
        
        args = entry.get("args", ())
        kwargs = entry.get("kwargs", {})
        
        try:
            sig.bind(*args, **kwargs)
        except TypeError as e:
            pytest.fail(f"Beat schedule entry '{name}' (task '{task_path}') has signature mismatch: {e}")
