"""Fincra sandbox payouts. A POST acknowledgment is not settlement."""

import httpx


class FincraClient:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.http = httpx.AsyncClient(
            base_url=settings.fincra_base_url,
            headers={"api-key": settings.fincra_secret_key.get_secret_value()},
            timeout=20,
            transport=transport,
        )

    @property
    def configured(self):
        return bool(
            self.settings.fincra_secret_key.get_secret_value() and self.settings.fincra_business_id
        )

    async def close(self):
        await self.http.aclose()

    @staticmethod
    def reference(record):
        return f"YARO-STELLAR-{record['id']}"

    async def trigger_sandbox_payout(self, record):
        if not self.configured:
            raise RuntimeError("Fincra sandbox credentials and business ID are required")
        first, _, last = record["account_name"].partition(" ")
        response = await self.http.post(
            "/disbursements/payouts",
            json={
                "business": self.settings.fincra_business_id,
                "sourceCurrency": "NGN",
                "destinationCurrency": "NGN",
                "amount": record["amount_ngn"],
                "description": "Yaro Stellar sandbox spend",
                "paymentDestination": "bank_account",
                "customerReference": self.reference(record),
                "beneficiary": {
                    "type": "individual",
                    "firstName": first,
                    "lastName": last,
                    "accountHolderName": record["account_name"],
                    "accountNumber": record["account_number"],
                    "bankCode": record["bank_code"],
                    "country": "NG",
                },
            },
        )
        response.raise_for_status()
        return self.parse(response.json(), record)

    async def lookup(self, record):
        response = await self.http.get(
            f"/disbursements/payouts/customer-reference/{self.reference(record)}"
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return self.parse(response.json(), record)

    def parse(self, response, record):
        data = response.get("data")
        if not isinstance(data, dict):
            raise ValueError("Unexpected Fincra response")
        if data.get("customerReference") != self.reference(record):
            raise ValueError("Fincra reference mismatch")
        return {
            "status": str(data.get("status", "processing")).lower(),
            "reference": data.get("reference"),
        }
