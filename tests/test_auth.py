from fastapi.testclient import TestClient
from app.main import app
def test_unauthenticated_protected_page_is_rejected():
    response=TestClient(app).get("/dashboard"); assert response.status_code==401
def test_oauth_unconfigured_is_safe():
    response=TestClient(app).get("/auth/login"); assert response.status_code==503; assert "not configured" in response.text
