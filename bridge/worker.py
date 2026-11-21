import asyncio
import hashlib
import hmac
import json
import logging

import httpx

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, db, stellar, fincra, settings):
        self.db, self.stellar, self.fincra, self.settings = db, stellar, fincra, settings
        self.last_error = None

    async def ingest(self):
        health = await self.stellar.health()
        if health["status"] != "healthy":
            raise RuntimeError("RPC unhealthy")
        for vault in await self.db.all("SELECT * FROM vaults"):
            start = vault["last_ledger"] + 1
            if start < health["oldest_ledger"]:
                raise RuntimeError(
                    "RPC retention gap: restore missing history before advancing cursor"
                )
            end = min(health["latest_ledger"] + 1, start + 10_000)
            if start >= end:
                continue
            async for event in self.stellar.events(vault["contract_id"], start, end):
                if event.contract_id != vault["contract_id"] or not start <= event.ledger < end:
                    raise ValueError("RPC returned an event outside the requested range")
                await self.db.execute(
                    "INSERT OR IGNORE INTO events(id,contract_id,ledger,tx_hash,kind,payload) VALUES(?,?,?,?,?,?)",
                    (
                        event.id,
                        event.contract_id,
                        event.ledger,
                        event.tx_hash,
                        event.kind,
                        json.dumps(event.payload),
                    ),
                )
            # A failed page never advances this vault. Already saved events are deduplicated on replay.
            async with self.db.transaction() as conn:
                await conn.execute(
                    "UPDATE vaults SET last_ledger=MAX(last_ledger,?) WHERE contract_id=?",
                    (end - 1, vault["contract_id"]),
                )
                await conn.execute(
                    "UPDATE rpc_cursor SET last_ledger=(SELECT MIN(last_ledger) FROM vaults) WHERE id=1"
                )

    async def process_events(self):
        for event in await self.db.all("SELECT * FROM events WHERE processed=0 ORDER BY ledger,id"):
            data = json.loads(event["payload"])
            async with self.db.transaction() as conn:
                current = await conn.execute(
                    "SELECT processed FROM events WHERE id=?", (event["id"],)
                )
                if (await current.fetchone())[0] != 0:
                    continue
                cursor = await conn.execute(
                    "SELECT * FROM spend_records WHERE contract_id=? AND onchain_tx_id=?",
                    (event["contract_id"], data["tx_id"]),
                )
                row = await cursor.fetchone()
                if row is None and (
                    event["kind"] == "spend_paused"
                    or (event["kind"] == "spend_executed" and not data["was_escalated"])
                ):
                    cursor = await conn.execute(
                        "SELECT * FROM spend_records WHERE contract_id=? AND stellar_tx_hash=? AND onchain_tx_id IS NULL",
                        (event["contract_id"], event["tx_hash"]),
                    )
                    row = await cursor.fetchone()
                if row is None:
                    continue  # Retained durably until the frontend links the submission.
                record = dict(row)
                error = None
                if event["kind"] != "spend_declined":
                    if (
                        data["amount"] != record["amount_usdc_stroops"]
                        or data["beneficiary_hash"] != record["beneficiary_hash"]
                    ):
                        error = "On-chain spend does not match saved intent"
                if event["kind"] == "spend_declined":
                    cursor = await conn.execute(
                        "SELECT sponsor_address FROM vaults WHERE contract_id=?",
                        (event["contract_id"],),
                    )
                    sponsor = (await cursor.fetchone())[0]
                    if data["sponsor"] != sponsor:
                        error = "Decline sponsor mismatch"
                expected = "PENDING_ONCHAIN"
                target = "PAUSED"
                if event["kind"] == "spend_executed":
                    expected = "PAUSED" if data["was_escalated"] else "PENDING_ONCHAIN"
                    target = "EXECUTED"
                elif event["kind"] == "spend_declined":
                    expected, target = "PAUSED", "DECLINED"
                if record["status"] != expected:
                    error = "Unexpected spend state transition"
                if error:
                    await conn.execute(
                        "UPDATE events SET processed=-1,error=? WHERE id=?", (error, event["id"])
                    )
                    continue
                await conn.execute(
                    "UPDATE spend_records SET onchain_tx_id=?,status=? WHERE id=?",
                    (data["tx_id"], target, record["id"]),
                )
                await conn.execute("UPDATE events SET processed=1 WHERE id=?", (event["id"],))

    async def payouts(self):
        if not self.fincra.configured:
            return
        for record in await self.db.all("SELECT * FROM spend_records WHERE status='EXECUTED'"):
            try:
                if record["payout_requested"]:
                    result = await self.fincra.lookup(record)
                else:
                    # Commit BEFORE the external side effect. Following a crash or timeout,
                    # reconcile using GET only; never blindly resubmit an ambiguous payment.
                    async with self.db.transaction() as conn:
                        claimed = await conn.execute(
                            "UPDATE spend_records SET payout_requested=1 WHERE id=? AND payout_requested=0 AND status='EXECUTED'",
                            (record["id"],),
                        )
                        if claimed.rowcount != 1:
                            continue
                    result = await self.fincra.trigger_sandbox_payout(record)
                if result is None:
                    await self.db.execute(
                        "UPDATE spend_records SET last_error=? WHERE id=?",
                        (
                            "Payout not found; reconciliation required; POST will not be repeated",
                            record["id"],
                        ),
                    )
                    continue
                status = {"successful": "PAYOUT_COMPLETED", "failed": "PAYOUT_FAILED"}.get(
                    result["status"], "EXECUTED"
                )
                await self.db.execute(
                    "UPDATE spend_records SET status=?,fincra_reference=?,last_error=NULL WHERE id=?",
                    (status, result["reference"], record["id"]),
                )
            except Exception as exc:
                # Do not log provider response bodies or bank details.
                await self.db.execute(
                    "UPDATE spend_records SET last_error=? WHERE id=?",
                    (f"Payout reconciliation required ({type(exc).__name__})", record["id"]),
                )
                log.warning("Payout reconciliation required for record %s", record["id"])

    async def notifications(self):
        if not self.settings.yaro_webhook_url:
            return
        async with httpx.AsyncClient(timeout=10) as http:
            for row in await self.db.all(
                "SELECT * FROM spend_records WHERE status='PAUSED' AND notification_sent=0"
            ):
                body = json.dumps(
                    {
                        "event": "spend_paused",
                        "record_id": row["id"],
                        "contract_id": row["contract_id"],
                        "tx_id": row["onchain_tx_id"],
                        "amount_ngn": row["amount_ngn"],
                    },
                    sort_keys=True,
                ).encode()
                headers = {
                    "Content-Type": "application/json",
                    "Idempotency-Key": f"paused-{row['id']}",
                }
                secret = self.settings.yaro_webhook_secret.get_secret_value()
                if secret:
                    headers["X-Yaro-Signature"] = hmac.new(
                        secret.encode(), body, hashlib.sha256
                    ).hexdigest()
                try:
                    result = await http.post(
                        self.settings.yaro_webhook_url, content=body, headers=headers
                    )
                    result.raise_for_status()
                    await self.db.execute(
                        "UPDATE spend_records SET notification_sent=1 WHERE id=?", (row["id"],)
                    )
                except httpx.HTTPError:
                    log.warning("Notification will retry for record %s", row["id"])

    async def tick(self):
        self.last_error = None
        try:
            await self.ingest()
        except Exception as exc:
            self.last_error = f"Event polling failed ({type(exc).__name__})"
            log.warning(self.last_error)
        # Existing payouts and stored events continue through temporary RPC outages.
        await self.process_events()
        await self.payouts()
        await self.notifications()

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception as exc:
                self.last_error = f"Worker failed ({type(exc).__name__})"
                log.warning(self.last_error)
            await asyncio.sleep(self.settings.poll_interval_seconds)
