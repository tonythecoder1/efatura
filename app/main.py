import hashlib
import logging
import secrets
import tempfile
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from filelock import Timeout
from openai import APITimeoutError, AuthenticationError, OpenAIError, RateLimitError
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware

from app.auth import create_token, decode_token, hash_password, verify_password
from app.config import Settings
from app.database import DEFAULT_CATEGORIES, DatabaseStore, User
from app.extractor import (
    ExtractionFailed,
    InvalidDocument,
    OpenAIExtractor,
    review_warnings,
    validate_pdf,
)
from app.models import (
    AuthCredentials,
    AuthResponse,
    BatchFileResult,
    BatchUploadResult,
    BillingStatus,
    CategoryList,
    CheckoutRequest,
    CsvImportResult,
    InvoiceRecord,
    UploadResult,
)
from app.storage import CsvStore, csv_bytes, to_row

logger = logging.getLogger(__name__)


class UploadLimitMiddleware:
    """Bound the entire multipart body, including uploads without Content-Length."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        body_limit = self.max_bytes * 10 if scope.get("path") == "/v1/invoices/batch" else self.max_bytes
        # Spool instead of keeping every incoming chunk in RAM. Closed on all exits.
        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as body:
            total = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                total += len(chunk)
                if total > body_limit:
                    response = JSONResponse(
                        status_code=413, content={"detail": "Upload demasiado grande."}
                    )
                    return await response(scope, receive, send)
                await run_in_threadpool(body.write, chunk)
                if not message.get("more_body", False):
                    break
            body.seek(0)
            remaining = total

            async def replay():
                nonlocal remaining
                if remaining < 0:
                    return await receive()
                chunk = await run_in_threadpool(body.read, 65536)
                remaining -= len(chunk)
                more = remaining > 0
                if not more:
                    remaining = -1
                return {"type": "http.request", "body": chunk, "more_body": more}

            await self.app(scope, replay, send)


def create_app(settings: Settings | None = None, extractor=None) -> FastAPI:
    settings = settings or Settings()
    store = DatabaseStore(settings.database_url) if settings.database_url else CsvStore(settings.csv_path)
    database = store if isinstance(store, DatabaseStore) else None
    parser = extractor if extractor is not None else OpenAIExtractor(settings)
    api = FastAPI(
        title="API de Faturas PDF",
        description="Extrai faturas em português/inglês e guarda os dados num CSV local.",
        version="0.1.0",
    )
    api.add_middleware(UploadLimitMiddleware, max_bytes=settings.max_upload_bytes + 1024 * 1024)
    api.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["X-API-Key", "Authorization", "Content-Type"],
    )
    key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def require_access(
        key: Annotated[str | None, Depends(key_header)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> str | None:
        if database is not None:
            secret = settings.auth_secret.get_secret_value()
            if not secret:
                raise HTTPException(status_code=503, detail="AUTH_SECRET não está configurado.")
            token = authorization.removeprefix("Bearer ").strip() if authorization else ""
            user_id = decode_token(token, secret) if token else None
            if not user_id or database.get_user(user_id) is None:
                raise HTTPException(status_code=401, detail="Inicia sessão para continuar.")
            return user_id
        expected = settings.api_key.get_secret_value()
        if expected and not secrets.compare_digest((key or "").encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="Chave de acesso inválida.")
        return None

    def auth_user(user_id: str) -> User:
        if database is None:
            raise HTTPException(status_code=503, detail="Autenticação persistente não está configurada.")
        user = database.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="A conta já não existe.")
        return user

    def user_payload(user: User) -> dict[str, str | int]:
        return {
            "id": user.id,
            "email": user.email,
            "plan": user.plan,
            "subscription_status": user.subscription_status,
            "usage_count": user.usage_count,
        }

    @api.post("/v1/auth/register", response_model=AuthResponse, tags=["Autenticação"])
    def register(credentials: AuthCredentials):
        if database is None:
            raise HTTPException(status_code=503, detail="Base de dados não configurada.")
        if not settings.auth_secret.get_secret_value():
            raise HTTPException(status_code=503, detail="AUTH_SECRET não está configurado.")
        email = credentials.email.strip().casefold()
        if "@" not in email or len(email) > 320:
            raise HTTPException(status_code=422, detail="Indica um email válido.")
        if len(credentials.password) < 8:
            raise HTTPException(status_code=422, detail="A palavra-passe deve ter pelo menos 8 caracteres.")
        if database.get_user_by_email(email):
            raise HTTPException(status_code=409, detail="Já existe uma conta com esse email.")
        try:
            user = database.create_user(email, hash_password(credentials.password))
        except Exception as exc:
            logger.warning("User registration failed: %s", type(exc).__name__)
            raise HTTPException(status_code=409, detail="Não foi possível criar a conta.") from exc
        token = create_token(user.id, settings.auth_secret.get_secret_value(), settings.auth_token_days * 86400)
        return AuthResponse(token=token, user=user_payload(user))

    @api.post("/v1/auth/login", response_model=AuthResponse, tags=["Autenticação"])
    def login(credentials: AuthCredentials):
        if database is None:
            raise HTTPException(status_code=503, detail="Base de dados não configurada.")
        if not settings.auth_secret.get_secret_value():
            raise HTTPException(status_code=503, detail="AUTH_SECRET não está configurado.")
        email = credentials.email.strip().casefold()
        user = database.get_user_by_email(email)
        password_hash = database.password_hash(email)
        if user is None or password_hash is None or not verify_password(credentials.password, password_hash):
            raise HTTPException(status_code=401, detail="Email ou palavra-passe inválidos.")
        token = create_token(user.id, settings.auth_secret.get_secret_value(), settings.auth_token_days * 86400)
        return AuthResponse(token=token, user=user_payload(user))

    @api.get("/v1/auth/me", response_model=AuthResponse, tags=["Autenticação"])
    def me(user_id: str = Depends(require_access)):
        user = auth_user(user_id)
        token = create_token(user.id, settings.auth_secret.get_secret_value(), settings.auth_token_days * 86400)
        return AuthResponse(token=token, user=user_payload(user))

    @api.get("/health", tags=["Estado"])
    def health():
        return {
            "status": "ok",
            "extraction_configured": bool(settings.openai_api_key.get_secret_value()),
            "accepted_formats": ["pdf"],
            "max_upload_mb": settings.max_upload_mb,
            "max_pages": settings.max_pages,
            "persistent_storage": database is not None,
            "auth_enabled": database is not None,
            "free_invoice_limit": settings.free_invoice_limit,
        }

    def process_pdf(
        data: bytes,
        filename: str,
        user_id: str | None = None,
        category: str | None = None,
        cost_center: str | None = None,
    ) -> tuple[InvoiceRecord, bool]:
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413, detail=f"Limite: {settings.max_upload_mb} MB por PDF."
            )
        try:
            page_count = validate_pdf(data, settings.max_pages)
        except InvalidDocument as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        digest = hashlib.sha256(data).hexdigest()
        existing = store.find(digest, user_id)
        if existing:
            return existing, True
        if database is not None and user_id:
            usage = database.usage(user_id)
            if not usage.subscribed and usage.usage_count >= settings.free_invoice_limit:
                raise HTTPException(
                    status_code=402,
                    detail=(
                        f"Atingiste as {settings.free_invoice_limit} faturas gratuitas. "
                        "Escolhe um plano para continuar."
                    ),
                )
        if extractor is None and not settings.openai_api_key.get_secret_value():
            raise HTTPException(status_code=503, detail="Configura OPENAI_API_KEY no ficheiro .env.")
        try:
            result = parser.extract(data)
        except APITimeoutError as exc:
            raise HTTPException(
                status_code=504, detail="O serviço de análise excedeu o tempo limite."
            ) from exc
        except RateLimitError as exc:
            raise HTTPException(
                status_code=503, detail="Limite ou saldo do serviço de IA excedido."
            ) from exc
        except AuthenticationError as exc:
            raise HTTPException(status_code=503, detail="A chave do serviço de IA é inválida.") from exc
        except (OpenAIError, ExtractionFailed, ValidationError) as exc:
            logger.warning("Invoice extraction failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=502, detail="Não foi possível concluir a análise do PDF."
            ) from exc

        if not result.is_invoice or result.invoice_count == 0:
            raise HTTPException(status_code=422, detail="Não foi identificada uma fatura neste PDF.")
        if result.invoice_count != 1:
            raise HTTPException(
                status_code=422,
                detail="O PDF contém transações independentes. Envia cada transação num PDF separado.",
            )
        if result.invoice is None:
            raise HTTPException(status_code=502, detail="O serviço não devolveu os dados da fatura.")
        warnings = review_warnings(result.invoice, result.warnings)
        record = InvoiceRecord(
            id=str(uuid4()),
            sha256=digest,
            filename=filename.replace("\\", "/").rsplit("/", 1)[-1],
            processed_at=datetime.now(UTC),
            page_count=page_count,
            model=settings.openai_model,
            needs_review=bool(warnings),
            warnings=warnings,
            invoice=result.invoice,
            category=category.strip() if category and category.strip() else None,
            cost_center=cost_center.strip() if cost_center and cost_center.strip() else None,
        )
        return store.save(record, user_id)

    @api.get("/v1/categories", response_model=CategoryList, tags=["Faturas"])
    def categories(user_id: str = Depends(require_access)):
        return CategoryList(categories=list(DEFAULT_CATEGORIES))

    @api.get("/v1/billing/status", response_model=BillingStatus, tags=["Pagamentos"])
    def billing_status(user_id: str = Depends(require_access)):
        user = auth_user(user_id)
        remaining = max(0, settings.free_invoice_limit - user.usage_count)
        return BillingStatus(
            plan=user.plan,
            subscription_status=user.subscription_status,
            used=user.usage_count,
            free_limit=settings.free_invoice_limit,
            remaining_free=remaining,
            subscribed=user.subscribed,
        )

    @api.post("/v1/billing/checkout", tags=["Pagamentos"])
    def billing_checkout(payload: CheckoutRequest, user_id: str = Depends(require_access)):
        user = auth_user(user_id)
        if not settings.stripe_secret_key.get_secret_value():
            raise HTTPException(status_code=503, detail="STRIPE_SECRET_KEY não está configurado.")
        price_id = (
            settings.stripe_monthly_price_id
            if payload.interval == "monthly"
            else settings.stripe_weekly_price_id
        )
        if not price_id:
            raise HTTPException(status_code=503, detail="O preço escolhido ainda não está configurado.")
        import stripe

        stripe.api_key = settings.stripe_secret_key.get_secret_value()
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=user.email,
            client_reference_id=user.id,
            metadata={"user_id": user.id, "plan": payload.interval},
            subscription_data={"metadata": {"user_id": user.id, "plan": payload.interval}},
            success_url=f"{settings.app_base_url}/?billing=success",
            cancel_url=f"{settings.app_base_url}/?billing=cancelled",
        )
        return {"url": session.url}

    @api.post("/v1/billing/webhook", tags=["Pagamentos"])
    async def billing_webhook(request: Request):
        if database is None or not settings.stripe_webhook_secret.get_secret_value():
            raise HTTPException(status_code=503, detail="Webhook Stripe não está configurado.")
        import stripe

        payload = await request.body()
        signature = request.headers.get("stripe-signature", "")
        try:
            event = stripe.Webhook.construct_event(
                payload, signature, settings.stripe_webhook_secret.get_secret_value()
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Webhook Stripe inválido.") from exc
        event_type = event.get("type")
        obj = event.get("data", {}).get("object", {})
        metadata = obj.get("metadata", {}) or {}
        user_id = metadata.get("user_id") or obj.get("client_reference_id")
        if user_id:
            if event_type in {"checkout.session.completed", "customer.subscription.updated"}:
                plan = metadata.get("plan", "monthly")
                database.set_subscription(
                    user_id,
                    plan,
                    "active",
                    str(obj.get("customer")) if obj.get("customer") else None,
                    str(obj.get("subscription")) if obj.get("subscription") else None,
                )
            elif event_type == "customer.subscription.deleted":
                database.set_subscription(user_id, "free", "canceled")
        return {"received": True}

    @api.post(
        "/v1/invoices",
        response_model=UploadResult,
        status_code=201,
        dependencies=[Depends(require_access)],
        tags=["Faturas"],
        responses={200: {"description": "PDF já registado"}},
    )
    def upload_invoice(
        response: Response,
        file: Annotated[UploadFile, File()],
        user_id: str | None = Depends(require_access),
    ):
        """Envia uma transação (uma ou várias páginas). Repete o PDF sem duplicar o CSV."""
        try:
            if not file.filename or not file.filename.lower().endswith(".pdf"):
                raise HTTPException(status_code=415, detail="Apenas são aceites ficheiros .pdf.")
            data = file.file.read(settings.max_upload_bytes + 1)
        finally:
            file.file.close()
        try:
            stored, duplicate = process_pdf(data, file.filename, user_id)
            if duplicate:
                response.status_code = 200
            return UploadResult(duplicate=duplicate, record=stored)
        except HTTPException:
            raise
        except (OSError, Timeout, ValueError) as exc:
            logger.error("Invoice storage failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Não foi possível aceder ao CSV.") from exc

    @api.post(
        "/v1/invoices/import-csv",
        response_model=CsvImportResult,
        dependencies=[Depends(require_access)],
        tags=["Faturas"],
    )
    def import_csv(csv_file: Annotated[UploadFile, File()], user_id: str | None = Depends(require_access)):
        """Importa um CSV criado pela aplicação e ignora linhas já existentes."""
        try:
            if not csv_file.filename or not csv_file.filename.lower().endswith(".csv"):
                raise HTTPException(status_code=415, detail="Apenas são aceites ficheiros .csv.")
            data = csv_file.file.read(10 * 1024 * 1024 + 1)
        finally:
            csv_file.file.close()
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Limite: 10 MB por CSV.")
        try:
            return store.import_csv(data, user_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (OSError, Timeout) as exc:
            logger.error("CSV import failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Não foi possível aceder ao CSV.") from exc

    @api.post(
        "/v1/invoices/batch",
        response_model=BatchUploadResult,
        dependencies=[Depends(require_access)],
        tags=["Faturas"],
    )
    def upload_batch(
        files: Annotated[list[UploadFile], File()],
        mode: Annotated[Literal["combined", "separate"], Form()] = "combined",
        category: Annotated[str | None, Form()] = None,
        cost_center: Annotated[str | None, Form()] = None,
        user_id: str | None = Depends(require_access),
    ):
        """Processa entre 1 e 10 PDFs e devolve CSV combinado ou individual."""
        if not files or len(files) > 10:
            raise HTTPException(status_code=422, detail="Envia entre 1 e 10 faturas por lote.")
        results: list[BatchFileResult] = []
        processed = duplicates = failed = 0
        for upload in files:
            filename = upload.filename or "fatura.pdf"
            try:
                if not filename.lower().endswith(".pdf"):
                    raise HTTPException(status_code=415, detail="Apenas são aceites ficheiros .pdf.")
                data = upload.file.read(settings.max_upload_bytes + 1)
                stored, duplicate = process_pdf(data, filename, user_id, category, cost_center)
                processed += not duplicate
                duplicates += duplicate
                results.append(
                    BatchFileResult(
                        filename=filename,
                        status="duplicate" if duplicate else "processed",
                        duplicate=duplicate,
                        record=stored,
                        csv_content=csv_bytes([to_row(stored)]).decode("utf-8") if mode == "separate" else None,
                    )
                )
            except HTTPException as exc:
                failed += 1
                results.append(BatchFileResult(filename=filename, status="error", detail=str(exc.detail)))
            except (OSError, Timeout, ValueError) as exc:
                failed += 1
                logger.error("Batch invoice processing failed: %s", type(exc).__name__)
                results.append(
                    BatchFileResult(
                        filename=filename,
                        status="error",
                        detail="Não foi possível aceder ao CSV.",
                    )
                )
            finally:
                upload.file.close()
        return BatchUploadResult(
            mode=mode,
            total=len(files),
            processed=processed,
            duplicates=duplicates,
            failed=failed,
            results=results,
        )

    @api.get("/v1/invoices.csv", dependencies=[Depends(require_access)], tags=["Faturas"])
    def download_csv(user_id: str | None = Depends(require_access)):
        """Descarrega todas as faturas guardadas com os campos comerciais habituais."""
        try:
            data = store.export(user_id)
        except (OSError, Timeout) as exc:
            raise HTTPException(status_code=503, detail="Não foi possível aceder ao CSV.") from exc
        return Response(
            data,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="faturas.csv"',
                "Cache-Control": "no-store",
            },
        )

    return api


app = create_app()
