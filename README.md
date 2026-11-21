# Yaro Vault — Non-Custodial Elder Protection on Stellar

**Yaro** bridges diaspora sponsors and their elderly parents in Nigeria. Sponsors fund a non-custodial **Soroban** vault in Stellar USDC, while parents spend seamlessly in Naira through an ultra-accessible, biometric interface—protected by on-chain spending guardrails.

> **Note for Stellar Buildathon Judges:** This repository contains the open-source Soroban smart contracts, unit tests, and Stellar RPC bridge specifications built for the Stellar Buildathon. Yaro’s core mobile-first web application and fiat settlement backend are maintained in a private repository to protect startup IP; temporary read-only access is available to judges upon request.

---

## Architecture Overview

```text
[Diaspora Sponsor]
       │  Deposits Stellar USDC
       ▼
┌──────────────────────────────────────────────────────────┐
│                 YaroVault (Soroban Contract)             │
│  • Holds USDC non-custodially                            │
│  • Enforces daily spending caps & Safe List on-chain     │
│  • Locks flagged/over-limit spends in Pending state      │
└──────────────┬───────────────────────────┬───────────────┘
               │ SpendExecuted             │ SpendPaused
               ▼                           ▼
   [Stellar Anchor / Off-Ramp]     [Sponsor Push Alert]
   (Testnet: Fincra Sandbox)       (Approve / Decline on-chain)
               │
               ▼
    [Local Merchant receives NGN]

```

## Core Smart Contract Features (`YaroVault`)

1. **Non-Custodial USDC Custody:** Funds live in a Soroban smart contract controlled by the sponsor's key—neither Yaro's servers nor bad actors can drain the balance.
2. **On-Chain Guardrails:** Enforces a rolling 24-hour `daily_limit` and a hashed `safe_list` of pre-approved beneficiary bank accounts.
3. **Pause & Escalate Engine:** Any spend request exceeding the daily cap or targeting an unknown beneficiary is automatically locked on-chain (`SpendPaused` event) until the sponsor cryptographically signs `resolve_paused(tx_id, true)`.
4. **Just-in-Time NGN Settlement:** Only the exact USDC required for an approved transaction is released to the settlement rail (SEP-24/SEP-6 African off-ramp partners in production; simulated via Fincra's NGN payout API during the Buildathon).

## Repository Structure

```text
yaro-vault/
├── contracts/
│   └── yaro_vault/
│       ├── Cargo.toml
│       └── src/
│           ├── lib.rs        # YaroVault Soroban contract
│           └── test.rs       # Unit tests (limits, safe list, pause/approve)
├── scripts/deploy-testnet.sh # Testnet deployment
├── PRD.md                    # Full technical specification
└── README.md

```

## Build & Test Locally

Requires current stable Rust, the `wasm32v1-none` target, and a Stellar CLI compatible with Soroban SDK 29.0.1. Dependencies are pinned in `Cargo.lock`.

```bash
# Install the CLI, build target and checks
cargo install stellar-cli --version 30.0.1 --locked
rustup target add wasm32v1-none
rustup component add rustfmt clippy

# Run unit tests
cargo test --workspace --locked

# Build optimized WASM binary
stellar contract build --locked

```

## Buildathon Roadmap

* [x] Architecture & technical specification
* [x] `YaroVault` Soroban contract + full unit test suite
* [ ] Stellar Testnet deployment with Testnet USDC (SEP-41 SAC)
* [x] FastAPI + SQLite RPC event listener bridging `SpendPaused` (Push Alerts) and `SpendExecuted` (Fincra NGN Sandbox Payout)
* [ ] End-to-end live demo (USDC deposit → Elder Naira spend → On-chain Pause → Sponsor Approval → Naira receipt)

## Contract semantics

- Amounts are integer USDC base units: `10_000_000` = 1 USDC.
- Only the sponsor can deposit, withdraw, change rules, or resolve requests; only the elder can request spends. Authorization failures use Soroban's native host errors (`Unauthorized = 5` is reserved in the ABI).
- The PRD's “rolling reset” is a 24-hour window anchored at construction and reset on the first spend request after expiry, not a sliding sum of all spends in the preceding 24 hours. `get_daily_status` reports zero after expiry without writing storage.
- Sponsor approvals bypass both rules and do not consume the automatic daily allowance or add the beneficiary to the safe list.
- Pending requests do not reserve funds. Withdrawals and other spends may leave insufficient funds for approval; a failed approval leaves the request pending and can be retried after funding.
- Resolved pending records remain available. Automatically executed requests exist in events, not in `get_pending_tx`.
- Active mutations renew instance storage; accessed safe-list entries and written pending records renew persistent storage. Operators must monitor and extend contract code/instance and inactive entry TTLs, and restore archived entries as needed. Read-only simulations do not maintain TTLs on-chain.
- Beneficiary hashes must be SHA-256 of the exact UTF-8 string `bank_code:account_number`; preserve leading zeroes. No bank details are stored on-chain.

Structured events use the SDK's snake-case topics: `vault_deposited`, `spend_executed`, `spend_paused`, `spend_declined`, and `rules_updated`. Fields are in the event data map. The future bridge should deduplicate by event ID, not transaction hash alone: one Stellar transaction can emit multiple events. These definitions use the [Soroban SDK structured event API](https://docs.rs/soroban-sdk/29.0.1/soroban_sdk/attr.contractevent.html).

## Deploy to Testnet

Create and fund a local Stellar CLI identity first. Set the public addresses and limit, then run:

```bash
export SOURCE_ACCOUNT=sponsor
export SPONSOR_ADDRESS=G... # Actual sponsor public address
export ELDER_ADDRESS=G...   # Actual elder public address
export OFFRAMP_RAIL=G...    # Actual settlement public address
export DAILY_LIMIT=1000000000 # 100 USDC
./scripts/deploy-testnet.sh
```

The script tests, builds, deploys with constructor arguments, and saves the `yaro-vault` CLI alias. Override `VAULT_ALIAS` for additional vaults. `USDC_TOKEN` defaults to the PRD's Testnet SAC; verify the asset and current Testnet availability before funding. No secret keys are stored in this repository. Deployment has not been performed by this implementation.

Part B of `PRD.md` specifies FastAPI and SQLite and supersedes its older Django references. The bridge is implemented in `bridge/`; see [bridge setup and integration](bridge/README.md). Live Testnet/Fincra end-to-end validation remains pending deployment and sandbox credentials.

## License

MIT