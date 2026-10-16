import pytest
from app import create_app

@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client

def test_landing_page_fallback_and_aspect_ratio(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="hero3DContainer"' in html
    assert 'aspect-ratio: 4 / 3' in html
    assert 'hero.js' in html or 'hero.bundle.min.js' in html
