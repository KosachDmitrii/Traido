"""Application settings — secrets from env only."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.enums import BrokerEnvironment, TradingMode, UniverseMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Traido"
    environment: str = Field(default="development", alias="TRAIDO_ENV")

    broker_env: BrokerEnvironment = Field(
        default=BrokerEnvironment.PAPER, alias="TRAIDO_BROKER_ENV"
    )
    trading_mode: TradingMode = Field(default=TradingMode.CONFIRMATION, alias="TRAIDO_TRADING_MODE")
    allow_live_trading: bool = Field(default=False, alias="TRAIDO_ALLOW_LIVE_TRADING")

    # Auth: if set, require X-API-Key on /api/*; if empty, local-only clients
    api_key: str | None = Field(default=None, alias="TRAIDO_API_KEY")

    database_url: str = Field(
        default="postgresql+asyncpg://traido:traido@localhost:5432/traido",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # Alpaca (paper broker + market data) — primary V1 vendor
    alpaca_api_key: str | None = Field(default=None, alias="ALPACA_API_KEY")
    alpaca_api_secret: str | None = Field(default=None, alias="ALPACA_API_SECRET")
    alpaca_data_base_url: str = Field(
        default="https://data.alpaca.markets", alias="ALPACA_DATA_BASE_URL"
    )
    alpaca_broker_base_url: str = Field(
        default="https://paper-api.alpaca.markets", alias="ALPACA_BROKER_BASE_URL"
    )

    # Later-stage providers
    finnhub_api_key: str | None = Field(default=None, alias="FINNHUB_API_KEY")
    fred_api_key: str | None = Field(default=None, alias="FRED_API_KEY")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")

    # ── Universe and scanner shape ───────────────────────────────────────────
    # The scanner's stage limits. Every one of them is a work budget, not a
    # business truth: the funnel narrows because each stage costs more per name
    # than the one before it, and these say how much of each stage is affordable.
    universe_mode: UniverseMode = Field(default=UniverseMode.CORE, alias="TRAIDO_UNIVERSE_MODE")
    universe_max_size: int = Field(default=1000, alias="TRAIDO_UNIVERSE_MAX_SIZE")
    universe_refresh_seconds: float = Field(
        default=6 * 3600.0, alias="TRAIDO_UNIVERSE_REFRESH_SECONDS"
    )
    market_prefilter_limit: int = Field(default=150, alias="TRAIDO_MARKET_PREFILTER_LIMIT")
    quant_top_k: int = Field(default=30, alias="TRAIDO_QUANT_TOP_K")
    deep_analysis_top_k: int = Field(default=20, alias="TRAIDO_DEEP_ANALYSIS_TOP_K")
    max_llm_candidates: int = Field(default=20, alias="TRAIDO_MAX_LLM_CANDIDATES")
    max_llm_calls_per_scan: int = Field(default=60, alias="TRAIDO_MAX_LLM_CALLS_PER_SCAN")
    scanner_concurrency: int = Field(default=4, alias="TRAIDO_SCANNER_CONCURRENCY")
    provider_batch_size: int = Field(default=200, alias="TRAIDO_PROVIDER_BATCH_SIZE")
    market_data_requests_per_minute: int = Field(default=180, alias="TRAIDO_MARKET_DATA_RPM")
    """The vendor account's own quota, in requests per minute.

    Belongs to the account, not to a caller: Stage 1 reads in batches, Stage 3
    paginates hourly bars a dozen requests at a time, and reconciliation reads
    quotes — all against one key. Budgeting them separately means the sum is
    whatever happens to be in flight, which is how a cycle earns a 429 storm
    while every individual component looks well-behaved.

    Alpaca's free data tier is 200/min. The default sits just under it.
    """
    scan_interval_seconds: float = Field(default=300.0, alias="TRAIDO_SCAN_INTERVAL_SECONDS")

    def assert_scanner_config(self) -> None:
        """Refuse a stage layout that cannot mean what it says.

        A funnel only narrows. `deep_analysis_top_k` larger than `quant_top_k`
        does not widen it — Stage 3 can only receive what Stage 2 shortlisted —
        it just makes the configured number a lie, and the first person to debug
        a thin shortlist would spend the afternoon on the wrong parameter. Same
        for a prefilter limit below the quant Top-K.

        Refused at startup rather than clamped at runtime, because a silently
        corrected setting reads as respected on the settings page.
        """
        problems: list[str] = []
        if self.universe_max_size < 0:
            problems.append("TRAIDO_UNIVERSE_MAX_SIZE must not be negative")
        if self.quant_top_k <= 0:
            problems.append("TRAIDO_QUANT_TOP_K must be positive")
        if self.deep_analysis_top_k <= 0:
            problems.append("TRAIDO_DEEP_ANALYSIS_TOP_K must be positive")
        if self.deep_analysis_top_k > self.quant_top_k:
            problems.append(
                f"TRAIDO_DEEP_ANALYSIS_TOP_K ({self.deep_analysis_top_k}) exceeds "
                f"TRAIDO_QUANT_TOP_K ({self.quant_top_k}) — Stage 3 cannot analyse "
                "more names than Stage 2 shortlisted"
            )
        if 0 < self.market_prefilter_limit < self.quant_top_k:
            problems.append(
                f"TRAIDO_MARKET_PREFILTER_LIMIT ({self.market_prefilter_limit}) is below "
                f"TRAIDO_QUANT_TOP_K ({self.quant_top_k}) — Stage 2 cannot rank more "
                "names than Stage 1 passed"
            )
        if self.max_llm_candidates > self.deep_analysis_top_k:
            problems.append(
                f"TRAIDO_MAX_LLM_CANDIDATES ({self.max_llm_candidates}) exceeds "
                f"TRAIDO_DEEP_ANALYSIS_TOP_K ({self.deep_analysis_top_k})"
            )
        if self.scanner_concurrency <= 0:
            problems.append("TRAIDO_SCANNER_CONCURRENCY must be positive")
        if self.provider_batch_size <= 0:
            problems.append("TRAIDO_PROVIDER_BATCH_SIZE must be positive")
        if self.scan_interval_seconds < 30:
            problems.append("TRAIDO_SCAN_INTERVAL_SECONDS must be at least 30")
        if problems:
            raise RuntimeError("Invalid scanner configuration: " + "; ".join(problems))

    def assert_safe_startup(self) -> None:
        self.assert_scanner_config()
        if self.broker_env != BrokerEnvironment.PAPER:
            if not self.allow_live_trading:
                raise RuntimeError(
                    "Refusing to start: TRAIDO_BROKER_ENV is not paper and live is not allowed."
                )
            raise RuntimeError("Live trading is not implemented in V1.")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.assert_safe_startup()
    return settings
