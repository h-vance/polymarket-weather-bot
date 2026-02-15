"""Signal generator for Polymarket weather temperature markets using ensemble forecasts and live CLOB data."""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from backend.config import settings
from backend.data.weather import fetch_ensemble_forecast, EnsembleForecast, CITY_CONFIG
from backend.core.market_scanner import WeatherMarket, fetch_polymarket_weather_markets
from backend.models.database import SessionLocal, Signal
from backend.core.book_listener import book_listener

logger = logging.getLogger("trading_bot")

def calculate_edge(model_prob: float, market_prob: float) -> tuple[float, str]:
    """Calculate the edge against the market probability. Treats YES as 'up' and NO as 'down'."""
    if model_prob > market_prob:
        return model_prob - market_prob, "up"
    else:
        # Edge for NO is (1 - model_prob) - (1 - market_prob) = market_prob - model_prob
        return market_prob - model_prob, "down"

def calculate_kelly_size(edge: float, probability: float, market_price: float, direction: str, bankroll: float) -> float:
    """Fractional Kelly criterion for sizing."""
    if edge <= 0:
        return 0.0

    if direction == "up":
        # b = odds received - 1 = (1 / market_price) - 1
        b = (1.0 / market_price) - 1.0
        p = probability
    else:
        # betting NO
        no_price = 1.0 - market_price
        b = (1.0 / no_price) - 1.0
        p = 1.0 - probability

    q = 1.0 - p
    if b <= 0:
        return 0.0

    f_star = (p * b - q) / b
    fraction = max(0.0, f_star * settings.KELLY_FRACTION)
    return bankroll * fraction

@dataclass
class WeatherTradingSignal:
    """A trading signal for a weather temperature market."""
    market: WeatherMarket

    model_probability: float = 0.5
    market_probability: float = 0.5
    edge: float = 0.0
    direction: str = "yes"

    confidence: float = 0.5
    kelly_fraction: float = 0.0
    suggested_size: float = 0.0

    sources: List[str] = field(default_factory=list)
    reasoning: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)

    ensemble_mean: float = 0.0
    ensemble_std: float = 0.0
    ensemble_members: int = 0

    @property
    def passes_threshold(self) -> bool:
        return abs(self.edge) >= settings.WEATHER_MIN_EDGE_THRESHOLD

async def generate_weather_signal(market: WeatherMarket) -> Optional[WeatherTradingSignal]:
    """Generate signal using ensemble forecast and live CLOB orderbook prices."""
    forecast = await fetch_ensemble_forecast(market.city_key, market.target_date)
    if not forecast or not forecast.member_highs:
        return None

    if market.metric == "high":
        if market.direction == "above":
            model_yes_prob = forecast.probability_high_above(market.threshold_f)
        else:
            model_yes_prob = forecast.probability_high_below(market.threshold_f)
    else:
        if market.direction == "above":
            model_yes_prob = forecast.probability_low_above(market.threshold_f)
        else:
            model_yes_prob = forecast.probability_low_below(market.threshold_f)

    model_yes_prob = max(0.05, min(0.95, model_yes_prob))

    # Fetch live order book price instead of static Gamma API price
    live_yes_price, live_no_price = await book_listener.get_live_prices(market)
    
    if live_yes_price is None:
        logger.warning(f"Skipping {market.slug}: Stale or missing CLOB orderbook.")
        return None

    market_yes_prob = live_yes_price

    edge, direction_raw = calculate_edge(model_yes_prob, market_yes_prob)
    direction = "yes" if direction_raw == "up" else "no"

    entry_price = live_yes_price if direction == "yes" else live_no_price
    if entry_price > settings.WEATHER_MAX_ENTRY_PRICE:
        edge = 0.0

    if market.metric == "high":
        members = forecast.member_highs
    else:
        members = forecast.member_lows

    above_count = sum(1 for m in members if m > market.threshold_f)
    agreement_frac = max(above_count, len(members) - above_count) / len(members)
    confidence = min(0.9, agreement_frac)

    bankroll = settings.INITIAL_BANKROLL
    suggested_size = calculate_kelly_size(
        edge=abs(edge),
        probability=model_yes_prob,
        market_price=market_yes_prob,
        direction=direction_raw,
        bankroll=bankroll,
    )
    suggested_size = min(suggested_size, settings.WEATHER_MAX_TRADE_SIZE)

    mean_val = forecast.mean_high if market.metric == "high" else forecast.mean_low
    std_val = forecast.std_high if market.metric == "high" else forecast.std_low

    filter_status = "ACTIONABLE" if abs(edge) >= settings.WEATHER_MIN_EDGE_THRESHOLD else "FILTERED"
    filter_notes = []
    if entry_price > settings.WEATHER_MAX_ENTRY_PRICE:
        filter_notes.append(f"entry {entry_price:.0%} > {settings.WEATHER_MAX_ENTRY_PRICE:.0%}")
    filter_note = f" [{', '.join(filter_notes)}]" if filter_notes else ""

    reasoning = (
        f"[{filter_status}]{filter_note} "
        f"{market.city_name} {market.metric} {market.direction} {market.threshold_f:.0f}F on {market.target_date} | "
        f"Ensemble: {mean_val:.1f}F +/- {std_val:.1f}F ({forecast.num_members} members) | "
        f"Model YES: {model_yes_prob:.0%} vs Live CLOB: {market_yes_prob:.0%} | "
        f"Edge: {edge:+.1%} -> {direction.upper()} @ {entry_price:.0%} | "
        f"Agreement: {agreement_frac:.0%}"
    )

    return WeatherTradingSignal(
        market=market,
        model_probability=model_yes_prob,
        market_probability=market_yes_prob,
        edge=edge,
        direction=direction,
        confidence=confidence,
        kelly_fraction=suggested_size / bankroll if bankroll > 0 else 0,
        suggested_size=suggested_size,
        sources=[f"open_meteo_ensemble_{forecast.num_members}m", "polymarket_clob"],
        reasoning=reasoning,
        ensemble_mean=mean_val,
        ensemble_std=std_val,
        ensemble_members=forecast.num_members,
    )

async def scan_for_weather_signals() -> List[WeatherTradingSignal]:
    signals = []
    city_keys = [c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()]

    logger.info("=" * 50)
    logger.info("CLOB SCAN: Fetching weather markets and live books...")

    markets = []
    try:
        poly_markets = await fetch_polymarket_weather_markets(city_keys)
        markets.extend(poly_markets)
        logger.info(f"Discovered {len(poly_markets)} weather markets via Gamma API")
    except Exception as e:
        logger.error(f"Failed to fetch Polymarket weather markets: {e}")

    for market in markets:
        try:
            signal = await generate_weather_signal(market)
            if signal:
                signals.append(signal)
        except Exception as e:
            logger.debug(f"Signal generation failed for {market.title}: {e}")

    signals.sort(key=lambda s: abs(s.edge), reverse=True)

    actionable = [s for s in signals if s.passes_threshold]
    logger.info(f"SCAN COMPLETE: {len(signals)} signals, {len(actionable)} actionable")

    for signal in actionable[:5]:
        logger.info(f"  {signal.market.city_name}: {signal.market.metric} {signal.market.direction} "
                     f"{signal.market.threshold_f:.0f}F | Edge: {signal.edge:+.1%}")

    _persist_weather_signals(signals)
    return signals

def _persist_weather_signals(signals: List[WeatherTradingSignal]):
    """Save signals to database."""
    db = SessionLocal()
    try:
        for s in signals:
            db_signal = Signal(
                market_ticker=s.market.market_id,
                direction=s.direction,
                model_probability=s.model_probability,
                market_probability=s.market_probability,
                edge=s.edge,
                confidence=s.confidence,
                suggested_size=s.suggested_size,
                reasoning=s.reasoning,
                executed=False,
            )
            db.add(db_signal)
        db.commit()
    except Exception as e:
        logger.error(f"Failed to save signals: {e}")
        db.rollback()
    finally:
        db.close()
