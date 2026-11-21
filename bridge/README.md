# Yaro Stellar bridge

Async FastAPI + SQLite implementation of PRD Part B. It reads Stellar Testnet and initiates NGN payouts from a **funded Fincra sandbox NGN wallet**. It does not convert USDC at Fincra, hold wallet keys, sign Stellar transactions, or submit them. The configured FX rate is a demo quote, not a live exchange rate.

## Run

From the repository root (Python 3.11+):

```bash
python3 -m venv .venv
.venv/bin/pip install -r bridge/requirements-dev.txt
cd bridge
cp .env.example .env
# Generate a token, then put it in BRIDGE_API_KEY in .env:
../.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
# Configure FINCRA_SECRET_KEY and FINCRA_BUSINESS_ID for payouts.
../.venv/bin/uvicorn main:app --reload
```

Use one application worker per SQLite database. Interactive API docs: `http://127.0.0.1:8000/docs`. All business endpoints require `X-API-Key`; `/health` is public. An unset key disables business endpoints. Keep this key on your trusted application backend; do not embed it in browser JavaScript. The upstream application remains responsible for authenticating users and authorizing access to their vaults. Registration is an administrative action: register only deployments of the reviewed YaroVault WASM. The bridge checks instance configuration, not deployed bytecode identity.

Without Fincra credentials the application still serves intents and ingests chain events, leaving executed spends queued. It never reports a fake successful payout. The `.env` sample includes the additional business ID required by Fincra's payout API. Testnet and Fincra sandbox are enforced.

## Frontend integration sequence

1. Deploy YaroVault, then `POST /vaults` with `contract_id`, `sponsor_address`, and `elder_address`. Optionally supply `start_ledger` for retained historical events. By default ingestion starts at the current ledger. Registration validates the RPC network and on-chain sponsor, elder, and USDC token. Register before the first spend.
2. `POST /spend-intent` with `contract_id`, `amount_ngn` (decimal string), `bank_code`, `account_number`, and `account_name` (first and last name). Preserve leading zeroes in both bank identifiers. The response includes `id`, a hex SHA-256 beneficiary hash, a base-unit amount string, and the frozen quote.
3. The elder's wallet simulates, signs, and submits `request_spend(amount, beneficiary_hash)` using that response. Convert the amount to BigInt and the hex hash to 32 bytes. Use **one spend request per Stellar transaction**.
4. `POST /spend-intents/{id}/submission` with `stellar_tx_hash`. Repeating the same link is safe; replacing it or linking one transaction to two intents returns 409. This is a correlation hint, not proof of execution. Only observed contract events can authorize settlement. If the transaction failed, create a new intent for a new attempt.
5. Poll `GET /spend-intents/{id}`. Events received before step 4 are retained and matched on a later poll. Match requires the registered contract, submitted transaction hash, USDC amount, and beneficiary hash. Subsequent resolution events match `(contract_id, onchain_tx_id)`.
6. `GET /vaults/{contract_id}/paused` supplies pending on-chain transaction IDs. The sponsor signs `resolve_paused(tx_id, approve)` in their wallet. The bridge consumes the resolution event; no server-side signing endpoint exists.

`GET /vaults/{contract_id}` returns the simulated balance, its NGN equivalent, and the current daily allowance. All USDC base-unit values and on-chain IDs are strings to avoid JavaScript integer precision loss. NGN has at most two decimal places; conversion rounds USDC **up** to the nearest base unit. Each intent stores its original FX rate.

## State and recovery

- States: `PENDING_ONCHAIN` → `PAUSED` → `EXECUTED` or `DECLINED`; safe spends go directly to `EXECUTED`. Fincra confirmation advances `EXECUTED` to `PAYOUT_COMPLETED` or `PAYOUT_FAILED`.
- A successful HTTP POST can mean only `processing`. The poller queries Fincra by the deterministic `YARO-STELLAR-{record_uuid}` reference until a terminal provider status arrives.
- The database commits a payout claim **before** POST. After timeout or crash, it only queries that reference; it never resends an ambiguous payment. If Fincra keeps returning 404, `last_error` requests reconciliation. An operator must verify the provider ledger before any manual recovery. This intentionally favors avoiding duplicate payments over automatic retries after ambiguous failures.
- A fourth `events` table is necessary for durable, replayable event deduplication. Events are keyed by RPC event ID, not transaction hash: approval and pause events can share a transaction. Each vault has a separate ingestion watermark; `rpc_cursor.last_ledger` is their minimum. Full pages are consumed before advancing a vault watermark.
- A crash mid-page is safe: committed events replay with `INSERT OR IGNORE`. Unlinked events remain pending. Payload mismatches are quarantined (`processed=-1`) and degrade health without initiating payouts.
- If the saved watermark falls outside RPC retention, ingestion reports degraded health instead of skipping history. Restore missing history from an archival source before manually advancing it. Back up SQLite with its WAL consistently; do not discard records to restart a funded integration.
- Optional sponsor notifications retry until acknowledged. Receivers must deduplicate the `Idempotency-Key` header. `X-Yaro-Signature`, when configured, is hex HMAC-SHA256 over the exact request body. Notifications contain no bank details.
- Bank account data remains in SQLite for payouts. Restrict filesystem access and keep the database out of Git. No sponsor or elder secret keys are accepted or stored.

## Verify

```bash
.venv/bin/python -m pytest bridge/tests -q
```

Tests use SQLite, real Stellar XDR encoding, and fake external services; they do not initiate payments. Live end-to-end verification still requires a deployed vault, funded Testnet identities/USDC, and Fincra sandbox credentials.

API references used: [Stellar event pagination](https://developers.stellar.org/docs/data/apis/rpc/api-reference/methods/getEvents), [Python Stellar SDK](https://stellar-sdk.readthedocs.io/en/stable/api.html), [Fincra NGN bank transfers](https://docs.fincra.com/docs/making-bank-account-transfers), and [payout lookup by customer reference](https://docs.fincra.com/di/reference/fetch-payout-by-customer-reference).
