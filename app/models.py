from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# Strings preserve exact decimal values and avoid binary floating-point rounding.
DecimalText = Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$")]


class ExtractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Party(ExtractionModel):
    name: str | None
    tax_id: str | None
    address: str | None
    email: str | None


class Item(ExtractionModel):
    description: str | None
    product_code: str | None
    quantity: DecimalText | None
    unit: str | None
    unit_price: DecimalText | None
    discount: DecimalText | None
    tax_rate: DecimalText | None
    tax_amount: DecimalText | None
    net_amount: DecimalText | None
    total: DecimalText | None


class Tax(ExtractionModel):
    label: str | None
    rate: DecimalText | None
    taxable_amount: DecimalText | None
    amount: DecimalText | None


class ExtraField(ExtractionModel):
    label: str
    value: str


class Invoice(ExtractionModel):
    language: Literal["pt", "en", "mixed", "other", "unknown"]
    document_type: str | None
    invoice_number: str | None
    issue_date: date | None
    due_date: date | None
    supplier: Party
    customer: Party
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None
    subtotal: DecimalText | None
    discount_total: DecimalText | None
    tax_total: DecimalText | None
    total: DecimalText | None
    amount_due: DecimalText | None
    payment_method: str | None
    iban: str | None
    purchase_order: str | None
    items: list[Item]
    taxes: list[Tax]
    extra_fields: list[ExtraField]
    notes: str | None


class Extraction(ExtractionModel):
    is_invoice: bool = Field(
        description=(
            "True when the PDF contains one or more commercial billing documents, "
            "including invoices, invoice-receipts, receipts or ticket-invoices."
        )
    )
    invoice_count: int = Field(
        ge=0,
        description=(
            "Number of distinct billing documents. Count pages, payment receipts, "
            "tickets, booking confirmations, terms pages and duplicate copies for "
            "the same transaction as one document. Reservation, order, booking, "
            "payment and reference numbers are not separate invoice numbers; count "
            "separately only clearly different billing documents."
        ),
    )
    invoice: Invoice | None
    warnings: list[str]


class InvoiceRecord(BaseModel):
    id: str
    sha256: str
    filename: str
    processed_at: datetime
    page_count: int
    model: str
    needs_review: bool
    warnings: list[str]
    invoice: Invoice


class UploadResult(BaseModel):
    duplicate: bool
    record: InvoiceRecord


class CsvImportResult(BaseModel):
    imported: int
    duplicates: int
    total: int


class BatchFileResult(BaseModel):
    filename: str
    status: Literal["processed", "duplicate", "error"]
    duplicate: bool = False
    detail: str | None = None
    record: InvoiceRecord | None = None
    csv_content: str | None = None


class BatchUploadResult(BaseModel):
    mode: Literal["combined", "separate"]
    total: int
    processed: int
    duplicates: int
    failed: int
    results: list[BatchFileResult]
