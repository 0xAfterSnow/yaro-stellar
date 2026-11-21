import hashlib
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from bridge.config import Settings, to_stroops
from bridge.db import Database
from bridge.fincra import FincraClient
from bridge.main import create_app
from bridge.stellar_client import ChainEvent, StellarClient, decode_event
from bridge.worker import Worker
from fastapi.testclient import TestClient
from stellar_sdk import StrKey, scval

CONTRACT = StrKey.encode_contract(bytes([1]) * 32)
SPONSOR = StrKey.encode_ed25519_public_key(bytes([2]) * 32)
ELDER = StrKey.encode_ed25519_public_key(bytes([3]) * 32)
HASH = "a" * 64
PAYEE = hashlib.sha256(b"044:0123456789").hexdigest()


class FakeStellar:
    def __init__(self):
        self.chain = []
        self.latest = 100
        self.oldest = 1
        self.fail = False

    async def close(self):
        pass

    async def health(self):
        if self.fail:
            raise ConnectionError()
        return {"status": "healthy", "latest_ledger": self.latest, "oldest_ledger": self.oldest}

    async def validate_vault(self, *args):
        pass

    async def vault_status(self, *args):
        return {"balance_usdc_stroops": "10000000", "spent_today": "0", "daily_limit": "10000000"}

    async def events(self, contract, start, end):
        for event in self.chain:
            if event.contract_id == contract and start <= event.ledger < end:
                yield event


class FakeFincra:
    configured = True

    def __init__(self):
        self.posts = 0
        self.gets = 0
        self.timeout = False
        self.result = {"status": "processing", "reference": "F-123"}

    async def close(self):
        pass

    async def trigger_sandbox_payout(self, record):
        self.posts += 1
        if self.timeout:
            raise TimeoutError()
        return self.result

    async def lookup(self, record):
        self.gets += 1
        return self.result


def settings(tmp_path, **kwargs):
    return Settings(
        _env_file=None,
        database_path=str(tmp_path / "test.sqlite3"),
        bridge_api_key="test-secret",
        poller_enabled=False,
        **kwargs,
    )


def event(
    kind="spend_executed", id="001", ledger=100, tx_hash=HASH, escalated=False, amount="10000000"
):
    data = {
        "tx_id": "1",
        "amount": amount,
        "beneficiary_hash": PAYEE,
        "was_escalated": escalated,
        "reason": "NEW_PAYEE",
        "sponsor": SPONSOR,
    }
    return ChainEvent(id, CONTRACT, ledger, tx_hash, kind, data)


async def setup_worker(tmp_path):
    conf = settings(tmp_path)
    db = await Database(conf.database_path).open()
    await db.execute(
        "INSERT INTO vaults(contract_id,sponsor_address,elder_address,last_ledger) VALUES(?,?,?,?)",
        (CONTRACT, SPONSOR, ELDER, 99),
    )
    await db.execute(
        """INSERT INTO spend_records
        (id,contract_id,stellar_tx_hash,bank_code,account_number,account_name,beneficiary_hash,amount_usdc_stroops,amount_ngn,fx_rate)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            "record-1",
            CONTRACT,
            HASH,
            "044",
            "0123456789",
            "Ada Okafor",
            PAYEE,
            "10000000",
            "1500",
            "1500",
        ),
    )
    chain, fincra = FakeStellar(), FakeFincra()
    return db, chain, fincra, Worker(db, chain, fincra, conf)


@pytest.mark.asyncio
async def test_execution_replay_restart_and_settlement(tmp_path):
    db, chain, fincra, worker = await setup_worker(tmp_path)
    try:
        chain.chain = [event()]
        await worker.tick()
        row = await db.one("SELECT * FROM spend_records")
        assert row["status"] == "EXECUTED"  # accepted != paid
        assert fincra.posts == 1
        await db.execute("UPDATE vaults SET last_ledger=99")
        await worker.tick()
        assert fincra.posts == 1
        assert (await db.one("SELECT COUNT(*) AS n FROM events"))["n"] == 1
        await db.close()
        await db.open()
        fincra.result["status"] = "successful"
        restarted = Worker(db, chain, fincra, worker.settings)
        await restarted.tick()
        assert (await db.one("SELECT * FROM spend_records"))["status"] == "PAYOUT_COMPLETED"
        await restarted.tick()
        assert fincra.posts == 1
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
async def test_paused_resolution_and_same_transaction_events(tmp_path, approve):
    db, chain, fincra, worker = await setup_worker(tmp_path)
    try:
        chain.chain = [event("spend_paused")]
        await worker.tick()
        assert (await db.one("SELECT * FROM spend_records"))["status"] == "PAUSED"
        assert fincra.posts == 0
        chain.latest = 101
        chain.chain.append(
            event("spend_executed" if approve else "spend_declined", "002", 101, escalated=True)
        )
        await worker.tick()
        assert (await db.one("SELECT * FROM spend_records"))["status"] == (
            "EXECUTED" if approve else "DECLINED"
        )
        assert fincra.posts == int(approve)
        assert (await db.one("SELECT COUNT(*) AS n FROM events"))["n"] == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_late_binding_and_mismatched_intent_never_pays(tmp_path):
    db, chain, fincra, worker = await setup_worker(tmp_path)
    try:
        await db.execute("UPDATE spend_records SET stellar_tx_hash=NULL")
        chain.chain = [event(amount="999")]
        await worker.tick()
        assert (await db.one("SELECT * FROM events"))["processed"] == 0
        await db.execute("UPDATE spend_records SET stellar_tx_hash=?", (HASH,))
        await worker.tick()
        assert (await db.one("SELECT * FROM events"))["processed"] == -1
        assert fincra.posts == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_timeout_is_reconciled_without_second_post_even_during_rpc_outage(tmp_path):
    db, chain, fincra, worker = await setup_worker(tmp_path)
    try:
        chain.chain = [event()]
        fincra.timeout = True
        await worker.tick()
        fincra.result = None
        chain.fail = True
        await worker.tick()
        assert fincra.posts == 1 and fincra.gets == 1
        assert (await db.one("SELECT * FROM spend_records"))["status"] == "EXECUTED"
        fincra.result = {"status": "successful", "reference": "F-123"}
        await worker.tick()
        assert fincra.posts == 1
        assert (await db.one("SELECT * FROM spend_records"))["status"] == "PAYOUT_COMPLETED"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_history_gap_does_not_skip_ledgers(tmp_path):
    db, chain, fincra, worker = await setup_worker(tmp_path)
    try:
        chain.oldest = 101
        chain.latest = 200
        await worker.tick()
        assert worker.last_error
        assert (await db.one("SELECT * FROM vaults"))["last_ledger"] == 99
    finally:
        await db.close()


def test_api_flow_validation_auth_and_decimal_conversion(tmp_path):
    chain, fincra = FakeStellar(), FakeFincra()
    app = create_app(settings(tmp_path), chain, fincra)
    headers = {"X-API-Key": "test-secret"}
    body = {"contract_id": CONTRACT, "sponsor_address": SPONSOR, "elder_address": ELDER}
    with TestClient(app) as client:
        assert client.post("/vaults", json=body).status_code == 401
        assert client.post("/vaults", json=body, headers=headers).status_code == 201
        assert (
            client.post(
                "/vaults", json={**body, "elder_address": SPONSOR}, headers=headers
            ).status_code
            == 409
        )
        response = client.get(f"/vaults/{CONTRACT}", headers=headers)
        assert response.json()["balance_ngn"] == "1500.00"
        spend = {
            "contract_id": CONTRACT,
            "bank_code": "044",
            "account_number": "0123456789",
            "account_name": "Ada Okafor",
            "amount_ngn": "0.01",
        }
        response = client.post("/spend-intent", json=spend, headers=headers)
        assert response.status_code == 201
        record = response.json()
        assert record["amount_usdc_stroops"] == "67"
        assert record["beneficiary_hash"] == PAYEE
        link = f"/spend-intents/{record['id']}/submission"
        assert client.post(link, json={"stellar_tx_hash": HASH}, headers=headers).status_code == 200
        assert (
            client.post(link, json={"stellar_tx_hash": "b" * 64}, headers=headers).status_code
            == 409
        )
        second = client.post("/spend-intent", json=spend, headers=headers).json()
        assert (
            client.post(
                f"/spend-intents/{second['id']}/submission",
                json={"stellar_tx_hash": HASH},
                headers=headers,
            ).status_code
            == 409
        )
        for value in ["0", "-1", "NaN", "1.001"]:
            assert (
                client.post(
                    "/spend-intent", json={**spend, "amount_ngn": value}, headers=headers
                ).status_code
                == 422
            )
        assert client.get(f"/vaults/{CONTRACT}/paused", headers=headers).json() == []
        assert client.get("/health").status_code == 200
        chain.fail = True
        assert client.get("/health").status_code == 503


def raw_event(id="e1", ledger=100):
    return SimpleNamespace(
        topic=[scval.to_symbol("spend_executed").to_xdr()],
        value=scval.to_struct(
            {
                "tx_id": scval.to_uint64(1),
                "amount": scval.to_int128(10_000_000),
                "beneficiary_hash": scval.to_bytes(bytes.fromhex(PAYEE)),
                "was_escalated": scval.to_bool(False),
            }
        ).to_xdr(),
        id=id,
        ledger=ledger,
        contract_id=CONTRACT,
        transaction_hash=HASH,
    )


def test_decode_real_xdr():
    decoded = decode_event(raw_event())
    assert decoded.payload == {
        "tx_id": "1",
        "amount": "10000000",
        "beneficiary_hash": PAYEE,
        "was_escalated": False,
    }
    assert to_stroops(Decimal("0.01"), Decimal(1500)) == 67


@pytest.mark.asyncio
async def test_sdk_pagination_never_combines_cursor_and_ledger_bounds(tmp_path):
    server = SimpleNamespace(
        get_events=AsyncMock(
            side_effect=[
                SimpleNamespace(events=[raw_event()], cursor="cursor1"),
                SimpleNamespace(events=[raw_event("e2")], cursor="cursor2"),
                SimpleNamespace(events=[raw_event("future", 101)], cursor="cursor3"),
            ]
        )
    )
    client = StellarClient(settings(tmp_path), server)
    events = [event async for event in client.events(CONTRACT, 100, 101)]
    assert [e.id for e in events] == ["e1", "e2"]
    calls = server.get_events.call_args_list
    assert calls[0].kwargs["start_ledger"] == 100
    assert calls[1].kwargs["start_ledger"] is None
    assert calls[1].kwargs["end_ledger"] is None
    assert calls[1].kwargs["cursor"] == "cursor1"


@pytest.mark.asyncio
async def test_fincra_request_and_status_parsing(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "customerReference": "YARO-STELLAR-r1",
                    "reference": "F-1",
                    "status": "processing",
                }
            },
        )

    client = FincraClient(
        settings(tmp_path, fincra_secret_key="secret", fincra_business_id="biz"),
        httpx.MockTransport(handler),
    )
    record = {
        "id": "r1",
        "amount_ngn": "1500.01",
        "account_name": "Ada Okafor",
        "account_number": "0123456789",
        "bank_code": "044",
    }
    try:
        assert (await client.trigger_sandbox_payout(record))["status"] == "processing"
        await client.lookup(record)
        payload = json.loads(calls[0].content)
        assert payload["customerReference"] == "YARO-STELLAR-r1"
        assert payload["beneficiary"]["accountNumber"] == "0123456789"
        assert calls[0].headers["api-key"] == "secret"
        assert calls[1].url.path.endswith("/customer-reference/YARO-STELLAR-r1")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_vault_registration_decodes_contract_instance(tmp_path):
    from stellar_sdk import Address, xdr

    conf = settings(tmp_path)
    entries = [
        xdr.SCMapEntry(scval.to_vec([scval.to_symbol(key)]), scval.to_address(value))
        for key, value in [
            ("Sponsor", SPONSOR),
            ("Elder", ELDER),
            ("UsdcToken", conf.stellar_usdc_contract_id),
        ]
    ]
    instance = xdr.SCContractInstance(
        xdr.ContractExecutable(
            xdr.ContractExecutableType.CONTRACT_EXECUTABLE_WASM, xdr.Hash(bytes(32))
        ),
        xdr.SCMap(entries),
    )
    entry = xdr.LedgerEntryData(
        xdr.LedgerEntryType.CONTRACT_DATA,
        contract_data=xdr.ContractDataEntry(
            xdr.ExtensionPoint(0),
            Address(CONTRACT).to_xdr_sc_address(),
            xdr.SCVal(xdr.SCValType.SCV_LEDGER_KEY_CONTRACT_INSTANCE),
            xdr.ContractDataDurability.PERSISTENT,
            xdr.SCVal(xdr.SCValType.SCV_CONTRACT_INSTANCE, instance=instance),
        ),
    )
    server = SimpleNamespace(
        get_network=AsyncMock(
            return_value=SimpleNamespace(passphrase=conf.stellar_network_passphrase)
        ),
        get_contract_data=AsyncMock(return_value=SimpleNamespace(xdr=entry.to_xdr())),
    )
    client = StellarClient(conf, server)
    await client.validate_vault(CONTRACT, SPONSOR, ELDER)
    with pytest.raises(ValueError, match="Sponsor"):
        await client.validate_vault(CONTRACT, ELDER, ELDER)
    server.get_network.return_value.passphrase = "Public Global Stellar Network ; September 2015"
    with pytest.raises(ValueError, match="network"):
        await client.validate_vault(CONTRACT, SPONSOR, ELDER)


@pytest.mark.asyncio
async def test_read_simulates_without_signing(tmp_path):
    server = SimpleNamespace(
        simulate_transaction=AsyncMock(
            side_effect=[
                SimpleNamespace(
                    error=None, results=[SimpleNamespace(xdr=scval.to_int128(100).to_xdr())]
                ),
                SimpleNamespace(
                    error=None,
                    results=[
                        SimpleNamespace(
                            xdr=scval.to_vec([scval.to_int128(20), scval.to_int128(90)]).to_xdr()
                        )
                    ],
                ),
            ]
        )
    )
    client = StellarClient(settings(tmp_path), server)
    assert await client.vault_status(CONTRACT, SPONSOR) == {
        "balance_usdc_stroops": "100",
        "spent_today": "20",
        "daily_limit": "90",
    }
    for call in server.simulate_transaction.call_args_list:
        assert call.args[0].signatures == []


@pytest.mark.asyncio
async def test_partial_ingestion_replays_and_late_link_executes(tmp_path):
    db, chain, fincra, worker = await setup_worker(tmp_path)
    try:
        await db.execute("UPDATE spend_records SET stellar_tx_hash=NULL")

        async def broken_pages(*args):
            yield event()
            raise ConnectionError("second page failed")

        chain.events = broken_pages
        await worker.tick()
        assert (await db.one("SELECT * FROM vaults"))["last_ledger"] == 99
        assert (await db.one("SELECT * FROM events"))["processed"] == 0
        assert fincra.posts == 0
        del chain.events
        chain.chain = [event()]
        await db.execute("UPDATE spend_records SET stellar_tx_hash=?", (HASH,))
        await worker.tick()
        assert (await db.one("SELECT * FROM vaults"))["last_ledger"] == 100
        assert (await db.one("SELECT COUNT(*) AS n FROM events"))["n"] == 1
        assert fincra.posts == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unconfigured_payout_waits_then_provider_failure_is_terminal(tmp_path):
    db, chain, fincra, worker = await setup_worker(tmp_path)
    try:
        chain.chain = [event()]
        fincra.configured = False
        await worker.tick()
        assert (await db.one("SELECT * FROM spend_records"))["payout_requested"] == 0
        fincra.configured = True
        fincra.result["status"] = "failed"
        await worker.tick()
        await worker.tick()
        assert (await db.one("SELECT * FROM spend_records"))["status"] == "PAYOUT_FAILED"
        assert fincra.posts == 1
    finally:
        await db.close()
