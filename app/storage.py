import csv
import hashlib
import io
import json
import os
import re
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from filelock import FileLock

from app.models import ExtraField, Invoice, InvoiceRecord, Party, Tax

csv.field_size_limit(10 * 1024 * 1024)
DECIMAL_CELL = re.compile(r"^-?\d+(?:\.\d+)?$")

FIELDS = [
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
    "categoria",
    "centro_custo",
]

# Current CSV format before the Portuguese headers were introduced. Keep these
# names so existing installations can be migrated without losing their data.
PREVIOUS_FIELDS = [
    "document_type",
    "invoice_number",
    "issue_date",
    "due_date",
    "supplier_name",
    "supplier_tax_id",
    "supplier_address",
    "supplier_email",
    "customer_name",
    "customer_tax_id",
    "customer_address",
    "customer_email",
    "currency",
    "subtotal",
    "discount_total",
    "tax_total",
    "total",
    "amount_due",
    "payment_method",
    "iban",
    "purchase_order",
    "notes",
    "items",
    "taxes",
    "other_details",
]

PUBLIC_FIELDS_BEFORE_CATEGORIES = [field for field in FIELDS if field not in {"categoria", "centro_custo"}]

OLDER_FIELDS = [field for field in PREVIOUS_FIELDS if field != "other_details"]

LEGACY_FIELDS = [
    "id",
    "sha256",
    "filename",
    "processed_at",
    "page_count",
    "model",
    "needs_review",
    "language",
    "document_type",
    "invoice_number",
    "issue_date",
    "due_date",
    "supplier_name",
    "supplier_tax_id",
    "supplier_address",
    "supplier_email",
    "customer_name",
    "customer_tax_id",
    "customer_address",
    "customer_email",
    "currency",
    "subtotal",
    "discount_total",
    "tax_total",
    "total",
    "amount_due",
    "payment_method",
    "iban",
    "purchase_order",
    "notes",
    "items_json",
    "taxes_json",
    "extra_fields_json",
    "warnings",
    "record_json",
]


def safe_cell(value: object) -> str:
    text = "" if value is None else str(value)
    # Quoting a CSV cell alone does not stop Excel from evaluating a formula.
    if (
        text.lstrip().startswith(("=", "+", "@"))
        or (text.startswith("-") and not DECIMAL_CELL.fullmatch(text.strip()))
        or text.startswith(("\t", "\r", "\n"))
    ):
        return "'" + text
    return text


def csv_bytes(rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, delimiter=";")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def row_fingerprint(row: dict[str, str]) -> str:
    value = json.dumps(
        [row.get(field, "") for field in FIELDS], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def invoice_identities(record: InvoiceRecord) -> set[str]:
    """Return stable commercial identities that survive PDF re-exports.

    The PDF bytes and the extracted prose can change when the same document is
    downloaded or rendered again. A supplier plus invoice number is the strongest
    identity; the fallback is deliberately stricter for documents without a number.
    """
    invoice = record.invoice

    def token(value: object) -> str:
        return re.sub(r"[^\w]+", "", str(value or "").casefold(), flags=re.UNICODE)

    number = token(invoice.invoice_number)
    supplier_tax_id = token(invoice.supplier.tax_id)
    supplier_name = token(invoice.supplier.name)
    identities: set[str] = set()
    if number:
        if supplier_tax_id:
            identities.add(f"number:{supplier_tax_id}:{number}")
        if supplier_name:
            identities.add(f"number-name:{supplier_name}:{number}")

    date_value = invoice.issue_date.isoformat() if invoice.issue_date else ""
    total = token(invoice.total)
    currency = token(invoice.currency)
    customer = token(invoice.customer.tax_id or invoice.customer.name)
    supplier = supplier_tax_id or supplier_name
    if not number and supplier and date_value and total and currency and customer:
        identities.add(f"fallback:{supplier}:{date_value}:{currency}:{total}:{customer}")
    return identities


def _clean_import_value(value: str | None) -> str | None:
    return value.strip() if value and value.strip() else None


def _import_date(value: str | None) -> date | None:
    value = _clean_import_value(value)
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _import_decimal(value: str | None) -> str | None:
    value = _clean_import_value(value)
    return value if value and DECIMAL_CELL.fullmatch(value) else None


def _import_taxes(value: str | None) -> list[Tax]:
    taxes = []
    for part in (_clean_import_value(value) or "").split(";"):
        part = part.strip()
        if not part:
            continue
        tokens = part.split()
        label_part, separator, amount_part = part.partition(":")
        amount = _import_decimal(amount_part.strip() if separator else (tokens.pop() if tokens else None))
        if separator:
            tokens = label_part.split()
        rate = None
        if tokens and tokens[-1].endswith("%"):
            rate = _import_decimal(tokens.pop()[:-1])
        taxes.append(
            Tax(
                label=_normalize_tax_label(" ".join(tokens) or None),
                rate=rate,
                taxable_amount=None,
                amount=amount,
            )
        )
    return taxes


def _normalize_tax_label(label: str | None) -> str | None:
    value = re.sub(r"\s+", " ", (label or "").strip())
    if not value:
        return None
    # Portuguese invoices use prefixes such as "PT IVA*" and descriptions such
    # as "IVA Reduzido" for the same tax. Keep the export label predictable.
    value = re.sub(r"^[A-Z]{2}\s+", "", value)
    if "iva" in value.casefold():
        return "IVA"
    return value.rstrip("*").strip() or None


def _tax_cell(tax: dict[str, str | None]) -> str:
    label = _normalize_tax_label(tax.get("label")) or "Imposto"
    rate = f" {tax['rate']}%" if tax.get("rate") else ""
    amount = f": {tax['amount']}" if tax.get("amount") else ""
    return f"{label}{rate}{amount}"


def _import_extra_fields(value: str | None) -> list[ExtraField]:
    fields = []
    for part in (_clean_import_value(value) or "").split(";"):
        label, separator, field_value = part.partition(":")
        if separator and label.strip() and field_value.strip():
            fields.append(ExtraField(label=label.strip(), value=field_value.strip()))
    return fields


def record_from_csv_row(row: dict[str, str], digest: str) -> InvoiceRecord:
    """Create a private index record for a row imported from our own CSV."""
    currency = _clean_import_value(row.get("moeda"))
    if currency and not re.fullmatch(r"[A-Z]{3}", currency):
        currency = None
    invoice = Invoice(
        language="unknown",
        document_type=_clean_import_value(row.get("tipo_documento")),
        invoice_number=_clean_import_value(row.get("numero_fatura")),
        issue_date=_import_date(row.get("data_emissao")),
        due_date=_import_date(row.get("data_vencimento")),
        supplier=Party(
            name=_clean_import_value(row.get("nome_fornecedor")),
            tax_id=_clean_import_value(row.get("nif_fornecedor")),
            address=_clean_import_value(row.get("morada_fornecedor")),
            email=_clean_import_value(row.get("email_fornecedor")),
        ),
        customer=Party(
            name=_clean_import_value(row.get("nome_cliente")),
            tax_id=_clean_import_value(row.get("nif_cliente")),
            address=_clean_import_value(row.get("morada_cliente")),
            email=_clean_import_value(row.get("email_cliente")),
        ),
        currency=currency,
        subtotal=_import_decimal(row.get("subtotal")),
        discount_total=_import_decimal(row.get("desconto_total")),
        tax_total=_import_decimal(row.get("total_impostos")),
        total=_import_decimal(row.get("total")),
        amount_due=_import_decimal(row.get("valor_em_divida")),
        payment_method=_clean_import_value(row.get("metodo_pagamento")),
        iban=_clean_import_value(row.get("iban")),
        purchase_order=_clean_import_value(row.get("ordem_compra")),
        items=[],
        taxes=_import_taxes(row.get("impostos")),
        extra_fields=_import_extra_fields(row.get("outros_detalhes")),
        notes=_clean_import_value(row.get("observacoes")),
    )
    return InvoiceRecord(
        id=str(uuid4()),
        sha256=digest,
        filename="importado.csv",
        processed_at=datetime.now(UTC),
        page_count=0,
        model="csv-import",
        needs_review=False,
        warnings=[],
        invoice=invoice,
        category=_clean_import_value(row.get("categoria")),
        cost_center=_clean_import_value(row.get("centro_custo")),
    )


def to_row(record: InvoiceRecord) -> dict[str, str]:
    invoice = record.invoice.model_dump(mode="json")
    raw = {
        "tipo_documento": invoice.get("document_type"),
        "numero_fatura": invoice.get("invoice_number"),
        "data_emissao": invoice.get("issue_date"),
        "data_vencimento": invoice.get("due_date"),
        "nome_fornecedor": invoice["supplier"].get("name"),
        "nif_fornecedor": invoice["supplier"].get("tax_id"),
        "morada_fornecedor": invoice["supplier"].get("address"),
        "email_fornecedor": invoice["supplier"].get("email"),
        "nome_cliente": invoice["customer"].get("name"),
        "nif_cliente": invoice["customer"].get("tax_id"),
        "morada_cliente": invoice["customer"].get("address"),
        "email_cliente": invoice["customer"].get("email"),
        "moeda": invoice.get("currency"),
        "subtotal": invoice.get("subtotal"),
        "desconto_total": invoice.get("discount_total"),
        "total_impostos": invoice.get("tax_total"),
        "total": invoice.get("total"),
        "valor_em_divida": invoice.get("amount_due"),
        "metodo_pagamento": invoice.get("payment_method"),
        "iban": invoice.get("iban"),
        "ordem_compra": invoice.get("purchase_order"),
        "observacoes": invoice.get("notes"),
        "categoria": record.category,
        "centro_custo": record.cost_center,
    }
    raw["impostos"] = "; ".join(_tax_cell(tax) for tax in invoice["taxes"])
    raw["outros_detalhes"] = "; ".join(
        f"{field['label']}: {field['value']}" for field in invoice["extra_fields"]
    )
    return {field: safe_cell(raw.get(field)) for field in FIELDS}


class CsvStore:
    """Small-volume local store. Lock + atomic replace protect concurrent workers."""

    def __init__(self, path: Path):
        self.path = path
        self.index_path = path.with_name(path.name + ".index.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(path) + ".lock", timeout=30)
        self._migrate_legacy_csv()
        self._deduplicate_current_records()
        self._normalize_current_tax_rows()
        if self.index_path.exists():
            self.index_path.chmod(0o600)

    def _write_csv_rows(self, rows: list[dict[str, str]]) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8-sig",
                newline="",
                dir=self.path.parent,
                prefix=".invoices-",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary = Path(file.name)
                writer = csv.DictWriter(file, fieldnames=FIELDS, delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _migrate_legacy_csv(self) -> None:
        """Migrate old CSV headers to the Portuguese public CSV format."""
        if not self.path.exists():
            return
        with self.lock:
            with self.path.open(encoding="utf-8-sig", newline="") as file:
                reader = csv.DictReader(file, delimiter=";")
                if reader.fieldnames == PUBLIC_FIELDS_BEFORE_CATEGORIES:
                    rows = list(reader)
                    self._write_csv_rows(
                        [
                            {
                                **row,
                                "categoria": row.get("categoria", ""),
                                "centro_custo": row.get("centro_custo", ""),
                            }
                            for row in rows
                        ]
                    )
                    return
                if reader.fieldnames not in (LEGACY_FIELDS, PREVIOUS_FIELDS, OLDER_FIELDS):
                    return
                rows = list(reader)
            if reader.fieldnames == LEGACY_FIELDS:
                records = [InvoiceRecord.model_validate_json(row["record_json"]) for row in rows]
                index = {record.sha256: record for record in records}
                self._write_csv_rows([to_row(record) for record in records])
                self._write_index(index)
                return

            index = self._read_index() if self.index_path.exists() else {}
            if index and len(index) == len(rows):
                self._write_csv_rows([to_row(record) for record in index.values()])
            else:
                self._write_csv_rows([self._migrate_previous_row(row) for row in rows])

    @staticmethod
    def _migrate_previous_row(row: dict[str, str]) -> dict[str, str]:
        mapping = {
            "tipo_documento": "document_type",
            "numero_fatura": "invoice_number",
            "data_emissao": "issue_date",
            "data_vencimento": "due_date",
            "nome_fornecedor": "supplier_name",
            "nif_fornecedor": "supplier_tax_id",
            "morada_fornecedor": "supplier_address",
            "email_fornecedor": "supplier_email",
            "nome_cliente": "customer_name",
            "nif_cliente": "customer_tax_id",
            "morada_cliente": "customer_address",
            "email_cliente": "customer_email",
            "moeda": "currency",
            "subtotal": "subtotal",
            "desconto_total": "discount_total",
            "total_impostos": "tax_total",
            "total": "total",
            "valor_em_divida": "amount_due",
            "metodo_pagamento": "payment_method",
            "iban": "iban",
            "ordem_compra": "purchase_order",
            "observacoes": "notes",
            "impostos": "taxes",
            "outros_detalhes": "other_details",
        }
        return {field: safe_cell(row.get(source, "")) for field, source in mapping.items()}

    def _read_index(self) -> dict[str, InvoiceRecord]:
        if not self.index_path.exists():
            return {}
        payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        return {digest: InvoiceRecord.model_validate(value) for digest, value in payload.items()}

    def _deduplicate_current_records(self) -> None:
        """Remove semantic duplicates already present from older deployments."""
        if not self.path.exists() or not self.index_path.exists():
            return
        with self.lock:
            rows = self._read()
            index = self._read_index()
            if len(rows) != len(index):
                return
            kept_rows: list[dict[str, str]] = []
            kept_index: dict[str, InvoiceRecord] = {}
            known_fingerprints: set[str] = set()
            known_identities: set[str] = set()
            for row, (digest, record) in zip(rows, index.items()):
                fingerprint = row_fingerprint(row)
                identities = invoice_identities(record)
                if fingerprint in known_fingerprints or identities & known_identities:
                    continue
                kept_rows.append(row)
                kept_index[digest] = record
                known_fingerprints.add(fingerprint)
                known_identities.update(identities)
            if len(kept_rows) == len(rows):
                return
            previous_csv = self.path.read_bytes()
            previous_index = self.index_path.read_bytes()
            try:
                self._write_csv_rows(kept_rows)
                self._write_index(kept_index)
            except Exception:
                self._restore_file(self.path, previous_csv)
                self._restore_file(self.index_path, previous_index)
                raise

    def _normalize_current_tax_rows(self) -> None:
        """Rewrite existing rows using the current normalized tax display."""
        if not self.path.exists() or not self.index_path.exists():
            return
        with self.lock:
            rows = self._read()
            index = self._read_index()
            if len(rows) != len(index):
                return
            normalized_rows = [to_row(record) for record in index.values()]
            if normalized_rows == rows:
                return
            previous_csv = self.path.read_bytes()
            try:
                self._write_csv_rows(normalized_rows)
            except Exception:
                self._restore_file(self.path, previous_csv)
                raise

    def _write_index(self, index: dict[str, InvoiceRecord]) -> None:
        temporary = self.index_path.with_name("." + self.index_path.name + ".tmp")
        try:
            temporary.write_text(
                json.dumps(
                    {digest: record.model_dump(mode="json") for digest, record in index.items()},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            os.replace(temporary, self.index_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _read(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file, delimiter=";")
            if reader.fieldnames != FIELDS:
                raise ValueError("O CSV existente tem um formato incompatível.")
            return list(reader)

    def import_csv(self, data: bytes, user_id: str | None = None) -> dict[str, int]:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("O CSV deve estar codificado em UTF-8.") from exc
        reader = csv.DictReader(io.StringIO(text), delimiter=";")
        if reader.fieldnames != FIELDS:
            raise ValueError("O CSV tem um cabeçalho incompatível com o formato da aplicação.")
        imported_rows = []
        for row in reader:
            if row.get(None) is not None:
                raise ValueError("O CSV contém colunas adicionais não suportadas.")
            normalized = {field: safe_cell(row.get(field, "")) for field in FIELDS}
            if any(normalized.values()):
                imported_rows.append(normalized)

        with self.lock:
            rows = self._read()
            index = self._read_index()
            if len(rows) != len(index):
                raise ValueError("O CSV e o índice de deduplicação estão inconsistentes.")
            known = {row_fingerprint(row) for row in rows}
            known_identities = set()
            for record in index.values():
                known_identities.update(invoice_identities(record))
            added_rows = []
            added_records = {}
            duplicates = 0
            for row in imported_rows:
                fingerprint = row_fingerprint(row)
                digest = f"csv:{fingerprint}"
                candidate = record_from_csv_row(row, digest)
                identities = invoice_identities(candidate)
                if fingerprint in known or identities & known_identities:
                    duplicates += 1
                    continue
                while digest in index or digest in added_records:
                    digest = f"csv:{hashlib.sha256(digest.encode('utf-8')).hexdigest()}"
                known.add(fingerprint)
                known_identities.update(identities)
                added_rows.append(row)
                candidate.sha256 = digest
                added_records[digest] = candidate

            previous_csv = self.path.read_bytes() if self.path.exists() else None
            previous_index = self.index_path.read_bytes() if self.index_path.exists() else None
            try:
                self._write_csv_rows(rows + added_rows)
                index.update(added_records)
                self._write_index(index)
            except Exception:
                self._restore_file(self.path, previous_csv)
                self._restore_file(self.index_path, previous_index)
                raise
        return {
            "imported": len(added_rows),
            "duplicates": duplicates,
            "total": len(rows) + len(added_rows),
        }

    def find(self, digest: str, user_id: str | None = None) -> InvoiceRecord | None:
        with self.lock:
            rows = self._read()
            index = self._read_index()
            if len(rows) != len(index):
                raise ValueError("O CSV e o índice de deduplicação estão inconsistentes.")
            return index.get(digest)

    def _restore_file(self, path: Path, previous: bytes | None) -> None:
        if previous is None:
            path.unlink(missing_ok=True)
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=".restore-", suffix=".tmp", delete=False
            ) as file:
                temporary = Path(file.name)
                file.write(previous)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def save(self, record: InvoiceRecord, user_id: str | None = None) -> tuple[InvoiceRecord, bool]:
        with self.lock:
            rows = self._read()
            index = self._read_index()
            if len(rows) != len(index):
                raise ValueError("O CSV e o índice de deduplicação estão inconsistentes.")
            # Recheck under lock: another worker may have finished the same PDF.
            if record.sha256 in index:
                return index[record.sha256], True
            new_row = to_row(record)
            new_fingerprint = row_fingerprint(new_row)
            new_identities = invoice_identities(record)
            for row, indexed_record in zip(rows, index.values()):
                if new_identities & invoice_identities(indexed_record):
                    return indexed_record, True
                if indexed_record.model == "csv-import" and row_fingerprint(row) == new_fingerprint:
                    return indexed_record, True
            previous_csv = self.path.read_bytes() if self.path.exists() else None
            previous_index = self.index_path.read_bytes() if self.index_path.exists() else None
            try:
                self._write_csv_rows(rows + [new_row])
                index[record.sha256] = record
                self._write_index(index)
            except Exception:
                self._restore_file(self.path, previous_csv)
                self._restore_file(self.index_path, previous_index)
                raise
            return record, False

    def export(self, user_id: str | None = None) -> bytes:
        with self.lock:
            if self.path.exists():
                return self.path.read_bytes()
            buffer = io.StringIO(newline="")
            csv.DictWriter(buffer, fieldnames=FIELDS, delimiter=";").writeheader()
            return buffer.getvalue().encode("utf-8-sig")
