from dataclasses import dataclass

from stellar_sdk import Account, SorobanServerAsync, TransactionBuilder, scval, xdr
from stellar_sdk.soroban_rpc import EventFilter


@dataclass(frozen=True)
class ChainEvent:
    id: str
    contract_id: str
    ledger: int
    tx_hash: str
    kind: str
    payload: dict


def decode_event(event):
    if not event.topic:
        return None
    kind = scval.from_symbol(event.topic[0])
    if kind not in {"spend_paused", "spend_executed", "spend_declined"}:
        return None
    fields = scval.from_struct(event.value)
    payload = {"tx_id": str(scval.from_uint64(fields["tx_id"]))}
    if kind != "spend_declined":
        payload["amount"] = str(scval.from_int128(fields["amount"]))
        beneficiary = scval.from_bytes(fields["beneficiary_hash"])
        if len(beneficiary) != 32 or int(payload["amount"]) <= 0:
            raise ValueError("Invalid spend event")
        payload["beneficiary_hash"] = beneficiary.hex()
        if kind == "spend_executed":
            payload["was_escalated"] = scval.from_bool(fields["was_escalated"])
        else:
            payload["reason"] = scval.from_symbol(fields["reason"])
    else:
        payload["sponsor"] = scval.from_address(fields["sponsor"]).address
    return ChainEvent(
        event.id, event.contract_id, event.ledger, event.transaction_hash, kind, payload
    )


class StellarClient:
    def __init__(self, settings, server=None):
        self.settings = settings
        self.server = server or SorobanServerAsync(settings.stellar_rpc_url)

    async def close(self):
        await self.server.close()

    async def health(self):
        result = await self.server.get_health()
        return {
            "status": result.status,
            "latest_ledger": result.latest_ledger,
            "oldest_ledger": result.oldest_ledger,
        }

    async def validate_vault(self, contract_id, sponsor, elder):
        network = await self.server.get_network()
        if network.passphrase != self.settings.stellar_network_passphrase:
            raise ValueError("RPC network does not match configured Testnet")
        entry = await self.server.get_contract_data(
            contract_id, xdr.SCVal(xdr.SCValType.SCV_LEDGER_KEY_CONTRACT_INSTANCE)
        )
        if entry is None:
            raise ValueError("Contract instance not found")
        data = xdr.LedgerEntryData.from_xdr(entry.xdr)
        instance = data.contract_data.val.instance
        if not instance or not instance.storage:
            raise ValueError("Missing vault configuration")
        config = {}
        for item in instance.storage.sc_map:
            key = scval.to_native(item.key)
            if isinstance(key, list) and len(key) == 1:
                config[key[0]] = scval.to_native(item.val)
        expected = {
            "Sponsor": sponsor,
            "Elder": elder,
            "UsdcToken": self.settings.stellar_usdc_contract_id,
        }
        for name, address in expected.items():
            if getattr(config.get(name), "address", None) != address:
                raise ValueError(f"On-chain {name} does not match registration")

    async def read(self, contract_id, sponsor, method):
        # Simulation only: no private key, signature, or transaction submission.
        tx = (
            TransactionBuilder(
                Account(sponsor, 0),
                network_passphrase=self.settings.stellar_network_passphrase,
                base_fee=100,
            )
            .append_invoke_contract_function_op(contract_id, method, [])
            .set_timeout(30)
            .build()
        )
        result = await self.server.simulate_transaction(tx)
        if result.error or not result.results:
            raise ValueError("Vault simulation failed")
        return scval.to_native(result.results[0].xdr)

    async def vault_status(self, contract_id, sponsor):
        balance = await self.read(contract_id, sponsor, "get_balance")
        spent, limit = await self.read(contract_id, sponsor, "get_daily_status")
        return {
            "balance_usdc_stroops": str(balance),
            "spent_today": str(spent),
            "daily_limit": str(limit),
        }

    async def events(self, contract_id, start, end):
        """Yield all pages through end (exclusive), including full-ledger pages."""
        filters = [EventFilter(type="contract", contractIds=[contract_id])]
        cursor = None
        while True:
            page = await self.server.get_events(
                start_ledger=start if cursor is None else None,
                end_ledger=end if cursor is None else None,
                cursor=cursor,
                filters=filters,
                limit=1000,
            )
            for raw in page.events:
                if raw.ledger >= end:
                    return
                event = decode_event(raw)
                if event:
                    yield event
            if not page.events:
                return
            next_cursor = page.cursor
            if not next_cursor or next_cursor == cursor:
                raise ValueError("RPC event pagination did not advance")
            cursor = next_cursor
