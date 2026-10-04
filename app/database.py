"""Persistent PostgreSQL storage and account data for Render deployments."""

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import create_engine, text

from app.models import InvoiceRecord
from app.storage import FIELDS, csv_bytes, invoice_identities, record_from_csv_row, to_row

DEFAULT_CATEGORIES = (
    "Transportes",
    "Software",
    "Refeições",
    "Telecomunicações",
    "Combustível",
    "Outros",
)


@dataclass(frozen=True)
class User:
    id: str
    email: str
    plan: str
    subscription_status: str
    usage_count: int

    @property
    def subscribed(self) -> bool:
        return self.plan in {"monthly", "weekly"} and self.subscription_status in {
            "active",
            "trialing",
        }


class DatabaseStore:
    """Small SQL storage adapter with the same invoice API as CsvStore."""

    is_persistent = True

    def __init__(self, url: str):
        normalized = url.strip()
        if normalized.startswith("postgres://"):
            normalized = "postgresql+psycopg://" + normalized.removeprefix("postgres://")
        elif normalized.startswith("postgresql://"):
            normalized = "postgresql+psycopg://" + normalized.removeprefix("postgresql://")
        self.engine = create_engine(normalized, pool_pre_ping=True, pool_recycle=1800)
        self._create_schema()

    def _create_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS app_users (
                id VARCHAR(36) PRIMARY KEY,
                email VARCHAR(320) NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                plan VARCHAR(20) NOT NULL DEFAULT 'free',
                subscription_status VARCHAR(30) NOT NULL DEFAULT 'free',
                stripe_customer_id VARCHAR(255),
                stripe_subscription_id VARCHAR(255),
                usage_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS invoice_records (
                id VARCHAR(36) PRIMARY KEY,
                user_id VARCHAR(36) NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                sha256 VARCHAR(64) NOT NULL,
                record_json TEXT NOT NULL,
                category VARCHAR(100),
                cost_center VARCHAR(100),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, sha256)
            )
            """,
            "CREATE INDEX IF NOT EXISTS invoice_records_user_created ON invoice_records(user_id, created_at)",
        )
        with self.engine.begin() as connection:
            for statement in statements:
                connection.exec_driver_sql(statement)

    @staticmethod
    def _user(row) -> User | None:
        if row is None:
            return None
        return User(
            id=row["id"],
            email=row["email"],
            plan=row["plan"],
            subscription_status=row["subscription_status"],
            usage_count=int(row["usage_count"]),
        )

    def get_user(self, user_id: str) -> User | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text("SELECT id, email, plan, subscription_status, usage_count FROM app_users WHERE id=:id"),
                {"id": user_id},
            ).mappings().first()
        return self._user(row)

    def get_user_by_email(self, email: str) -> User | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text("SELECT id, email, plan, subscription_status, usage_count FROM app_users WHERE email=:email"),
                {"email": email.casefold()},
            ).mappings().first()
        return self._user(row)

    def create_user(self, email: str, password_hash: str) -> User:
        user_id = str(uuid4())
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO app_users (id, email, password_hash) VALUES (:id, :email, :password_hash)"
                ),
                {"id": user_id, "email": email.casefold(), "password_hash": password_hash},
            )
        return self.get_user(user_id)  # type: ignore[return-value]

    def password_hash(self, email: str) -> str | None:
        with self.engine.connect() as connection:
            return connection.execute(
                text("SELECT password_hash FROM app_users WHERE email=:email"),
                {"email": email.casefold()},
            ).scalar_one_or_none()

    def set_subscription(
        self,
        user_id: str,
        plan: str,
        status: str,
        customer_id: str | None = None,
        subscription_id: str | None = None,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE app_users SET plan=:plan, subscription_status=:status,
                        stripe_customer_id=COALESCE(:customer_id, stripe_customer_id),
                        stripe_subscription_id=COALESCE(:subscription_id, stripe_subscription_id)
                    WHERE id=:id
                    """
                ),
                {
                    "id": user_id,
                    "plan": plan,
                    "status": status,
                    "customer_id": customer_id,
                    "subscription_id": subscription_id,
                },
            )

    def find(self, digest: str, user_id: str | None = None) -> InvoiceRecord | None:
        if not user_id:
            return None
        with self.engine.connect() as connection:
            value = connection.execute(
                text("SELECT record_json FROM invoice_records WHERE user_id=:user_id AND sha256=:sha256"),
                {"user_id": user_id, "sha256": digest},
            ).scalar_one_or_none()
        return InvoiceRecord.model_validate_json(value) if value else None

    def save(
        self,
        record: InvoiceRecord,
        user_id: str | None = None,
    ) -> tuple[InvoiceRecord, bool]:
        if not user_id:
            raise ValueError("É necessário iniciar sessão para guardar faturas.")
        with self.engine.begin() as connection:
            existing_rows = connection.execute(
                text("SELECT record_json, sha256 FROM invoice_records WHERE user_id=:user_id"),
                {"user_id": user_id},
            ).mappings().all()
            identities = invoice_identities(record)
            for row in existing_rows:
                existing = InvoiceRecord.model_validate_json(row["record_json"])
                if row["sha256"] == record.sha256 or identities & invoice_identities(existing):
                    return existing, True
            connection.execute(
                text(
                    """
                    INSERT INTO invoice_records
                        (id, user_id, sha256, record_json, category, cost_center)
                    VALUES (:id, :user_id, :sha256, :record_json, :category, :cost_center)
                    """
                ),
                {
                    "id": record.id,
                    "user_id": user_id,
                    "sha256": record.sha256,
                    "record_json": record.model_dump_json(),
                    "category": record.category,
                    "cost_center": record.cost_center,
                },
            )
            connection.execute(
                text("UPDATE app_users SET usage_count=usage_count+1 WHERE id=:id"),
                {"id": user_id},
            )
        return record, False

    def export(self, user_id: str | None = None) -> bytes:
        if not user_id:
            return csv_bytes([])
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT record_json FROM invoice_records WHERE user_id=:user_id ORDER BY created_at, id"),
                {"user_id": user_id},
            ).scalars().all()
        return csv_bytes([to_row(InvoiceRecord.model_validate_json(value)) for value in rows])

    def import_csv(self, data: bytes, user_id: str | None = None) -> dict[str, int]:
        if not user_id:
            raise ValueError("É necessário iniciar sessão para importar um CSV.")
        try:
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("O CSV deve estar codificado em UTF-8.") from exc
        reader = csv.DictReader(io.StringIO(content), delimiter=";")
        if reader.fieldnames != FIELDS:
            raise ValueError("O CSV tem um cabeçalho incompatível com o formato da aplicação.")
        imported = duplicates = 0
        for row in reader:
            if not any(row.values()):
                continue
            digest = "csv:" + hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
            record = record_from_csv_row(row, digest)
            _, duplicate = self.save(record, user_id)
            duplicates += duplicate
            imported += not duplicate
        return {"imported": imported, "duplicates": duplicates, "total": imported + duplicates}

    def usage(self, user_id: str) -> User:
        user = self.get_user(user_id)
        if user is None:
            raise ValueError("Conta não encontrada.")
        return user
