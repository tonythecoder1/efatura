from conftest import make_pdf
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app


def upload(client, pdf, name="fatura.pdf", **kwargs):
    return client.post("/v1/invoices", files={"file": (name, pdf, "application/pdf")}, **kwargs)


def test_register_login_and_free_invoice_limit(tmp_path, fake, pdf):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'app.sqlite'}",
        auth_secret=SecretStr("test-auth-secret"),
        free_invoice_limit=1,
    )
    with TestClient(create_app(settings, fake)) as client:
        registered = client.post(
            "/v1/auth/register",
            json={"email": "user@example.com", "password": "correct horse battery"},
        )
        assert registered.status_code == 200
        token = registered.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        assert client.get("/v1/billing/status", headers=headers).json()["remaining_free"] == 1
        first = upload(client, pdf, headers=headers)
        assert first.status_code == 201
        assert client.get("/v1/billing/status", headers=headers).json()["used"] == 1

        fake.result.invoice.invoice_number = "FT 2026/002"
        blocked = upload(client, make_pdf(pages=2), "second.pdf", headers=headers)
        assert blocked.status_code == 402
        assert "1 faturas gratuitas" in blocked.json()["detail"]

        assert client.get("/v1/invoices.csv", headers=headers).status_code == 200
        assert client.get("/v1/invoices.csv").status_code == 401


def test_login_rejects_wrong_password(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'app.sqlite'}",
        auth_secret=SecretStr("test-auth-secret"),
    )
    with TestClient(create_app(settings)) as client:
        client.post(
            "/v1/auth/register",
            json={"email": "user@example.com", "password": "correct horse battery"},
        )
        response = client.post(
            "/v1/auth/login",
            json={"email": "user@example.com", "password": "wrong password"},
        )
    assert response.status_code == 401
