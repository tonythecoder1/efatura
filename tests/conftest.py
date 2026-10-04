from io import BytesIO

import pytest
from pypdf import PdfWriter

from app.models import Extraction, Invoice


def make_pdf(pages=1, encrypted=False):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    if encrypted:
        writer.encrypt("secret")
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.fixture
def pdf():
    return make_pdf()


@pytest.fixture
def invoice():
    return Invoice.model_validate(
        {
            "language": "pt",
            "document_type": "Fatura",
            "invoice_number": "FT 2026/001",
            "issue_date": "2026-10-04",
            "due_date": None,
            "supplier": {
                "name": "Serviços, Lda.",
                "tax_id": "PT500000001",
                "address": "Lisboa",
                "email": None,
            },
            "customer": {
                "name": "José Silva",
                "tax_id": "001234567",
                "address": None,
                "email": None,
            },
            "currency": "EUR",
            "subtotal": "100.00",
            "discount_total": None,
            "tax_total": "23.00",
            "total": "123.00",
            "amount_due": "123.00",
            "payment_method": None,
            "iban": None,
            "purchase_order": None,
            "items": [
                {
                    "description": "Consultoria; implementação",
                    "product_code": "001",
                    "quantity": "2",
                    "unit": "h",
                    "unit_price": "50.00",
                    "discount": None,
                    "tax_rate": "23",
                    "tax_amount": "23.00",
                    "net_amount": "100.00",
                    "total": None,
                }
            ],
            "taxes": [
                {"label": "IVA", "rate": "23", "taxable_amount": "100.00", "amount": "23.00"}
            ],
            "extra_fields": [{"label": "ATCUD", "value": "ABCD-001"}],
            "notes": None,
        }
    )


class FakeExtractor:
    def __init__(self, invoice):
        self.result = Extraction(is_invoice=True, invoice_count=1, invoice=invoice, warnings=[])
        self.calls = 0
        self.error = None

    def extract(self, data):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def fake(invoice):
    return FakeExtractor(invoice)
