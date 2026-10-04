import base64
import json

import httpx
import pytest
from openai import OpenAI
from pydantic import ValidationError

from app.config import Settings
from app.extractor import ExtractionFailed, OpenAIExtractor, review_warnings
from app.models import Extraction, Invoice


def test_actual_sdk_serializes_pdf_and_parses_schema(monkeypatch, pdf, invoice):
    result = Extraction(is_invoice=True, invoice_count=1, invoice=invoice, warnings=[])

    def handle(request):
        payload = json.loads(request.content)
        assert payload["store"] is False
        assert payload["text"]["format"]["strict"] is True
        assert "payment receipt" in payload["instructions"]
        assert "Reservation, order, booking" in payload["instructions"]
        assert "return invoice_count=1" in payload["instructions"]
        assert "same reservation" in payload["instructions"]
        assert "sum the printed net amounts" in payload["instructions"]
        attachment = payload["input"][0]["content"][0]
        assert base64.b64decode(attachment["file_data"].split(",", 1)[1]) == pdf
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 1,
                "model": "gpt-6.1-sol",
                "status": "completed",
                "output": [
                    {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": result.model_dump_json(),
                                "annotations": [],
                            }
                        ],
                    }
                ],
            },
        )

    monkeypatch.setattr(
        "app.extractor.OpenAI",
        lambda **kwargs: OpenAI(
            api_key="test-key",
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        ),
    )
    extracted = OpenAIExtractor(Settings(_env_file=None)).extract(pdf)
    assert extracted == result


def test_refusal_is_reported(monkeypatch, pdf):
    def handle(request):
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 1,
                "model": "gpt-6.1-sol",
                "status": "completed",
                "output": [
                    {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "refusal", "refusal": "Cannot process"}],
                    }
                ],
            },
        )

    monkeypatch.setattr(
        "app.extractor.OpenAI",
        lambda **kwargs: OpenAI(
            api_key="test-key",
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        ),
    )
    with pytest.raises(ExtractionFailed):
        OpenAIExtractor(Settings(_env_file=None)).extract(pdf)


def test_multiple_sections_are_reconciled_before_rejecting(monkeypatch, pdf, invoice):
    first = Extraction(is_invoice=True, invoice_count=2, invoice=None, warnings=[])
    second = Extraction(is_invoice=True, invoice_count=1, invoice=invoice, warnings=[])
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if calls == 2:
            assert "RE-evaluate" not in payload["instructions"]
            assert "Re-evaluate" in payload["instructions"]
        result = first if calls == 1 else second
        return httpx.Response(
            200,
            json={
                "id": f"resp_test_{calls}",
                "object": "response",
                "created_at": 1,
                "model": "gpt-6.1-sol",
                "status": "completed",
                "output": [
                    {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": result.model_dump_json(),
                                "annotations": [],
                            }
                        ],
                    }
                ],
            },
        )

    monkeypatch.setattr(
        "app.extractor.OpenAI",
        lambda **kwargs: OpenAI(
            api_key="test-key",
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        ),
    )
    assert OpenAIExtractor(Settings(_env_file=None)).extract(pdf) == second
    assert calls == 2


@pytest.mark.parametrize("value", ["1.234,56", "1,234.56", "NaN", "Infinity", "€123", ""])
def test_invalid_decimal_cannot_be_saved(invoice, value):
    payload = invoice.model_dump()
    payload["total"] = value
    with pytest.raises(ValidationError):
        Invoice.model_validate(payload)


def test_exact_decimal_and_discount_reconciliation(invoice):
    invoice.subtotal = "0.10"
    invoice.tax_total = "0.20"
    invoice.total = "0.30"
    invoice.discount_total = "0.05"
    invoice.taxes = []
    assert review_warnings(invoice, []) == []
