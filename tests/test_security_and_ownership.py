import os
import pytest
from flask import url_for
from app import create_app
from extensions import db
from models import User, Job


@pytest.fixture
def app():
    os.environ["FLASK_ENV"] = "testing"
    os.environ["CELERY_BROKER_URL"] = "memory://"
    os.environ["CELERY_RESULT_BACKEND"] = "rpc://"

    db_path = os.path.abspath("test_security.db")
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
def users(app, client):
    with app.app_context():
        u1 = User(username="usera", email="usera@example.com", is_verified=True)
        u1.set_password("password123")
        u2 = User(username="userb", email="userb@example.com", is_verified=True)
        u2.set_password("password123")
        db.session.add_all([u1, u2])
        db.session.commit()

        job_a = Job(user_id=u1.id, tool="video_download", status="success", result_path="dummy.mp4")
        job_b = Job(user_id=u2.id, tool="video_download", status="success", result_path="dummy.mp4")
        db.session.add_all([job_a, job_b])
        db.session.commit()
        job_a_id = job_a.id
        job_b_id = job_b.id
        u1_id = u1.id
        u2_id = u2.id

    return {"user_a_id": u1_id, "user_b_id": u2_id, "job_a_id": job_a_id, "job_b_id": job_b_id}


def login_as(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def test_security_headers_and_csp_nonce(client):
    res = client.get("/")
    assert res.status_code == 200
    assert res.headers.get("X-Content-Type-Options") == "nosniff"
    assert res.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert res.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    
    csp = res.headers.get("Content-Security-Policy", "")
    assert "nonce-" in csp
    assert "default-src" in csp


def test_ownership_isolation(client, users):
    # Log in as User A
    login_as(client, users["user_a_id"])
    job_b_id = users["job_b_id"]

    # User A tries to view User B's job -> 403 Forbidden
    res = client.get(f"/tools/job/{job_b_id}")
    assert res.status_code == 403

    # User A tries to download User B's job -> 403 Forbidden
    res = client.get(f"/tools/job/{job_b_id}/download")
    assert res.status_code == 403

    # User A tries to delete User B's job -> 403 Forbidden
    res = client.post(f"/tools/job/{job_b_id}/delete")
    assert res.status_code == 403


def test_tool_routes_get_success(app, client, users):
    login_as(client, users["user_a_id"])

    tool_endpoints = [
        "tools.video_downloader",
        "tools.speech_to_text",
        "tools.video_to_frames",
        "tools.pdf_to_word",
        "tools.images_to_video",
        "tools.qr_code",
        "tools.text_to_speech",
        "tools.background_remover",
        "tools.background_replacer",
        "tools.photo_repair",
        "tools.audio_remover",
        "tools.video_enhancer",
        "tools.speech_translator",
        "tools.combine_pdf",
        "tools.video_speech_translator",
        "tools.auto_edit_video",
        "tools.history",
    ]

    with app.test_request_context():
        for endpoint in tool_endpoints:
            url = url_for(endpoint)
            res = client.get(url)
            assert res.status_code == 200, f"Endpoint {endpoint} returned status {res.status_code}"


def test_job_sse_logged_out(app, users):
    c = app.test_client()
    job_a_id = users["job_a_id"]
    res = c.get(f"/tools/job/{job_a_id}/events")
    assert res.status_code in (302, 401)


def test_job_sse_owner(app, users):
    c = app.test_client()
    login_as(c, users["user_a_id"])
    job_a_id = users["job_a_id"]
    res = c.get(f"/tools/job/{job_a_id}/events")
    assert res.status_code == 200
    assert "text/event-stream" in res.headers.get("Content-Type", "")
    data = res.get_data(as_text=True)
    assert "success" in data


def test_job_sse_forbidden_other_user(app, users):
    c = app.test_client()
    login_as(c, users["user_a_id"])
    job_b_id = users["job_b_id"] # belongs to user_b
    res = c.get(f"/tools/job/{job_b_id}/events")
    assert res.status_code == 403


def test_job_sse_not_found(app, users):
    c = app.test_client()
    login_as(c, users["user_a_id"])
    res = c.get("/tools/job/999999/events")
    assert res.status_code == 404


def test_job_sse_receives_updates(app, users):
    job_a_id = users["job_a_id"]
    with app.app_context():
        j = db.session.get(Job, job_a_id)
        j.status = "success"
        j.progress = 99
        j.message = "Updated progress before completion"
        db.session.commit()

    c = app.test_client()
    login_as(c, users["user_a_id"])
    res = c.get(f"/tools/job/{job_a_id}/events")
    assert res.status_code == 200
    data = res.get_data(as_text=True)
    assert "success" in data
    assert "99" in data
    assert "Updated progress before completion" in data



