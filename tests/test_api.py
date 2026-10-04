import csv
import io

import httpx
import pytest
from conftest import make_pdf
from fastapi.testclient import TestClient
from openai import APITimeoutError, AuthenticationError, RateLimitError
from pydantic import SecretStr

from app.config import Settings
from app.extractor import ExtractionFailed
from app.main import create_app
from app.models import ExtraField


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None, csv_path=tmp_path / "faturas.csv", openai_api_key="", api_key=""
    )


@pytest.fixture
def client(settings, fake):
    with TestClient(create_app(settings, fake)) as client:
        yield client


def upload(client, pdf, name="fatura.pdf", **kwargs):
    return client.post("/v1/invoices", files={"file": (name, pdf, "application/pdf")}, **kwargs)


@pytest.mark.parametrize(
    "language,name,currency,total",
    [
        ("pt", "Serviços, Lda.", "EUR", "123.00"),
        ("en", "Example Ltd", "GBP", "1234.56"),
    ],
)
def test_upload_roundtrip(client, fake, pdf, language, name, currency, total):
    fake.result.invoice.language = language
    fake.result.invoice.supplier.name = name
    fake.result.invoice.currency = currency
    fake.result.invoice.total = total
    response = upload(client, pdf)
    assert response.status_code == 201
    record = response.json()["record"]
    assert record["invoice"]["total"] == total
    exported = client.get("/v1/invoices.csv")
    assert exported.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig")), delimiter=";"))
    assert len(rows) == 1
    assert rows[0]["nome_fornecedor"] == name
    assert rows[0]["nif_cliente"] == "001234567"
    assert set(rows[0]) == {
        "tipo_documento",
        "numero_fatura",
        "data_emissao",
        "data_vencimento",
        "nome_fornecedor",
        "nif_fornecedor",
        "morada_fornecedor",
        "email_fornecedor",
        "nome_cliente",
        "nif_cliente",
        "morada_cliente",
        "email_cliente",
        "moeda",
        "subtotal",
        "desconto_total",
        "total_impostos",
        "total",
        "valor_em_divida",
        "metodo_pagamento",
        "iban",
        "ordem_compra",
        "observacoes",
        "impostos",
        "outros_detalhes",
    }
    assert "items" not in rows[0]
    assert "gpt-6-luna" not in exported.text


def test_same_pdf_is_not_analyzed_or_saved_twice(client, fake, pdf):
    first = upload(client, pdf)
    second = upload(client, pdf, "renamed.pdf")
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["record"] == first.json()["record"]
    assert fake.calls == 1


def test_reexported_pdf_with_same_invoice_identity_is_not_added_twice(client, fake, pdf):
    first = upload(client, pdf, "original.pdf")
    second = upload(client, make_pdf(pages=2), "reexported.pdf")

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["record"] == first.json()["record"]
    rows = list(
        csv.DictReader(io.StringIO(client.get("/v1/invoices.csv").content.decode("utf-8-sig")), delimiter=";")
    )
    assert len(rows) == 1
    assert fake.calls == 2


@pytest.mark.parametrize(
    "data,name,status",
    [
        (b"", "empty.pdf", 422),
        (b"not a pdf", "fake.pdf", 422),
        (b"%PDF-1.7\nbroken", "broken.pdf", 422),
        (b"image", "scan.png", 415),
    ],
)
def test_invalid_upload_does_not_call_ai(client, fake, data, name, status):
    assert upload(client, data, name).status_code == status
    assert fake.calls == 0


def test_encrypted_and_page_limit(client, fake, settings):
    assert upload(client, make_pdf(encrypted=True)).status_code == 422
    assert upload(client, make_pdf(pages=settings.max_pages + 1)).status_code == 422
    assert fake.calls == 0


def test_file_size_limit(settings, fake):
    settings.max_upload_mb = 1
    with TestClient(create_app(settings, fake)) as client:
        assert upload(client, b"%PDF-" + b"x" * (1024 * 1024)).status_code == 413
        assert upload(client, b"x" * (3 * 1024 * 1024)).status_code == 413
    assert fake.calls == 0


def test_chunked_body_limit(settings, fake):
    settings.max_upload_mb = 1
    with TestClient(create_app(settings, fake)) as client:
        response = client.post("/v1/invoices", content=iter([b"x" * 1024] * 2100))
    assert response.status_code == 413
    assert fake.calls == 0


def test_auth_protects_upload_and_download(settings, fake, pdf):
    settings.api_key = SecretStr("local-secret")
    with TestClient(create_app(settings, fake)) as client:
        assert upload(client, pdf).status_code == 401
        assert client.get("/v1/invoices.csv").status_code == 401
        assert upload(client, pdf, headers={"X-API-Key": "local-secret"}).status_code == 201
    assert fake.calls == 1


def test_auth_protects_import_and_batch(settings, fake, pdf):
    settings.api_key = SecretStr("local-secret")
    with TestClient(create_app(settings, fake)) as client:
        assert client.post(
            "/v1/invoices/import-csv",
            files={"csv_file": ("faturas.csv", b"invalid", "text/csv")},
        ).status_code == 401
        assert client.post(
            "/v1/invoices/batch",
            files={"files": ("fatura.pdf", pdf, "application/pdf")},
        ).status_code == 401


def test_batch_reports_an_invalid_file_without_processing_other_files(client, fake, pdf):
    response = client.post(
        "/v1/invoices/batch",
        files=[
            ("files", ("nota.txt", b"texto", "text/plain")),
            ("files", ("fatura.pdf", pdf, "application/pdf")),
        ],
    )
    payload = response.json()
    assert response.status_code == 200
    assert payload["failed"] == 1
    assert payload["processed"] == 1
    assert [item["status"] for item in payload["results"]] == ["error", "processed"]
    assert fake.calls == 1


def test_cors_allows_configured_frontend_origin(settings, fake):
    settings.frontend_origins = "http://localhost:5173"
    with TestClient(create_app(settings, fake)) as client:
        response = client.options(
            "/v1/invoices.csv",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-API-Key",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "GET" in response.headers["access-control-allow-methods"]


def test_cors_rejects_unconfigured_frontend_origin(settings, fake):
    settings.frontend_origins = "http://localhost:5173"
    with TestClient(create_app(settings, fake)) as client:
        response = client.options(
            "/v1/invoices.csv",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_missing_provider_key_is_clear(settings, pdf):
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json()["extraction_configured"] is False
        response = upload(client, pdf)
    assert response.status_code == 503
    assert "OPENAI_API_KEY" in response.json()["detail"]
    assert not settings.csv_path.exists()


def test_health_publishes_upload_contract(settings, fake):
    with TestClient(create_app(settings, fake)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "extraction_configured": False,
        "accepted_formats": ["pdf"],
        "max_upload_mb": 20,
        "max_pages": 30,
    }


def test_import_generated_csv_merges_only_new_rows(client, fake, pdf):
    first = upload(client, pdf)
    assert first.status_code == 201
    rows = list(
        csv.DictReader(io.StringIO(client.get("/v1/invoices.csv").content.decode("utf-8-sig")), delimiter=";")
    )
    rows.append({**rows[0], "numero_fatura": "FT 2026/002", "total": "124.00"})
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=rows[0], delimiter=";")
    writer.writeheader()
    writer.writerows([rows[0], rows[1]])

    response = client.post(
        "/v1/invoices/import-csv",
        files={"csv_file": ("faturas.csv", buffer.getvalue().encode("utf-8-sig"), "text/csv")},
    )

    assert response.status_code == 200
    assert response.json() == {"imported": 1, "duplicates": 1, "total": 2}
    exported_rows = list(
        csv.DictReader(io.StringIO(client.get("/v1/invoices.csv").content.decode("utf-8-sig")), delimiter=";")
    )
    assert len(exported_rows) == 2


def test_batch_upload_combined_mode_processes_two_pdfs(client, fake, pdf):
    second_pdf = make_pdf(pages=2)
    original_extract = fake.extract

    def extract_distinct_invoice(data):
        fake.result.invoice.invoice_number = f"FT 2026/00{fake.calls + 1}"
        return original_extract(data)

    fake.extract = extract_distinct_invoice
    response = client.post(
        "/v1/invoices/batch",
        data={"mode": "combined"},
        files=[
            ("files", ("one.pdf", pdf, "application/pdf")),
            ("files", ("two.pdf", second_pdf, "application/pdf")),
        ],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "combined"
    assert payload["total"] == 2
    assert payload["processed"] == 2
    assert payload["duplicates"] == 0
    assert [item["status"] for item in payload["results"]] == ["processed", "processed"]
    assert fake.calls == 2


def test_batch_upload_separate_mode_returns_one_csv_per_pdf(client, fake, pdf):
    response = client.post(
        "/v1/invoices/batch",
        data={"mode": "separate"},
        files=[
            ("files", ("one.pdf", pdf, "application/pdf")),
            ("files", ("two.pdf", make_pdf(pages=2), "application/pdf")),
        ],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "separate"
    assert len(payload["results"]) == 2
    assert all(item["csv_content"].startswith("\ufefftipo_documento;") for item in payload["results"])


def test_batch_upload_rejects_more_than_ten_files(client, fake, pdf):
    files = [("files", (f"{number}.pdf", pdf, "application/pdf")) for number in range(11)]
    response = client.post("/v1/invoices/batch", files=files)
    assert response.status_code == 422
    assert "10" in response.json()["detail"]
    assert fake.calls == 0


@pytest.mark.parametrize("count,is_invoice", [(0, False), (2, True)])
def test_non_invoice_and_multiple_invoices_are_not_written(
    client, fake, pdf, settings, count, is_invoice
):
    fake.result.invoice_count = count
    fake.result.is_invoice = is_invoice
    fake.result.invoice = None
    assert upload(client, pdf).status_code == 422
    assert not settings.csv_path.exists()


def test_missing_and_inconsistent_values_are_flagged(client, fake, pdf):
    fake.result.invoice.issue_date = None
    fake.result.invoice.total = "999.99"
    result = upload(client, pdf).json()["record"]
    assert result["needs_review"] is True
    assert result["invoice"]["issue_date"] is None
    assert len(result["warnings"]) == 2


@pytest.mark.parametrize(
    "kind,status", [("timeout", 504), ("rate", 503), ("auth", 503), ("incomplete", 502)]
)
def test_provider_errors_never_write_csv(client, fake, pdf, settings, kind, status):
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    upstream = httpx.Response(429, request=request)
    fake.error = {
        "timeout": APITimeoutError(request=request),
        "rate": RateLimitError("secret upstream details", response=upstream, body=None),
        "auth": AuthenticationError("secret key", response=upstream, body=None),
        "incomplete": ExtractionFailed("private invoice contents"),
    }[kind]
    response = upload(client, pdf)
    assert response.status_code == status
    assert "secret" not in response.text and "private" not in response.text
    assert not settings.csv_path.exists()


def test_spreadsheet_formula_is_escaped_and_technical_columns_are_absent(client, fake, pdf):
    fake.result.invoice.supplier.name = '=HYPERLINK("https://example.test")'
    upload(client, pdf, "=formula.pdf")
    rows = list(
        csv.DictReader(
            io.StringIO(client.get("/v1/invoices.csv").content.decode("utf-8-sig")), delimiter=";"
        )
    )
    assert rows[0]["nome_fornecedor"].startswith("'=")
    assert "filename" not in rows[0]
    assert "record_json" not in rows[0]


def test_negative_total_remains_a_number(client, fake, pdf):
    fake.result.invoice.total = "-12.50"
    upload(client, pdf)
    rows = list(
        csv.DictReader(
            io.StringIO(client.get("/v1/invoices.csv").content.decode("utf-8-sig")), delimiter=";"
        )
    )
    assert rows[0]["total"] == "-12.50"


def test_items_are_removed_but_tax_and_extra_details_are_kept(client, fake, pdf):
    fake.result.invoice.items[0].product_code = "SKU-42"
    fake.result.invoice.items[0].discount = "1.00"
    fake.result.invoice.items[0].tax_rate = "23"
    fake.result.invoice.items[0].tax_amount = "2.00"
    fake.result.invoice.items[0].net_amount = "9.00"
    fake.result.invoice.extra_fields = [ExtraField(label="ATCUD", value="ABC-123")]
    upload(client, pdf)
    rows = list(
        csv.DictReader(
            io.StringIO(client.get("/v1/invoices.csv").content.decode("utf-8-sig")), delimiter=";"
        )
    )
    assert "items" not in rows[0]
    assert rows[0]["impostos"] == "IVA 23% 23.00"
    assert rows[0]["outros_detalhes"] == "ATCUD: ABC-123"


def test_export_empty_csv(client):
    response = client.get("/v1/invoices.csv")
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
    assert len(rows) == 1
    assert "numero_fatura" in rows[0]


def test_incompatible_csv_is_not_overwritten(client, settings, pdf):
    settings.csv_path.write_text("existing;unrelated;file\n", encoding="utf-8")
    assert upload(client, pdf).status_code == 503
    assert settings.csv_path.read_text() == "existing;unrelated;file\n"
