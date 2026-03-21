from fastapi.testclient import TestClient


def test_docs_status_html_embeds_api_key_for_browser_fetches():
    from app.main import app
    from app.config import settings

    old_key = settings.api_key
    settings.api_key = "browser-test-key"
    try:
        client = TestClient(app)
        response = client.get("/api/v1/docs/status.html")
    finally:
        settings.api_key = old_key

    assert response.status_code == 200
    assert "browser-test-key" in response.text
    assert "__API_KEY__" not in response.text
    assert "const _H = API_KEY ? {'X-Api-Key': API_KEY} : {};" in response.text
    assert 'translate="no"' in response.text
    assert '<meta name="google" content="notranslate">' in response.text


def test_dashboard_html_marks_page_as_notranslate():
    from app.main import app

    client = TestClient(app)
    response = client.get("/dashboard")

    assert response.status_code == 200
    assert '<html lang="ru" translate="no" class="notranslate">' in response.text
    assert '<meta name="google" content="notranslate">' in response.text
