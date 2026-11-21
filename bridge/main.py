import asyncio
import hashlib
import secrets
import sqlite3
from contextlib import asynccontextmanager, suppress
from decimal import Decimal
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from stellar_sdk import StrKey

if __package__:
    from .config import Settings, to_ngn, to_stroops
    from .db import Database
    from .fincra import FincraClient
    from .stellar_client import StellarClient
    from .worker import Worker
else:  # `cd bridge && uvicorn main:app --reload`
    from config import Settings, to_ngn, to_stroops
    from db import Database
    from fincra import FincraClient
    from stellar_client import StellarClient
    from worker import Worker


class VaultInput(BaseModel):
    contract_id: str
    sponsor_address: str
    elder_address: str
    start_ledger: int | None = Field(default=None, gt=0)

    @field_validator("contract_id")
    @classmethod
    def contract(cls, value):
        if not StrKey.is_valid_contract(value):
            raise ValueError("Invalid Stellar contract address")
        return value

    @field_validator("sponsor_address", "elder_address")
    @classmethod
    def account(cls, value):
        if not (StrKey.is_valid_ed25519_public_key(value) or StrKey.is_valid_contract(value)):
            raise ValueError("Invalid Stellar address")
        return value


class SpendInput(BaseModel):
    contract_id: str
    amount_ngn: Decimal = Field(gt=0, max_digits=16, decimal_places=2)
    bank_code: str = Field(pattern=r"^[0-9]{3,6}$")
    account_number: str = Field(pattern=r"^[0-9]{10}$")
    account_name: str = Field(min_length=3, max_length=150)

    @field_validator("account_name")
    @classmethod
    def name(cls, value):
        value = " ".join(value.split())
        if len(value.split()) < 2:
            raise ValueError("Provide the beneficiary's first and last name")
        return value


class SubmissionInput(BaseModel):
    stellar_tx_hash: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


def create_app(settings=None, stellar=None, fincra=None):
    settings = settings or Settings()
    db = Database(settings.database_path)

    @asynccontextmanager
    async def lifespan(app):
        await db.open()
        app.state.stellar = stellar or StellarClient(settings)
        app.state.fincra = fincra or FincraClient(settings)
        app.state.db = db
        app.state.worker = Worker(db, app.state.stellar, app.state.fincra, settings)
        task = asyncio.create_task(app.state.worker.run()) if settings.poller_enabled else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await app.state.stellar.close()
            await app.state.fincra.close()
            await db.close()

    app = FastAPI(title="Yaro Stellar Bridge", version="0.1.0", lifespan=lifespan)

    async def authorize(x_api_key: Annotated[str | None, Header()] = None):
        key = settings.bridge_api_key.get_secret_value()
        if not key:
            raise HTTPException(503, "Configure BRIDGE_API_KEY before using the bridge")
        if not x_api_key or not secrets.compare_digest(x_api_key.encode(), key.encode()):
            raise HTTPException(401, "Invalid bridge API key")

    private = [Depends(authorize)]

    async def vault_or_404(contract_id):
        row = await db.one("SELECT * FROM vaults WHERE contract_id=?", (contract_id,))
        if row is None:
            raise HTTPException(404, "Vault not registered")
        return row

    @app.post("/vaults", dependencies=private, status_code=201)
    async def register_vault(body: VaultInput):
        existing = await db.one("SELECT * FROM vaults WHERE contract_id=?", (body.contract_id,))
        if existing:
            if (existing["sponsor_address"], existing["elder_address"]) != (
                body.sponsor_address,
                body.elder_address,
            ):
                raise HTTPException(409, "Vault already registered with different addresses")
            return existing
        try:
            await app.state.stellar.validate_vault(
                body.contract_id, body.sponsor_address, body.elder_address
            )
            health = await app.state.stellar.health()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, "Unable to verify vault with Stellar RPC") from exc
        start = body.start_ledger or health["latest_ledger"]
        if not health["oldest_ledger"] <= start <= health["latest_ledger"]:
            raise HTTPException(422, "start_ledger is outside RPC retained history")
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO vaults(contract_id,sponsor_address,elder_address,last_ledger) VALUES(?,?,?,?)",
                (body.contract_id, body.sponsor_address, body.elder_address, start - 1),
            )
            await conn.execute(
                "UPDATE rpc_cursor SET last_ledger=(SELECT MIN(last_ledger) FROM vaults) WHERE id=1"
            )
        return await vault_or_404(body.contract_id)

    @app.get("/vaults/{contract_id}", dependencies=private)
    async def get_vault(contract_id: str):
        vault = await vault_or_404(contract_id)
        # Account source is used for simulation only. Contract-address sponsors need
        # a valid G-address simulation source, provided by the SDK's null account.
        source = vault["sponsor_address"]
        if not StrKey.is_valid_ed25519_public_key(source):
            source = StrKey.encode_ed25519_public_key(bytes(32))
        try:
            status = await app.state.stellar.vault_status(contract_id, source)
        except Exception as exc:
            raise HTTPException(502, "Unable to read vault from Stellar RPC") from exc
        return {
            **vault,
            **status,
            "balance_ngn": to_ngn(int(status["balance_usdc_stroops"]), settings.usdc_ngn_rate),
            "fx_rate": str(settings.usdc_ngn_rate),
        }

    @app.post("/spend-intent", dependencies=private, status_code=201)
    async def spend_intent(body: SpendInput):
        await vault_or_404(body.contract_id)
        try:
            amount = to_stroops(body.amount_ngn, settings.usdc_ngn_rate)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        beneficiary_hash = hashlib.sha256(
            f"{body.bank_code}:{body.account_number}".encode()
        ).hexdigest()
        record_id = str(uuid4())
        await db.execute(
            """INSERT INTO spend_records
            (id,contract_id,bank_code,account_number,account_name,beneficiary_hash,amount_usdc_stroops,amount_ngn,fx_rate)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                record_id,
                body.contract_id,
                body.bank_code,
                body.account_number,
                body.account_name,
                beneficiary_hash,
                str(amount),
                str(body.amount_ngn),
                str(settings.usdc_ngn_rate),
            ),
        )
        return {
            "id": record_id,
            "contract_id": body.contract_id,
            "beneficiary_hash": beneficiary_hash,
            "amount_usdc_stroops": str(amount),
            "amount_ngn": str(body.amount_ngn),
            "fx_rate": str(settings.usdc_ngn_rate),
            "status": "PENDING_ONCHAIN",
        }

    @app.post("/spend-intents/{record_id}/submission", dependencies=private)
    async def link_submission(record_id: str, body: SubmissionInput):
        tx_hash = body.stellar_tx_hash.lower()
        try:
            async with db.transaction() as conn:
                cursor = await conn.execute("SELECT * FROM spend_records WHERE id=?", (record_id,))
                row = await cursor.fetchone()
                if row is None:
                    raise HTTPException(404, "Spend intent not found")
                if row["stellar_tx_hash"] and row["stellar_tx_hash"] != tx_hash:
                    raise HTTPException(409, "Submission already linked")
                await conn.execute(
                    "UPDATE spend_records SET stellar_tx_hash=? WHERE id=?", (tx_hash, record_id)
                )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Transaction already belongs to another intent") from exc
        return {"id": record_id, "stellar_tx_hash": tx_hash}

    @app.get("/spend-intents/{record_id}", dependencies=private)
    async def get_intent(record_id: str):
        row = await db.one("SELECT * FROM spend_records WHERE id=?", (record_id,))
        if row is None:
            raise HTTPException(404, "Spend intent not found")
        return row

    @app.get("/vaults/{contract_id}/paused", dependencies=private)
    async def paused(contract_id: str):
        await vault_or_404(contract_id)
        return await db.all(
            "SELECT * FROM spend_records WHERE contract_id=? AND status='PAUSED' ORDER BY created_at,id",
            (contract_id,),
        )

    @app.get("/health")
    async def health():
        cursor = await db.one("SELECT last_ledger FROM rpc_cursor WHERE id=1")
        try:
            rpc = await app.state.stellar.health()
        except Exception:
            rpc = {"status": "unavailable"}
        errors = await db.one("SELECT COUNT(*) AS count FROM events WHERE processed=-1")
        worker_error = app.state.worker.last_error
        healthy = rpc["status"] == "healthy" and not worker_error and not errors["count"]
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={
                "status": "ok" if healthy else "degraded",
                "rpc": rpc,
                "last_ledger": cursor["last_ledger"],
                "poller_enabled": settings.poller_enabled,
                "poller_error": worker_error,
                "rejected_events": errors["count"],
                "fincra_configured": app.state.fincra.configured,
            },
        )

    return app


app = create_app()
