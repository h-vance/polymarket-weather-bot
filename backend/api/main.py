"""FastAPI backend for Polymarket Weather Bot dashboard."""
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, Security
from fastapi.security.api_key import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from datetime import datetime
from typing import List, Optional
import asyncio
import os

from backend.config import settings
from backend.models.database import (
    get_db, init_db, SessionLocal,
    Signal, Trade, BotState, AILog
)
from backend.core.weather_signals import scan_for_weather_signals

from pydantic import BaseModel

app = FastAPI(
    title="Polymarket Weather Bot",
    description="Institutional-grade Polymarket Weather CLOB Bot",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Security ---
API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

async def get_api_key(api_key_header: str = Security(api_key_header)):
    if api_key_header == settings.API_SECRET_KEY:
        return api_key_header
    raise HTTPException(status_code=403, detail="Could not validate credentials")


# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass

ws_manager = ConnectionManager()


# --- Pydantic Models ---
class BotStats(BaseModel):
    bankroll: float
    total_trades: int
    winning_trades: int
    win_rate: float
    total_pnl: float
    is_running: bool
    last_run: Optional[datetime]

class TradeResponse(BaseModel):
    id: int
    market_ticker: str
    platform: str
    event_slug: Optional[str] = None
    direction: str
    entry_price: float
    size: float
    timestamp: datetime
    settled: bool
    result: str
    pnl: Optional[float]

class WeatherForecastResponse(BaseModel):
    city_key: str
    city_name: str
    target_date: str
    mean_high: float
    std_high: float
    mean_low: float
    std_low: float
    num_members: int
    ensemble_agreement: float

class WeatherMarketResponse(BaseModel):
    slug: str
    market_id: str
    platform: str = "polymarket"
    title: str
    city_key: str
    city_name: str
    target_date: str
    threshold_f: float
    metric: str
    direction: str
    yes_price: float
    no_price: float
    volume: float

class WeatherSignalResponse(BaseModel):
    market_id: str
    city_key: str
    city_name: str
    target_date: str
    threshold_f: float
    metric: str
    direction: str
    model_probability: float
    market_probability: float
    edge: float
    confidence: float
    suggested_size: float
    reasoning: str
    ensemble_mean: float
    ensemble_std: float
    ensemble_members: int
    actionable: bool = False

class DashboardData(BaseModel):
    stats: BotStats
    recent_trades: List[TradeResponse]
    equity_curve: List[dict]
    weather_signals: List[WeatherSignalResponse] = []
    weather_forecasts: List[WeatherForecastResponse] = []

class EventResponse(BaseModel):
    timestamp: str
    type: str
    message: str
    data: dict = {}

# --- Startup / Shutdown ---
@app.on_event("startup")
async def startup():
    print("=" * 60)
    print("POLYMARKET WEATHER BOT v1.0")
    print("=" * 60)
    
    init_db()
    db = SessionLocal()
    try:
        state = db.query(BotState).first()
        if not state:
            state = BotState(
                bankroll=settings.INITIAL_BANKROLL,
                total_trades=0,
                winning_trades=0,
                total_pnl=0.0,
                is_running=True
            )
            db.add(state)
            db.commit()
            print(f"Created new bot state with ${settings.INITIAL_BANKROLL:,.2f} bankroll")
        else:
            state.is_running = True
            db.commit()
            print(f"Loaded bot state: Bankroll ${state.bankroll:,.2f}")
    finally:
        db.close()

    print(f"Simulation mode: {settings.SIMULATION_MODE}")
    from backend.core.scheduler import start_scheduler, log_event
    start_scheduler()
    log_event("success", "Polymarket Weather bot initialized")

@app.on_event("shutdown")
async def shutdown():
    from backend.core.scheduler import stop_scheduler
    stop_scheduler()

# --- Endpoints ---
@app.get("/")
async def root():
    return {"status": "ok", "message": "Polymarket Weather Bot API", "simulation": settings.SIMULATION_MODE}

@app.get("/api/stats", response_model=BotStats, dependencies=[Depends(get_api_key)])
async def get_stats(db: Session = Depends(get_db)):
    state = db.query(BotState).first()
    win_rate = state.winning_trades / state.total_trades if state.total_trades > 0 else 0
    return BotStats(
        bankroll=state.bankroll,
        total_trades=state.total_trades,
        winning_trades=state.winning_trades,
        win_rate=win_rate,
        total_pnl=state.total_pnl,
        is_running=state.is_running,
        last_run=state.last_run
    )

@app.get("/api/dashboard", response_model=DashboardData, dependencies=[Depends(get_api_key)])
async def get_dashboard(db: Session = Depends(get_db)):
    stats = await get_stats(db)
    
    trades = db.query(Trade).order_by(Trade.timestamp.desc()).limit(50).all()
    recent_trades = [
        TradeResponse(
            id=t.id, market_ticker=t.market_ticker, platform=t.platform,
            event_slug=t.event_slug, direction=t.direction, entry_price=t.entry_price,
            size=t.size, timestamp=t.timestamp, settled=t.settled,
            result=t.result, pnl=t.pnl
        ) for t in trades
    ]

    equity_trades = db.query(Trade).filter(Trade.settled == True).order_by(Trade.timestamp).all()
    equity_curve = []
    cumulative_pnl = 0
    for trade in equity_trades:
        if trade.pnl is not None:
            cumulative_pnl += trade.pnl
            equity_curve.append({
                "timestamp": trade.timestamp.isoformat(),
                "pnl": cumulative_pnl,
                "bankroll": settings.INITIAL_BANKROLL + cumulative_pnl
            })

    weather_signals_data = []
    weather_forecasts_data = []
    
    try:
        from backend.data.weather import fetch_ensemble_forecast, CITY_CONFIG
        wx_signals = await scan_for_weather_signals()
        
        weather_signals_data = [
            WeatherSignalResponse(
                market_id=s.market.market_id, city_key=s.market.city_key,
                city_name=s.market.city_name, target_date=s.market.target_date.isoformat(),
                threshold_f=s.market.threshold_f, metric=s.market.metric, direction=s.direction,
                model_probability=s.model_probability, market_probability=s.market_probability,
                edge=s.edge, confidence=s.confidence, suggested_size=s.suggested_size,
                reasoning=s.reasoning, ensemble_mean=s.ensemble_mean, ensemble_std=s.ensemble_std,
                ensemble_members=s.ensemble_members, actionable=s.passes_threshold
            ) for s in wx_signals
        ]

        city_keys = [c.strip() for c in settings.WEATHER_CITIES.split(",") if c.strip()]
        for city_key in city_keys:
            if city_key not in CITY_CONFIG:
                continue
            forecast = await fetch_ensemble_forecast(city_key)
            if forecast:
                weather_forecasts_data.append(WeatherForecastResponse(
                    city_key=forecast.city_key, city_name=forecast.city_name,
                    target_date=forecast.target_date.isoformat(), mean_high=forecast.mean_high,
                    std_high=forecast.std_high, mean_low=forecast.mean_low,
                    std_low=forecast.std_low, num_members=forecast.num_members,
                    ensemble_agreement=forecast.ensemble_agreement
                ))
    except Exception as e:
        print(f"Error fetching dashboard weather data: {e}")

    return DashboardData(
        stats=stats,
        recent_trades=recent_trades,
        equity_curve=equity_curve,
        weather_signals=weather_signals_data,
        weather_forecasts=weather_forecasts_data
    )

# --- Control Endpoints ---
@app.post("/api/bot/start", dependencies=[Depends(get_api_key)])
async def start_bot(db: Session = Depends(get_db)):
    from backend.core.scheduler import start_scheduler, log_event, is_scheduler_running
    state = db.query(BotState).first()
    if state:
        state.is_running = True
        db.commit()
    if not is_scheduler_running():
        start_scheduler()
    log_event("success", "Trading bot started")
    return {"status": "started", "is_running": True}

@app.post("/api/bot/stop", dependencies=[Depends(get_api_key)])
async def stop_bot(db: Session = Depends(get_db)):
    from backend.core.scheduler import log_event
    state = db.query(BotState).first()
    if state:
        state.is_running = False
        db.commit()
    log_event("info", "Trading bot paused")
    return {"status": "stopped", "is_running": False}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
