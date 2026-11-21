#![no_std]

use soroban_sdk::{
    contract, contracterror, contractevent, contractimpl, contracttype, panic_with_error,
    symbol_short, token, Address, BytesN, Env, Symbol,
};

const DAY: u64 = 86_400;
// Renew active entries for approximately 30 days (assuming five-second ledgers).
const TTL_THRESHOLD: u32 = 17_280;
const TTL_EXTEND: u32 = 518_400;

#[contract]
pub struct YaroVault;

#[contracttype]
#[derive(Clone)]
pub enum DataKey {
    Sponsor,
    Elder,
    UsdcToken,
    OfframpRail,
    DailyLimit,
    SpentToday,
    LastResetTimestamp,
    SafeList(BytesN<32>),
    NextTxId,
    PendingTx(u64),
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

#[contracterror]
#[derive(Copy, Clone, Debug, Eq, PartialEq)]
#[repr(u32)]
pub enum Error {
    InvalidAmount = 1,
    InsufficientVaultBalance = 2,
    TxNotFound = 3,
    TxAlreadyResolved = 4,
    // Reserved for ABI compatibility. require_auth uses native host auth errors.
    Unauthorized = 5,
    TxIdExhausted = 6,
}

#[contractevent]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct VaultDeposited {
    pub sponsor: Address,
    pub amount: i128,
}
#[contractevent]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SpendExecuted {
    pub tx_id: u64,
    pub amount: i128,
    pub beneficiary_hash: BytesN<32>,
    pub was_escalated: bool,
}
#[contractevent]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SpendPaused {
    pub tx_id: u64,
    pub amount: i128,
    pub beneficiary_hash: BytesN<32>,
    pub reason: Symbol,
}
#[contractevent]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SpendDeclined {
    pub tx_id: u64,
    pub sponsor: Address,
}
#[contractevent]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RulesUpdated {
    pub daily_limit: i128,
}

fn positive(env: &Env, amount: i128) {
    if amount <= 0 {
        panic_with_error!(env, Error::InvalidAmount);
    }
}
fn touch(env: &Env) {
    env.storage()
        .instance()
        .extend_ttl(TTL_THRESHOLD, TTL_EXTEND);
}
fn touch_key(env: &Env, key: &DataKey) {
    env.storage()
        .persistent()
        .extend_ttl(key, TTL_THRESHOLD, TTL_EXTEND);
}
fn address(env: &Env, key: DataKey) -> Address {
    env.storage().instance().get(&key).unwrap()
}
fn token_client(env: &Env) -> token::Client<'_> {
    token::Client::new(env, &address(env, DataKey::UsdcToken))
}
fn sufficient(env: &Env, amount: i128) {
    if token_client(env).balance(&env.current_contract_address()) < amount {
        panic_with_error!(env, Error::InsufficientVaultBalance);
    }
}
fn daily_status(env: &Env) -> (i128, i128) {
    let last: u64 = env
        .storage()
        .instance()
        .get(&DataKey::LastResetTimestamp)
        .unwrap();
    let spent = if env.ledger().timestamp().saturating_sub(last) >= DAY {
        0
    } else {
        env.storage().instance().get(&DataKey::SpentToday).unwrap()
    };
    (
        spent,
        env.storage().instance().get(&DataKey::DailyLimit).unwrap(),
    )
}

#[contractimpl]
impl YaroVault {
    pub fn __constructor(
        env: Env,
        sponsor: Address,
        elder: Address,
        usdc_token: Address,
        offramp_rail: Address,
        daily_limit: i128,
    ) {
        positive(&env, daily_limit);
        let store = env.storage().instance();
        store.set(&DataKey::Sponsor, &sponsor);
        store.set(&DataKey::Elder, &elder);
        store.set(&DataKey::UsdcToken, &usdc_token);
        store.set(&DataKey::OfframpRail, &offramp_rail);
        store.set(&DataKey::DailyLimit, &daily_limit);
        store.set(&DataKey::SpentToday, &0_i128);
        store.set(&DataKey::LastResetTimestamp, &env.ledger().timestamp());
        store.set(&DataKey::NextTxId, &1_u64);
        touch(&env);
    }

    pub fn deposit(env: Env, amount: i128) {
        let sponsor = address(&env, DataKey::Sponsor);
        sponsor.require_auth();
        positive(&env, amount);
        touch(&env);
        token_client(&env).transfer(&sponsor, env.current_contract_address(), &amount);
        VaultDeposited { sponsor, amount }.publish(&env);
    }

    pub fn withdraw(env: Env, amount: i128) {
        let sponsor = address(&env, DataKey::Sponsor);
        sponsor.require_auth();
        positive(&env, amount);
        sufficient(&env, amount);
        touch(&env);
        token_client(&env).transfer(&env.current_contract_address(), &sponsor, &amount);
    }

    pub fn set_daily_limit(env: Env, new_limit: i128) {
        address(&env, DataKey::Sponsor).require_auth();
        positive(&env, new_limit);
        touch(&env);
        env.storage()
            .instance()
            .set(&DataKey::DailyLimit, &new_limit);
        RulesUpdated {
            daily_limit: new_limit,
        }
        .publish(&env);
    }

    pub fn set_safe_beneficiary(env: Env, beneficiary_hash: BytesN<32>, is_safe: bool) {
        address(&env, DataKey::Sponsor).require_auth();
        touch(&env);
        let key = DataKey::SafeList(beneficiary_hash);
        env.storage().persistent().set(&key, &is_safe);
        touch_key(&env, &key);
    }

    pub fn request_spend(env: Env, amount: i128, beneficiary_hash: BytesN<32>) -> u64 {
        address(&env, DataKey::Elder).require_auth();
        positive(&env, amount);
        sufficient(&env, amount);
        touch(&env);
        let now = env.ledger().timestamp();
        let last: u64 = env
            .storage()
            .instance()
            .get(&DataKey::LastResetTimestamp)
            .unwrap();
        let (spent, limit) = daily_status(&env);
        if now.saturating_sub(last) >= DAY {
            env.storage().instance().set(&DataKey::SpentToday, &0_i128);
            env.storage()
                .instance()
                .set(&DataKey::LastResetTimestamp, &now);
        }
        let safe_key = DataKey::SafeList(beneficiary_hash.clone());
        let is_safe = env.storage().persistent().get(&safe_key).unwrap_or(false);
        if env.storage().persistent().has(&safe_key) {
            touch_key(&env, &safe_key);
        }
        // Subtraction avoids overflowing i128 on a large valid request.
        let within_limit = spent <= limit && amount <= limit - spent;
        let tx_id: u64 = env.storage().instance().get(&DataKey::NextTxId).unwrap();
        let next_id = tx_id
            .checked_add(1)
            .unwrap_or_else(|| panic_with_error!(&env, Error::TxIdExhausted));
        env.storage().instance().set(&DataKey::NextTxId, &next_id);
        if is_safe && within_limit {
            env.storage()
                .instance()
                .set(&DataKey::SpentToday, &(spent + amount));
            token_client(&env).transfer(
                &env.current_contract_address(),
                address(&env, DataKey::OfframpRail),
                &amount,
            );
            SpendExecuted {
                tx_id,
                amount,
                beneficiary_hash,
                was_escalated: false,
            }
            .publish(&env);
        } else {
            let tx = PendingTransaction {
                id: tx_id,
                amount,
                beneficiary_hash: beneficiary_hash.clone(),
                created_at: now,
                status: TxStatus::Pending,
            };
            let key = DataKey::PendingTx(tx_id);
            env.storage().persistent().set(&key, &tx);
            touch_key(&env, &key);
            SpendPaused {
                tx_id,
                amount,
                beneficiary_hash,
                reason: if is_safe {
                    symbol_short!("LIMIT")
                } else {
                    symbol_short!("NEW_PAYEE")
                },
            }
            .publish(&env);
        }
        tx_id
    }

    pub fn resolve_paused(env: Env, tx_id: u64, approve: bool) {
        let sponsor = address(&env, DataKey::Sponsor);
        sponsor.require_auth();
        let key = DataKey::PendingTx(tx_id);
        let mut tx: PendingTransaction = env
            .storage()
            .persistent()
            .get(&key)
            .unwrap_or_else(|| panic_with_error!(&env, Error::TxNotFound));
        if tx.status != TxStatus::Pending {
            panic_with_error!(&env, Error::TxAlreadyResolved);
        }
        touch(&env);
        if approve {
            sufficient(&env, tx.amount);
            token_client(&env).transfer(
                &env.current_contract_address(),
                address(&env, DataKey::OfframpRail),
                &tx.amount,
            );
            tx.status = TxStatus::Approved;
            SpendExecuted {
                tx_id,
                amount: tx.amount,
                beneficiary_hash: tx.beneficiary_hash.clone(),
                was_escalated: true,
            }
            .publish(&env);
        } else {
            tx.status = TxStatus::Declined;
            SpendDeclined { tx_id, sponsor }.publish(&env);
        }
        env.storage().persistent().set(&key, &tx);
        touch_key(&env, &key);
    }

    pub fn get_balance(env: Env) -> i128 {
        token_client(&env).balance(&env.current_contract_address())
    }
    pub fn get_daily_status(env: Env) -> (i128, i128) {
        daily_status(&env)
    }
    pub fn get_pending_tx(env: Env, tx_id: u64) -> Option<PendingTransaction> {
        env.storage().persistent().get(&DataKey::PendingTx(tx_id))
    }
    pub fn is_beneficiary_safe(env: Env, beneficiary_hash: BytesN<32>) -> bool {
        env.storage()
            .persistent()
            .get(&DataKey::SafeList(beneficiary_hash))
            .unwrap_or(false)
    }
}

#[cfg(test)]
mod test;
