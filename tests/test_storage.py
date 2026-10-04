import csv
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from app.models import InvoiceRecord
from app.storage import FIELDS, PREVIOUS_FIELDS, CsvStore, to_row


def record(invoice, number):
    return InvoiceRecord(
        id=str(number),
        sha256=str(number),
        filename="invoice.pdf",
        processed_at=datetime.now(UTC),
        page_count=1,
        model="test",
        needs_review=False,
        warnings=[],
        invoice=invoice,
    )


def test_concurrent_workers_keep_all_rows_and_deduplicate(tmp_path, invoice):
    path = tmp_path / "faturas.csv"

    def save(number):
        distinct_invoice = invoice.model_copy(update={"invoice_number": f"FT 2026/{number:03}"})
        return CsvStore(path).save(record(distinct_invoice, number))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(save, list(range(12)) * 2))
    rows = list(
        csv.DictReader(io.StringIO(CsvStore(path).export().decode("utf-8-sig")), delimiter=";")
    )
    assert len(rows) == 12
    assert sum(duplicate for _, duplicate in results) == 12


def test_failed_atomic_replace_keeps_previous_csv(tmp_path, invoice, monkeypatch):
    store = CsvStore(tmp_path / "faturas.csv")
    store.save(record(invoice, 1))
    original = store.export()

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr("app.storage.os.replace", fail)
    with pytest.raises(OSError):
        changed_invoice = invoice.model_copy(update={"invoice_number": "FT 2026/002"})
        store.save(record(changed_invoice, 2))
    assert store.export() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_index_failure_rolls_back_csv_and_index(tmp_path, invoice, monkeypatch):
    store = CsvStore(tmp_path / "faturas.csv")
    store.save(record(invoice, 1))
    original_csv = store.path.read_bytes()
    original_index = store.index_path.read_bytes()

    def fail(_index):
        raise OSError("sidecar failure")

    monkeypatch.setattr(store, "_write_index", fail)
    with pytest.raises(OSError):
        changed_invoice = invoice.model_copy(update={"invoice_number": "FT 2026/002"})
        store.save(record(changed_invoice, 2))
    assert store.path.read_bytes() == original_csv
    assert store.index_path.read_bytes() == original_index
    assert store.find("1") is not None


def test_missing_index_fails_closed_instead_of_disabling_deduplication(tmp_path, invoice):
    store = CsvStore(tmp_path / "faturas.csv")
    store.save(record(invoice, 1))
    store.index_path.unlink()
    with pytest.raises(ValueError, match="inconsistentes"):
        CsvStore(store.path).find("1")


def test_previous_csv_format_is_migrated_using_the_private_index(tmp_path, invoice):
    store = CsvStore(tmp_path / "faturas.csv")
    saved = record(invoice, 1)
    store.save(saved)
    old_row = {field: "" for field in PREVIOUS_FIELDS}
    with store.path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=PREVIOUS_FIELDS, delimiter=";")
        writer.writeheader()
        writer.writerow(old_row)
    migrated = CsvStore(store.path)
    migrated_rows = list(
        csv.DictReader(io.StringIO(migrated.export().decode("utf-8-sig")), delimiter=";")
    )
    assert migrated_rows[0]["outros_detalhes"] == "ATCUD: ABCD-001"
    assert "items" not in migrated_rows[0]
    assert migrated_rows[0].keys() == dict.fromkeys(FIELDS).keys()


def test_import_generated_csv_adds_only_rows_that_are_different(tmp_path, invoice):
    store = CsvStore(tmp_path / "faturas.csv")
    store.save(record(invoice, 1))
    second_invoice = invoice.model_copy(update={"invoice_number": "FT 2026/002"})
    second = record(second_invoice, 2)
    existing_row = next(
        csv.DictReader(io.StringIO(store.export().decode("utf-8-sig")), delimiter=";")
    )
    imported_row = to_row(second)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, delimiter=";")
    writer.writeheader()
    writer.writerow(existing_row)
    writer.writerow(imported_row)
    writer.writerow(existing_row)

    result = store.import_csv(buffer.getvalue().encode("utf-8-sig"))

    assert result == {"imported": 1, "duplicates": 2, "total": 2}
    rows = list(csv.DictReader(io.StringIO(store.export().decode("utf-8-sig")), delimiter=";"))
    assert len(rows) == 2
    assert {row["numero_fatura"] for row in rows} == {"FT 2026/001", "FT 2026/002"}


def test_import_csv_rejects_a_different_header(tmp_path):
    store = CsvStore(tmp_path / "faturas.csv")
    with pytest.raises(ValueError, match="cabeçalho"):
        store.import_csv(b"outro;formato\nvalor\n")


def test_pdf_matching_an_imported_row_is_not_added_again(tmp_path, invoice):
    store = CsvStore(tmp_path / "faturas.csv")
    source = record(invoice, 1)
    row = to_row(source)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, delimiter=";")
    writer.writeheader()
    writer.writerow(row)
    assert store.import_csv(buffer.getvalue().encode("utf-8-sig"))["imported"] == 1

    _, duplicate = store.save(record(invoice, "new-pdf-digest"))

    assert duplicate is True
    assert len(list(csv.DictReader(io.StringIO(store.export().decode("utf-8-sig")), delimiter=";"))) == 1


def test_different_pdf_digests_with_the_same_invoice_identity_are_deduplicated(tmp_path, invoice):
    store = CsvStore(tmp_path / "faturas.csv")
    first = record(invoice, "first-pdf-digest")
    second = record(invoice, "reexported-pdf-digest")
    store.save(first)

    saved, duplicate = store.save(second)

    assert duplicate is True
    assert saved == first
    assert len(list(csv.DictReader(io.StringIO(store.export().decode("utf-8-sig")), delimiter=";"))) == 1


def test_startup_removes_existing_semantic_duplicates(tmp_path, invoice):
    path = tmp_path / "faturas.csv"
    store = CsvStore(path)
    first = record(invoice, "first-pdf-digest")
    second = record(invoice, "reexported-pdf-digest")
    store._write_csv_rows([to_row(first), to_row(second)])
    store._write_index({first.sha256: first, second.sha256: second})

    migrated = CsvStore(path)

    rows = list(csv.DictReader(io.StringIO(migrated.export().decode("utf-8-sig")), delimiter=";"))
    assert len(rows) == 1
    assert migrated.find(first.sha256) == first
    assert migrated.find(second.sha256) is None
