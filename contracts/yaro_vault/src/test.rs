use super::*;
use soroban_sdk::{
    testutils::{Address as _, Events, Ledger},
    Event, IntoVal,
};

const USDC: i128 = 10_000_000;
struct Fixture {
    env: Env,
    vault: Address,
    sponsor: Address,
    elder: Address,
    rail: Address,
    token: Address,
    payee: BytesN<32>,
}
impl Fixture {
    fn new() -> Self {
        let env = Env::default();
        env.mock_all_auths();
        env.ledger().with_mut(|l| l.timestamp = 1_000);
        let sponsor = Address::generate(&env);
        let elder = Address::generate(&env);
        let rail = Address::generate(&env);
        let admin = Address::generate(&env);
        let token = env.register_stellar_asset_contract_v2(admin).address();
        token::StellarAssetClient::new(&env, &token).mint(&sponsor, &(1_000 * USDC));
        let vault = env.register(YaroVault, (&sponsor, &elder, &token, &rail, 100 * USDC));
        let payee = BytesN::from_array(&env, &[7; 32]);
        let f = Self {
            env,
            vault,
            sponsor,
            elder,
            rail,
            token,
            payee,
        };
        f.client().deposit(&(500 * USDC));
        f
    }
    fn client(&self) -> YaroVaultClient<'_> {
        YaroVaultClient::new(&self.env, &self.vault)
    }
    fn safe(&self) {
        self.client().set_safe_beneficiary(&self.payee, &true);
    }
    fn rail_balance(&self) -> i128 {
        token::Client::new(&self.env, &self.token).balance(&self.rail)
    }
    fn event(&self, event: impl Event) {
        assert_eq!(
            self.env
                .events()
                .all()
                .filter_by_contract(&self.vault)
                .events()
                .last()
                .unwrap(),
            &event.to_xdr(&self.env, &self.vault)
        );
    }
}

#[test]
fn deposit_and_withdraw() {
    let f = Fixture::new();
    f.event(VaultDeposited {
        sponsor: f.sponsor.clone(),
        amount: 500 * USDC,
    });
    assert_eq!(f.client().get_balance(), 500 * USDC);
    f.client().withdraw(&(20 * USDC));
    assert_eq!(f.client().get_balance(), 480 * USDC);
    assert_eq!(
        token::Client::new(&f.env, &f.token).balance(&f.sponsor),
        520 * USDC
    );
}

#[test]
fn safe_spend_executes_at_exact_limit() {
    let f = Fixture::new();
    f.safe();
    let id = f.client().request_spend(&(100 * USDC), &f.payee);
    f.event(SpendExecuted {
        tx_id: id,
        amount: 100 * USDC,
        beneficiary_hash: f.payee.clone(),
        was_escalated: false,
    });
    assert_eq!(id, 1);
    assert_eq!(f.rail_balance(), 100 * USDC);
    assert_eq!(f.client().get_balance(), 400 * USDC);
    assert_eq!(f.client().get_daily_status(), (100 * USDC, 100 * USDC));
    assert_eq!(f.client().get_pending_tx(&id), None);
    assert_eq!(f.client().request_spend(&1, &f.payee), 2);
    f.event(SpendPaused {
        tx_id: 2,
        amount: 1,
        beneficiary_hash: f.payee.clone(),
        reason: symbol_short!("LIMIT"),
    });
}

#[test]
fn unknown_payee_pauses_even_when_both_rules_fail() {
    let f = Fixture::new();
    let id = f.client().request_spend(&(101 * USDC), &f.payee);
    f.event(SpendPaused {
        tx_id: id,
        amount: 101 * USDC,
        beneficiary_hash: f.payee.clone(),
        reason: symbol_short!("NEW_PAYEE"),
    });
    assert_eq!(f.rail_balance(), 0);
    assert_eq!(f.client().get_balance(), 500 * USDC);
    assert_eq!(f.client().get_daily_status().0, 0);
    assert_eq!(
        f.client().get_pending_tx(&id).unwrap(),
        PendingTransaction {
            id,
            amount: 101 * USDC,
            beneficiary_hash: f.payee.clone(),
            created_at: 1_000,
            status: TxStatus::Pending
        }
    );
}

#[test]
fn approval_overrides_rules_without_consuming_daily_allowance() {
    let f = Fixture::new();
    let id = f.client().request_spend(&(200 * USDC), &f.payee);
    f.client().resolve_paused(&id, &true);
    f.event(SpendExecuted {
        tx_id: id,
        amount: 200 * USDC,
        beneficiary_hash: f.payee.clone(),
        was_escalated: true,
    });
    assert_eq!(f.rail_balance(), 200 * USDC);
    assert_eq!(f.client().get_balance(), 300 * USDC);
    assert_eq!(
        f.client().get_pending_tx(&id).unwrap().status,
        TxStatus::Approved
    );
    assert_eq!(f.client().get_daily_status().0, 0);
    assert!(!f.client().is_beneficiary_safe(&f.payee));
    assert_eq!(
        f.client().try_resolve_paused(&id, &true),
        Err(Ok(Error::TxAlreadyResolved.into()))
    );
    assert_eq!(
        f.client().try_resolve_paused(&id, &false),
        Err(Ok(Error::TxAlreadyResolved.into()))
    );
}

#[test]
fn decline_keeps_funds_and_is_final() {
    let f = Fixture::new();
    let id = f.client().request_spend(&USDC, &f.payee);
    f.client().resolve_paused(&id, &false);
    f.event(SpendDeclined {
        tx_id: id,
        sponsor: f.sponsor.clone(),
    });
    assert_eq!(
        f.client().get_pending_tx(&id).unwrap().status,
        TxStatus::Declined
    );
    assert_eq!(f.client().get_balance(), 500 * USDC);
    assert_eq!(f.rail_balance(), 0);
    assert_eq!(
        f.client().try_resolve_paused(&id, &true),
        Err(Ok(Error::TxAlreadyResolved.into()))
    );
    assert_eq!(
        f.client().try_resolve_paused(&999, &false),
        Err(Ok(Error::TxNotFound.into()))
    );
}

#[test]
fn rolling_window_boundary_and_read_view() {
    let f = Fixture::new();
    f.safe();
    f.client().request_spend(&(100 * USDC), &f.payee);
    f.env.ledger().with_mut(|l| l.timestamp = 1_000 + DAY - 1);
    assert_eq!(f.client().get_daily_status().0, 100 * USDC);
    f.env.ledger().with_mut(|l| l.timestamp += 1);
    assert_eq!(f.client().get_daily_status().0, 0);
    f.client().request_spend(&USDC, &f.payee);
    assert_eq!(f.client().get_daily_status().0, USDC);
    f.env.ledger().with_mut(|l| l.timestamp += DAY + 1);
    assert_eq!(f.client().get_daily_status().0, 0);
    f.client().request_spend(&(2 * USDC), &f.payee);
    assert_eq!(f.client().get_daily_status().0, 2 * USDC);
}

#[test]
fn invalid_amounts_and_insufficient_balance_leave_state_unchanged() {
    let f = Fixture::new();
    for amount in [0, -1] {
        assert_eq!(
            f.client().try_deposit(&amount),
            Err(Ok(Error::InvalidAmount.into()))
        );
        assert_eq!(
            f.client().try_withdraw(&amount),
            Err(Ok(Error::InvalidAmount.into()))
        );
        assert_eq!(
            f.client().try_set_daily_limit(&amount),
            Err(Ok(Error::InvalidAmount.into()))
        );
        assert_eq!(
            f.client().try_request_spend(&amount, &f.payee),
            Err(Ok(Error::InvalidAmount.into()))
        );
    }
    assert_eq!(
        f.client().try_withdraw(&(501 * USDC)),
        Err(Ok(Error::InsufficientVaultBalance.into()))
    );
    assert_eq!(
        f.client().try_request_spend(&(501 * USDC), &f.payee),
        Err(Ok(Error::InsufficientVaultBalance.into()))
    );
    assert_eq!(f.client().request_spend(&USDC, &f.payee), 1);
}

#[test]
fn pending_funds_are_not_reserved_and_failed_approval_can_retry() {
    let f = Fixture::new();
    let id = f.client().request_spend(&(500 * USDC), &f.payee);
    f.client().withdraw(&(500 * USDC));
    assert_eq!(
        f.client().try_resolve_paused(&id, &true),
        Err(Ok(Error::InsufficientVaultBalance.into()))
    );
    assert_eq!(
        f.client().get_pending_tx(&id).unwrap().status,
        TxStatus::Pending
    );
    f.client().deposit(&(500 * USDC));
    f.client().resolve_paused(&id, &true);
    assert_eq!(f.rail_balance(), 500 * USDC);
}

#[test]
fn rules_can_be_updated_and_safe_payee_removed() {
    let f = Fixture::new();
    f.safe();
    f.client().request_spend(&(50 * USDC), &f.payee);
    f.client().set_daily_limit(&(10 * USDC));
    f.event(RulesUpdated {
        daily_limit: 10 * USDC,
    });
    f.client().request_spend(&1, &f.payee);
    f.event(SpendPaused {
        tx_id: 2,
        amount: 1,
        beneficiary_hash: f.payee.clone(),
        reason: symbol_short!("LIMIT"),
    });
    f.client().set_safe_beneficiary(&f.payee, &false);
    assert!(!f.client().is_beneficiary_safe(&f.payee));
}

#[test]
fn large_amount_does_not_overflow_limit_check() {
    let f = Fixture::new();
    f.safe();
    f.client().request_spend(&1, &f.payee);
    token::StellarAssetClient::new(&f.env, &f.token)
        .mint(&f.vault, &(i128::MAX - f.client().get_balance()));
    let id = f.client().request_spend(&i128::MAX, &f.payee);
    assert_eq!(
        f.client().get_pending_tx(&id).unwrap().status,
        TxStatus::Pending
    );
    assert_eq!(f.client().get_daily_status().0, 1);
}

#[test]
fn all_mutations_require_authorization() {
    let f = Fixture::new();
    let id = f.client().request_spend(&USDC, &f.payee);
    f.env.set_auths(&[]);
    assert!(f.client().try_deposit(&USDC).is_err());
    assert!(f.client().try_withdraw(&USDC).is_err());
    assert!(f.client().try_set_daily_limit(&USDC).is_err());
    assert!(f
        .client()
        .try_set_safe_beneficiary(&f.payee, &true)
        .is_err());
    assert!(f.client().try_request_spend(&USDC, &f.payee).is_err());
    assert!(f.client().try_resolve_paused(&id, &true).is_err());
    assert_eq!(f.client().get_balance(), 500 * USDC);
}

#[test]
fn spend_requires_elder_and_rules_require_sponsor() {
    let f = Fixture::new();
    f.client().request_spend(&USDC, &f.payee);
    assert_eq!(f.env.auths()[0].0, f.elder);
    f.client().set_daily_limit(&USDC);
    assert_eq!(f.env.auths()[0].0, f.sponsor);
    assert_eq!(
        f.env.auths()[0].1.function,
        soroban_sdk::testutils::AuthorizedFunction::Contract((
            f.vault.clone(),
            Symbol::new(&f.env, "set_daily_limit"),
            (USDC,).into_val(&f.env)
        ))
    );
}

#[test]
#[should_panic(expected = "Error(Contract, #1)")]
fn constructor_rejects_zero_limit() {
    let f = Fixture::new();
    f.env
        .register(YaroVault, (&f.sponsor, &f.elder, &f.token, &f.rail, 0_i128));
}

#[test]
fn cumulative_spends_use_one_window_and_failed_transfer_rolls_back() {
    let f = Fixture::new();
    f.safe();
    f.client().request_spend(&(60 * USDC), &f.payee);
    f.client().request_spend(&(40 * USDC), &f.payee);
    assert_eq!(f.client().get_daily_status().0, 100 * USDC);
    assert_eq!(f.rail_balance(), 100 * USDC);
    // A token-level failure must not advance the ID or daily accounting.
    f.env.ledger().with_mut(|l| l.timestamp += DAY);
    token::StellarAssetClient::new(&f.env, &f.token).mint(&f.rail, &(i128::MAX - f.rail_balance()));
    assert!(f.client().try_request_spend(&USDC, &f.payee).is_err());
    assert_eq!(f.client().get_daily_status().0, 0);
    token::Client::new(&f.env, &f.token).burn(&f.rail, &i128::MAX);
    assert_eq!(f.client().request_spend(&USDC, &f.payee), 3);
    assert_eq!(f.client().get_daily_status().0, USDC);
}
