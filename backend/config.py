"""Configuration settings for the Polymarket Weather Bot."""
from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # API Security
    API_SECRET_KEY: str = "change_me_in_production"

    # Database
    DATABASE_URL: str = "sqlite:///./tradingbot.db"

    # API Keys
    POLYMARKET_API_KEY: Optional[str] = None
    POLYMARKET_SECRET: Optional[str] = None
    POLYMARKET_PASSPHRASE: Optional[str] = None
    POLYMARKET_HOST: str = "https://clob.polymarket.com"
    
    # CLOB settings
    FEE_RATE: float = 0.0

    # AI API Keys
    GROQ_API_KEY: Optional[str] = None
    GROQ_MODEL: str = "llama-3.1-8b-instant"

    # Bot settings
    SIMULATION_MODE: bool = True
    INITIAL_BANKROLL: float = 1000.0
    KELLY_FRACTION: float = 0.15  # Fractional Kelly

    # Weather trading settings
    WEATHER_SCAN_INTERVAL_SECONDS: int = 300  # 5 min
    WEATHER_SETTLEMENT_INTERVAL_SECONDS: int = 1800  # 30 min
    WEATHER_MIN_EDGE_THRESHOLD: float = 0.08  
    WEATHER_MAX_ENTRY_PRICE: float = 0.70
    WEATHER_MAX_TRADE_SIZE: float = 50.0
    WEATHER_CITIES: str = "nyc,chicago,miami,los_angeles,denver"
    
    # Risk controls
    MAX_TOTAL_PENDING_TRADES: int = 20
    DAILY_LOSS_LIMIT: float = 100.0

    class Config:
        env_file = ".env"

settings = Settings()
