import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_active_workspace, get_current_user
from app.backtest.data import BacktestDataError, fetch_candles, load_csv
from app.backtest.engine import BacktestConfig, run_backtest
from app.core.config import settings
from app.core.database import get_db
from app.models import BacktestRun, BacktestStatus, User, Workspace
from app.schemas.backtest import BacktestDetail, BacktestRequest, BacktestSummary
from app.services.audit import log_event
from app.services.binance_client import BinanceAPIError, BinanceFuturesClient
from app.services.news.calendar import load_events
from app.services.news.gate import GateDecision, aggregate, event_gate, news_gate
from app.services.news.store import load_signals
from app.strategy.base import StrategyParamError
from app.strategy.strategies import build_strategy, list_strategies

router = APIRouter()


def market_client_factory(testnet: bool) -> BinanceFuturesClient:
    """Public market-data client (no API keys). Overridable in tests."""
    return BinanceFuturesClient(api_key="", api_secret="", testnet=testnet)


@router.get("/")
async def get_strategies(user: User = Depends(get_current_user)) -> list[dict]:
    """Estratégias disponíveis e seus parâmetros."""
    return list_strategies()


async def build_backtest_gate(db: AsyncSession, req, candles) -> tuple[Any, dict[str, Any]]:
    """Replay the bot news filter / event pause over stored news and events (off by default)."""
    nf, ep = req.filters.news_filter, req.filters.event_pause
    if nf.mode == "off" and not ep.enabled:
        return None, {}
    start = datetime.fromtimestamp(candles[0].open_time / 1000, tz=timezone.utc)
    end = datetime.fromtimestamp(candles[-1].close_time / 1000, tz=timezone.utc)
    window = settings.NEWS_SENTIMENT_WINDOW_HOURS
    signals = await load_signals(db, start - timedelta(hours=window), end, req.symbol) if nf.mode != "off" else []
    events = await load_events(db, start - timedelta(days=1), end + timedelta(days=1)) if ep.enabled else []

    def gate(intent, candle) -> GateDecision:
        now = datetime.fromtimestamp(candle.close_time / 1000, tz=timezone.utc)
        decision, _ = event_gate(events, now, ep)
        if decision.block or nf.mode == "off":
            return decision
        sent = aggregate(
            signals, req.symbol, now, settings.NEWS_SENTIMENT_HALF_LIFE_HOURS, window, nf.high_impact_minutes
        )
        return decision.merge(news_gate(intent.side, sent, nf))

    info = {"filters": req.filters.model_dump(), "news_items_available": len(signals), "events_available": len(events)}
    return gate, info


@router.post("/backtest", response_model=BacktestDetail, status_code=status.HTTP_201_CREATED)
async def create_backtest(
    req: BacktestRequest,
    user: User = Depends(get_current_user),
    workspace: Workspace = Depends(get_active_workspace),
    db: AsyncSession = Depends(get_db),
) -> BacktestRun:
    try:
        strategy = build_strategy(req.strategy_key, req.params)
    except StrategyParamError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    try:
        if req.candles_csv:
            candles = load_csv(req.candles_csv, req.interval)
        else:
            async with market_client_factory(req.use_testnet_data) as client:
                candles = await fetch_candles(client, req.symbol, req.interval, req.start, req.end)  # type: ignore[arg-type]
    except BacktestDataError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except BinanceAPIError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"Binance: {exc.message}") from exc
    if len(candles) < 2:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="dados insuficientes para o backtest")

    config = BacktestConfig.from_dict(req.config.model_dump())
    entry_gate, filter_info = await build_backtest_gate(db, req, candles)
    run = BacktestRun(
        workspace_id=workspace.id,
        user_id=user.id,
        strategy_key=req.strategy_key,
        symbol=req.symbol,
        interval=req.interval,
        params=strategy.params,
        config={**config.public_dict(), **filter_info},
        status=BacktestStatus.RUNNING,
        start_time=datetime.fromtimestamp(candles[0].open_time / 1000, tz=timezone.utc),
        end_time=datetime.fromtimestamp(candles[-1].close_time / 1000, tz=timezone.utc),
    )
    try:
        result = await run_in_threadpool(run_backtest, strategy, candles, req.symbol, req.interval, config, entry_gate)
        data = result.as_dict()
        run.metrics = {**data["metrics"], "rejections": data["rejections"], "killed_at": data["killed_at"]}
        run.equity_curve = data["equity_curve"]
        run.trades = data["trades"][-2000:]
        run.status = BacktestStatus.COMPLETED
    except Exception as exc:  # keep the failed run for inspection
        run.status = BacktestStatus.FAILED
        run.error = str(exc)[:2000]
    db.add(run)
    await log_event(
        db,
        "backtest.run",
        user_id=user.id,
        workspace_id=workspace.id,
        strategy=req.strategy_key,
        symbol=req.symbol,
        status=run.status,
    )
    await db.commit()
    await db.refresh(run)
    return run


@router.get("/backtests", response_model=list[BacktestSummary])
async def list_backtests(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    workspace: Workspace = Depends(get_active_workspace),
    db: AsyncSession = Depends(get_db),
) -> list[BacktestRun]:
    rows = await db.scalars(
        select(BacktestRun)
        .where(BacktestRun.workspace_id == workspace.id)
        .order_by(BacktestRun.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows)


@router.get("/backtests/{run_id}", response_model=BacktestDetail)
async def get_backtest(
    run_id: uuid.UUID, workspace: Workspace = Depends(get_active_workspace), db: AsyncSession = Depends(get_db)
) -> BacktestRun:
    run = await db.scalar(select(BacktestRun).where(BacktestRun.id == run_id, BacktestRun.workspace_id == workspace.id))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Backtest não encontrado")
    return run
