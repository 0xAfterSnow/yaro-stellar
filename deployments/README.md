# Testnet demo deployment

The demo vault is deployed and registered in the local bridge SQLite database.
Public addresses, WASM hash, and transaction hashes are in [testnet.json](testnet.json).

- Contract: `CCQVO7SM6R36F7KUGEPNAPYAXAYLXKWMTWCC6EUX2C2YL7LEGVVLYESC`
- Daily allowance: 100 USDC.
- Live bridge registration and balance/daily-status reads: verified.
- Three demo accounts funded with Testnet XLM.
- Sponsor and settlement USDC trustlines created.
- Sponsor funded with 20 Testnet USDC; 10 deposited into the vault.
- Approval and automatic-spend flows each released 1 USDC and completed a ₦1,500 Fincra sandbox payout.
- Decline flow preserved funds and created no payout.
- Final vault balance: 8 USDC. Daily automatic usage: 1 USDC / 100 USDC.
- [Transaction evidence and results](live-test-results.json).

[Deployment transaction](https://stellar.expert/explorer/testnet/tx/8aeb1a68346210afc84e5875f2131ab0e6e0aa070ccd633cc715f3dbaf002c23)

## Local wallet keys

CLI identities are in the ignored `.stellar/identity/` directory, with owner-only file permissions.
Use `--config-dir .stellar` explicitly on every CLI command; CLI 30 does not automatically select the project's local directory.
Aliases are `yaro-sponsor`, `yaro-elder`, and `yaro-settlement`. These are demo-only accounts. Never commit or share their identity files.

The current CLI executable is `/tmp/yaro-stellar-cli/stellar`; install Stellar CLI permanently if that temporary path is removed.

## Fund the sponsor with test USDC

Open [Circle's faucet](https://faucet.circle.com), select **USDC** and **Stellar Testnet**, and request tokens for:

```text
GAGDNEHY3FC2HS2QXHN56KRKDPIOKCND7CEYQMB4IBCOG6NXXRUZADLL
```

For a future deposit, the following sends 10 USDC into the vault (the initial deposit is already complete):

```bash
stellar contract invoke --config-dir .stellar --network testnet \
  --source-account yaro-sponsor \
  --id CCQVO7SM6R36F7KUGEPNAPYAXAYLXKWMTWCC6EUX2C2YL7LEGVVLYESC \
  -- deposit --amount 100000000
```

The initial deposit and spending tests are complete. Running the command again deposits another 10 USDC. Further spends follow the documented bridge flow. These tests used Fincra’s sandbox-only beneficiary data, not real bank recipients. Sponsor notifications were not exercised because no webhook URL is configured.
