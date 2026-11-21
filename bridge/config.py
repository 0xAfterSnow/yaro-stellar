from decimal import ROUND_CEILING, Decimal, localcontext

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

STROOPS = 10_000_000
I128_MAX = 2**127 - 1


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    stellar_rpc_url: str = "https://soroban-testnet.stellar.org"
    stellar_network_passphrase: str = "Test SDF Network ; September 2015"
    stellar_usdc_contract_id: str = "CBIELTK6YBZJU5UP2WWQEUCYKLPU6AUNZ2BQ4WWFEIE3USCIHMXQDAMA"
    fincra_secret_key: SecretStr = SecretStr("")
    fincra_business_id: str = ""
    fincra_base_url: str = "https://sandboxapi.fincra.com"
    usdc_ngn_rate: Decimal = Field(default=Decimal(1500), gt=0, max_digits=18, decimal_places=8)
    bridge_api_key: SecretStr = SecretStr("")
    database_path: str = "bridge.sqlite3"
    poll_interval_seconds: float = Field(default=5, ge=1)
    poller_enabled: bool = True
    yaro_webhook_url: str = ""
    yaro_webhook_secret: SecretStr = SecretStr("")

    @field_validator("fincra_base_url")
    @classmethod
    def sandbox_only(cls, value):
        if value.rstrip("/") != "https://sandboxapi.fincra.com":
            raise ValueError("This bridge supports only the Fincra sandbox")
        return value.rstrip("/")

    @field_validator("stellar_network_passphrase")
    @classmethod
    def testnet_only(cls, value):
        if value != "Test SDF Network ; September 2015":
            raise ValueError("This bridge supports Stellar Testnet only")
        return value


def to_stroops(amount_ngn: Decimal, rate: Decimal) -> int:
    with localcontext() as ctx:
        ctx.prec = 60
        result = int((amount_ngn * STROOPS / rate).to_integral_value(rounding=ROUND_CEILING))
    if not 0 < result <= I128_MAX:
        raise ValueError("Converted amount is outside the contract's i128 range")
    return result


def to_ngn(stroops: int, rate: Decimal) -> str:
    with localcontext() as ctx:
        ctx.prec = 60
        return str((Decimal(stroops) * rate / STROOPS).quantize(Decimal("0.01")))
