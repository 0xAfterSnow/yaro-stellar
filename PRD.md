# Technical PRD: Yaro Stellar Integration (`YaroVault` + Django RPC Bridge)

## 1. Objective
Build the non-custodial Stellar/Soroban layer for **Yaro** and connect it to the existing Django + PostgreSQL + Fincra backend.
- **Public Repo Scope (`yaro-vault`):** Rust/Soroban smart contract (`YaroVault`) + comprehensive unit tests + deployment scripts.
- **Private Backend Scope (Django):** Stellar Testnet RPC listener, FX conversion helper (USDC ↔ NGN), webhook/event handler that triggers existing Web Push notifications on paused spends and Fincra sandbox payouts on executed spends, plus REST endpoints for the frontend.

---

## 2. Network & Toolchain Constants
- **Network:** Stellar Testnet
- **RPC URL:** `https://soroban-testnet.stellar.org`
- **Network Passphrase:** `Test SDF Network ; September 2015`
- **Testnet USDC SAC Address:** `CBIELTK6YBZJU5UP2WWQEUCYKLPU6AUNZ2BQ4WWFEIE3USCIHMXQDAMA` (7 decimals; 1 USDC = `10_000_000` stroops)
- **Soroban SDK:** `soroban-sdk = "29.0.1"` (or latest v28+/v29+ compatible with local `stellar-cli`)
- **Build Command:** `stellar contract build` (targets `wasm32v1-none`; do NOT use `wasm32-unknown-unknown`)

---

## 3. Part A: Soroban Smart Contract Specification (`contracts/yaro_vault`)

### 3.1 Contract Struct & Storage Keys
Use `#[contract]` on a unit struct `pub struct YaroVault;`.

```rust
#[contracttype]
#[derive(Clone)]
pub enum DataKey {
    Sponsor,                     // Address
    Elder,                       // Address
    UsdcToken,                   // Address (SEP-41 Token / SAC)
    OfframpRail,                 // Address (settlement wallet)
    DailyLimit,                  // i128 (in stroops, 7 decimals)
    SpentToday,                  // i128
    LastResetTimestamp,          // u64 (ledger timestamp in seconds)
    SafeList(BytesN<32>),        // bool (SHA-256 hash of bank_code:account_number)
    NextTxId,                    // u64
    PendingTx(u64),              // PendingTransaction struct
}

#[contracttype]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PendingTransaction {
    pub id: u64,
    pub amount: i128,
    pub beneficiary_hash: BytesN<32>,
    pub created_at: u64,
    pub status: TxStatus,
}

#[contracttype]
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TxStatus {
    Pending,
    Approved,
    Declined,
}

```

### 3.2 Error Enum (`#[contracterror]`)

Define explicit contract errors using `#[contracterror]` (do NOT use `#[contracttype]` for panic errors):

* `InvalidAmount = 1` (amount <= 0)
* `InsufficientVaultBalance = 2`
* `TxNotFound = 3`
* `TxAlreadyResolved = 4`
* `Unauthorized = 5`

### 3.3 Contract Events (`#[contractevent]`)

Emit structured events so the Django RPC watcher can react cleanly:

1. `VaultDeposited { sponsor: Address, amount: i128 }`
2. `SpendExecuted { tx_id: u64, amount: i128, beneficiary_hash: BytesN<32>, was_escalated: bool }`
3. `SpendPaused { tx_id: u64, amount: i128, beneficiary_hash: BytesN<32>, reason: Symbol }` (`reason` = `symbol_short!("LIMIT")` or `symbol_short!("NEW_PAYEE")`)
4. `SpendDeclined { tx_id: u64, sponsor: Address }`
5. `RulesUpdated { daily_limit: i128 }`

### 3.4 Required Contract Methods

1. **`__constructor(env: Env, sponsor: Address, elder: Address, usdc_token: Address, offramp_rail: Address, daily_limit: i128)`**
* Validate `daily_limit > 0`.
* Store all config fields in `env.storage().instance()`.
* Initialize `SpentToday = 0`, `LastResetTimestamp = env.ledger().timestamp()`, `NextTxId = 1`.


2. **`deposit(env: Env, amount: i128)`**
* `sponsor.require_auth()`.
* Validate `amount > 0`.
* Transfer `amount` of `usdc_token` from `sponsor` to `env.current_contract_address()` via `token::Client::new(&env, &usdc_token).transfer(...)`.
* Emit `VaultDeposited`.


3. **`withdraw(env: Env, amount: i128)`**
* `sponsor.require_auth()`.
* Allow sponsor to reclaim unused USDC back to their own address.
* Validate `amount > 0` and sufficient vault balance.


4. **`set_daily_limit(env: Env, new_limit: i128)`**
* `sponsor.require_auth()`.
* Validate `new_limit > 0`. Update `DailyLimit`. Emit `RulesUpdated`.


5. **`set_safe_beneficiary(env: Env, beneficiary_hash: BytesN<32>, is_safe: bool)`**
* `sponsor.require_auth()`.
* Set `DataKey::SafeList(beneficiary_hash)` in persistent storage.


6. **`request_spend(env: Env, amount: i128, beneficiary_hash: BytesN<32>) -> u64`**
* `elder.require_auth()`.
* Validate `amount > 0` and vault USDC balance >= `amount`.
* **24-hour Rolling Reset:** If `env.ledger().timestamp() - last_reset >= 86400`, reset `SpentToday = 0` and update `LastResetTimestamp`.
* Check rules:
* `is_safe = persistent().get(&DataKey::SafeList(beneficiary_hash)).unwrap_or(false)`
* `within_limit = (spent_today + amount) <= daily_limit`


* Increment and fetch `tx_id`.
* **Branch 1 (Auto-Approve):** If `is_safe && within_limit`:
* Update `SpentToday += amount`.
* Transfer `amount` of `usdc_token` from `env.current_contract_address()` to `offramp_rail`.
* Emit `SpendExecuted { tx_id, amount, beneficiary_hash, was_escalated: false }`.
* Return `tx_id`.


* **Branch 2 (Pause & Escalate):** Otherwise:
* Store `PendingTransaction { id: tx_id, amount, beneficiary_hash, created_at: now, status: TxStatus::Pending }` in persistent storage.
* Determine `reason`: if `!is_safe` -> `NEW_PAYEE`, else `LIMIT`.
* Emit `SpendPaused { tx_id, amount, beneficiary_hash, reason }`.
* Return `tx_id`.




7. **`resolve_paused(env: Env, tx_id: u64, approve: bool)`**
* `sponsor.require_auth()`.
* Load `PendingTransaction` for `tx_id` (panic with `TxNotFound` if missing).
* Ensure `status == TxStatus::Pending` (panic with `TxAlreadyResolved` otherwise).
* If `approve`:
* Verify vault balance >= `tx.amount`.
* Transfer `tx.amount` from `env.current_contract_address()` to `offramp_rail`.
* Update `status = TxStatus::Approved` and persist.
* Emit `SpendExecuted { tx_id, amount: tx.amount, beneficiary_hash: tx.beneficiary_hash, was_escalated: true }`.


* If `!approve`:
* Update `status = TxStatus::Declined` and persist.
* Emit `SpendDeclined { tx_id, sponsor }`.




8. **Read-Only View Functions:**
* `get_balance(env: Env) -> i128`
* `get_daily_status(env: Env) -> (i128, i128)` (returns `(spent_today, daily_limit)`)
* `get_pending_tx(env: Env, tx_id: u64) -> Option<PendingTransaction>`
* `is_beneficiary_safe(env: Env, beneficiary_hash: BytesN<32>) -> bool`



### 3.5 Unit Test Requirements (`src/test.rs`)

Use `env.mock_all_auths()` and `env.register_stellar_asset_contract_v2(...)` to test:

1. Successful deposit and balance check.
2. Safe beneficiary + under daily limit -> auto-executes, transfers USDC to `offramp_rail`, emits `SpendExecuted`.
3. Unknown beneficiary -> pauses, does NOT move USDC, emits `SpendPaused` with `NEW_PAYEE`.
4. Safe beneficiary + over daily limit -> pauses, emits `SpendPaused` with `LIMIT`.
5. Sponsor approves paused tx -> releases USDC to `offramp_rail`, emits `SpendExecuted(was_escalated: true)`.
6. Sponsor declines paused tx -> marks `Declined`, funds stay in vault, emits `SpendDeclined`.
7. Double-resolving a transaction fails with `TxAlreadyResolved`.
8. Ledger timestamp advances by 86,401 seconds -> `spent_today` resets to `0`.

---


## 4. Part B: Lightweight FastAPI Stellar Bridge (`bridge/`)

Build a minimal, standalone async FastAPI service that connects the frontend and Fincra Sandbox to the deployed `YaroVault` Soroban contract. Do NOT use Django, Celery, or Redis.

### 4.1 Folder Structure
```text
bridge/
├── main.py            # FastAPI app, routes, and startup background event poller
├── stellar_client.py  # Soroban RPC calls (balance, simulate, getEvents) via stellar-sdk
├── db.py              # Lightweight SQLite storage (vaults, spend_intents, cursor)
├── fincra.py          # Async httpx client for Fincra sandbox NGN payout
├── requirements.txt
└── .env.example

```

### 4.2 Dependencies (`requirements.txt`)

* `fastapi`
* `uvicorn[standard]`
* `stellar-sdk[aiohttp]>=17.0.1`
* `httpx`
* `pydantic-settings`
* `aiosqlite`

### 4.3 SQLite Tables (`db.py`)

Keep persistence dead simple with 3 tables:

1. `vaults`: `contract_id` (PK), `sponsor_address`, `elder_address`, `created_at`
2. `spend_records`: `id` (PK), `contract_id`, `onchain_tx_id`, `stellar_tx_hash` (unique), `bank_code`, `account_number`, `account_name`, `beneficiary_hash`, `amount_usdc_stroops`, `amount_ngn`, `fx_rate`, `status` (`PENDING_ONCHAIN` | `PAUSED` | `EXECUTED` | `DECLINED` | `PAYOUT_COMPLETED`), `fincra_reference`
3. `rpc_cursor`: `id` (PK=1), `last_ledger` (integer)

### 4.4 Background Event Poller (FastAPI `lifespan` task)

Run a single `asyncio.create_task` loop every 5 seconds inside FastAPI's `lifespan` context manager:

* Poll Soroban Testnet RPC `get_events` for all registered `contract_id`s from `last_ledger`.
* Deduplicate every event by `stellar_tx_hash`.
* **On `SpendPaused`:** Update record to `PAUSED` and (optionally) POST an alert payload to `YARO_WEBHOOK_URL` so the sponsor gets notified.
* **On `SpendExecuted`:** Update record to `EXECUTED`, call `fincra.trigger_sandbox_payout(...)` using deterministic reference `f"YARO-STELLAR-{record_id}"`, then mark `PAYOUT_COMPLETED`.
* **On `SpendDeclined`:** Update record to `DECLINED`.

### 4.5 API Endpoints (`main.py`)

1. `POST /vaults` — Registers a newly deployed `YaroVault` `contract_id` with sponsor/elder addresses.
2. `GET /vaults/{contract_id}` — Queries Soroban RPC (read-only simulation) and returns live on-chain USDC balance, NGN equivalent, `daily_limit`, and `spent_today`.
3. `POST /spend-intent` — Accepts `{ contract_id, amount_ngn, bank_code, account_number, account_name }`. Computes `beneficiary_hash = sha256(f"{bank_code}:{account_number}")` and `amount_usdc_stroops`, saves a `PENDING_ONCHAIN` row, and returns `{ beneficiary_hash, amount_usdc_stroops, fx_rate, contract_id }` for the frontend to sign `request_spend()`.
4. `GET /vaults/{contract_id}/paused` — Lists all `PAUSED` spend records so the sponsor can approve or decline them on-chain.
5. `GET /health` — Returns RPC connectivity status and `last_ledger`.

### 4.6 Constraints

* Never store sponsor private keys on the server; all vault state changes are signed client-side.
* Keep `bridge/` completely runnable with `uvicorn main:app --reload` using only `.env` variables (`STELLAR_RPC_URL`, `STELLAR_NETWORK_PASSPHRASE`, `STELLAR_USDC_CONTRACT_ID`, `FINCRA_SECRET_KEY`, `FINCRA_BASE_URL`, `USDC_NGN_RATE`).
