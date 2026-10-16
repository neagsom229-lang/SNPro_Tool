import pytest
from bs4 import BeautifulSoup
from app import create_app
from extensions import db, limiter
import extensions
from models import User, Job

@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["RATELIMIT_ENABLED"] = False
    limiter.enabled = False
    app.config["CELERY_ALWAYS_EAGER"] = True
    app.config["CELERY_TASK_ALWAYS_EAGER"] = True
    app.config["CELERY_RESULT_BACKEND"] = "rpc://"

    if extensions.celery:
        extensions.celery.conf.update(
            task_always_eager=True,
            task_eager_propagates=True,
            result_backend="rpc://",
        )

    with app.app_context():
        db.create_all()
        user = User(username="e2e_user", email="e2e@example.com")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()
        yield app
        db.drop_all()

@pytest.fixture
def client(app):
    return app.test_client()

@pytest.fixture
def auth_client(client, app):
    with app.app_context():
        user = User.query.filter_by(username="e2e_user").first()
        user_id = user.id

    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
    return client

def test_pages_batch1_batch_assertions(client, auth_client, app):
    with app.app_context():
        user = User.query.filter_by(username="e2e_user").first()
        job = Job(user_id=user.id, tool="qr_code", status="success", progress=100, result_path="dummy.png")
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    pages = ["/dashboard", "/tools/history", f"/tools/job/{job_id}"]
    for page in pages:
        res = auth_client.get(page)
        assert res.status_code == 200, f"Page {page} failed with {res.status_code}"
        
        csp = res.headers.get("Content-Security-Policy", "")
        nonce = ""
        if "nonce-" in csp:
            import re
            m = re.search(r"nonce-([a-zA-Z0-9]+)", csp)
            if m:
                nonce = m.group(1)

        html = res.get_data(as_text=True)
        soup = BeautifulSoup(html, "html.parser")

        # Check inline scripts carry CSP nonce
        for script in soup.find_all("script"):
            if not script.get("src"):
                assert script.has_attr("nonce")
                if nonce:
                    assert script.get("nonce") == nonce

        # Check static links carry ?v=
        for link in soup.find_all("link", rel="stylesheet"):
            href = link.get("href", "")
            if "/static/" in href:
                assert "?v=" in href

    # Specific checks for job page
    res = auth_client.get(f"/tools/job/{job_id}")
    soup = BeautifulSoup(res.get_data(as_text=True), "html.parser")
    assert soup.find(id="jobStatusRoot")
    assert soup.find(id="jobStatusRoot").has_attr("data-job-id")
    assert soup.find(id="jobRetryBtn")
    assert soup.find(id="jobDownloadWrap")
