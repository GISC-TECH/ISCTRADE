import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_active_workspace, get_current_user
from app.backtest import archive
from app.backtest.data import BacktestDataError, fetch_candles, load_csv
from app.backtest.engine import BacktestConfig, run_backtest
from app.backtest.funding import FundingEvent, constant_funding, fetch_funding, funding_summary, load_funding_csv
from app.backtest.walkforward import WalkForwardConfig, WalkForwardError, run_walk_forward
from app.core.config import settings
from app.core.database import get_db
from app.exchanges.base import ExchangeAdapter, ExchangeAPIError
from app.exchanges.registry import public_adapter
from app.models import BacktestRun, BacktestStatus, User, Workspace
from app.schemas.backtest import BacktestDetail, BacktestRequest, BacktestSummary, WalkForwardRequest
from app.services.audit import log_event
from app.services.news.calendar import load_events
from app.services.news.gate import GateDecision, aggregate, event_gate, news_gate
from app.services.news.store import load_signals
from app.strategy.base import StrategyParamError
from app.strategy.presets import list_presets
from app.strategy.strategies import build_strategy, list_strategies

router = APIRouter()
logger = logging.getLogger(__name__)


def market_client_factory(testnet: bool) -> ExchangeAdapter:
    """Public market-data adapter (no API keys). Overridable in tests."""
    return public_adapter(testnet)


def archive_transport():
    """httpx transport for data.binance.vision (None = real network). Overridable in tests."""
    return None


async def load_candles(req: BacktestRequest) -> list:
    try:
        if req.candles_csv:
            candles = load_csv(req.candles_csv, req.interval)
        elif req.data_source == "archive":
            if req.use_testnet_data:
                raise BacktestDataError("o arquivo data.binance.vision só tem dados da mainnet")
            candles = await archive.fetch_archive_candles(
                req.symbol,
                req.interval,
                req.start,
                req.end,
                transport=archive_transport(),  # type: ignore[arg-type]
            )
        else:
            async with market_client_factory(req.use_testnet_data) as client:
                candles = await fetch_candles(client, req.symbol, req.interval, req.start, req.end)  # type: ignore[arg-type]
    except BacktestDataError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except ExchangeAPIError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"Binance: {exc.message}") from exc
    if len(candles) < 2:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="dados insuficientes para o backtest")
    return candles


async def load_funding(req: BacktestRequest, candles: list) -> tuple[list[FundingEvent], dict]:
    """Funding events for the candle range + a summary stored with the run (see app/backtest/funding.py)."""
    opts = req.funding
    start_ms, end_ms = candles[0].open_time, candles[-1].close_time
    mode = opts.mode
    if mode == "auto":
        mode = "csv" if opts.csv else ("none" if req.candles_csv else "exchange")
    try:
        if mode in ("off", "none"):
            return [], funding_summary([], mode)
        if mode == "csv":
            if not opts.csv:
                raise BacktestDataError("funding.csv vazio")
            return _summarized([e for e in load_funding_csv(opts.csv) if start_ms <= e.time <= end_ms], "csv")
        if mode == "constant":
            from decimal import Decimal

            return _summarized(constant_funding(start_ms, end_ms, Decimal(str(opts.constant_rate))), "constant")
        if mode == "archive":
            if req.use_testnet_data:
                raise BacktestDataError("o arquivo data.binance.vision só tem dados da mainnet")
            events = await archive.fetch_archive_funding(req.symbol, start_ms, end_ms, transport=archive_transport())
            return _summarized(events, "archive")
    except BacktestDataError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    # exchange history; in auto mode fall back to the public archive (e.g. REST geo-blocked) and then to none
    try:
        async with market_client_factory(req.use_testnet_data) as client:
            events = await fetch_funding(client, req.symbol, start_ms, end_ms)
        return _summarized(events, "exchange")
    except (ExchangeAPIError, httpx.HTTPError, OSError) as exc:
        if opts.mode == "exchange":
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="histórico de funding indisponível") from exc
        # e.g. HTTP 451 (geo-blocked REST): the static archive is usually reachable
        logger.info("funding history from exchange unavailable (%s); trying archive", type(exc).__name__)
    if not req.use_testnet_data and (req.data_source == "archive" or _is_binance()):
        try:
            events = await archive.fetch_archive_funding(req.symbol, start_ms, end_ms, transport=archive_transport())
            if events:
                return _summarized(events, "archive")
        except Exception as exc:  # noqa: BLE001
            logger.info("funding archive unavailable (%s)", type(exc).__name__)
    return [], funding_summary([], "unavailable")


def _is_binance() -> bool:
    from app.exchanges.registry import get_adapter_class

    try:
        return get_adapter_class().exchange_id == "binance_usdm"
    except Exception:  # noqa: BLE001
        return False


def _summarized(events: list[FundingEvent], source: str) -> tuple[list[FundingEvent], dict]:
    return events, funding_summary(events, source)


@router.get("/")
async def get_strategies(user: User = Depends(get_current_user)) -> list[dict]:
    """Estratégias disponíveis e seus parâmetros."""
    return list_strategies()


@router.get("/presets")
async def get_presets(user: User = Depends(get_current_user)) -> list[dict]:
    """Robôs prontos: estratégia + timeframe + parâmetros + opções de risco/notícias (sempre em modo paper)."""
    return list_presets()


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

    candles = await load_candles(req)
    config = BacktestConfig.from_dict(req.config.model_dump())
    entry_gate, filter_info = await build_backtest_gate(db, req, candles)
    funding, funding_info = await load_funding(req, candles)
    run = BacktestRun(
        workspace_id=workspace.id,
        user_id=user.id,
        strategy_key=req.strategy_key,
        symbol=req.symbol,
        interval=req.interval,
        params=strategy.params,
        config={**config.public_dict(), **filter_info, **funding_info, "data_source": _source(req)},
        status=BacktestStatus.RUNNING,
        start_time=datetime.fromtimestamp(candles[0].open_time / 1000, tz=timezone.utc),
        end_time=datetime.fromtimestamp(candles[-1].close_time / 1000, tz=timezone.utc),
    )
    try:
        result = await run_in_threadpool(
            run_backtest, strategy, candles, req.symbol, req.interval, config, entry_gate, funding
        )
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


def _source(req: BacktestRequest) -> str:
    return "csv" if req.candles_csv else req.data_source


@router.post("/walk-forward", response_model=BacktestDetail, status_code=status.HTTP_201_CREATED)
async def create_walk_forward(
    req: WalkForwardRequest,
    user: User = Depends(get_current_user),
    workspace: Workspace = Depends(get_active_workspace),
    db: AsyncSession = Depends(get_db),
) -> BacktestRun:
    """Walk-forward: otimiza `param_grid` em cada janela in-sample e mede fora da amostra (OOS).

    O resultado fica salvo como um backtest (`config.mode = "walk_forward"`): `metrics` traz o agregado OOS e
    `metrics.folds` cada janela (parâmetros escolhidos, métricas IS e OOS); `equity_curve` é a curva OOS encadeada.
    """
    try:
        base = build_strategy(req.strategy_key, req.params)
    except StrategyParamError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    candles = await load_candles(req)
    config = BacktestConfig.from_dict(req.config.model_dump())
    entry_gate, filter_info = await build_backtest_gate(db, req, candles)
    funding, funding_info = await load_funding(req, candles)
    wf = WalkForwardConfig(**req.walk_forward.model_dump(), max_candle_steps=settings.WALK_FORWARD_MAX_CANDLE_STEPS)
    try:
        report = await run_in_threadpool(
            run_walk_forward, req.strategy_key, base.params, candles, req.symbol, req.interval, config, wf,
            req.param_grid, funding, entry_gate,
        )  # fmt: skip
    except WalkForwardError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    run = BacktestRun(
        workspace_id=workspace.id,
        user_id=user.id,
        strategy_key=req.strategy_key,
        symbol=req.symbol,
        interval=req.interval,
        params=report["metrics"]["folds"][-1]["best_params"],
        config={
            **config.public_dict(),
            **filter_info,
            **funding_info,
            "data_source": _source(req),
            "mode": report["metrics"]["mode"],
            "walk_forward": report["metrics"]["settings"],
        },
        status=BacktestStatus.COMPLETED,
        start_time=datetime.fromtimestamp(candles[0].open_time / 1000, tz=timezone.utc),
        end_time=datetime.fromtimestamp(candles[-1].close_time / 1000, tz=timezone.utc),
        metrics=report["metrics"],
        equity_curve=_downsample(report["equity_curve"]),
        trades=report["trades"][-2000:],
    )
    db.add(run)
    await log_event(
        db,
        "backtest.walk_forward",
        user_id=user.id,
        workspace_id=workspace.id,
        strategy=req.strategy_key,
        symbol=req.symbol,
        folds=req.walk_forward.folds,
        combinations=report["metrics"]["combinations"],
    )
    await db.commit()
    await db.refresh(run)
    return run


def _downsample(points: list) -> list:
    from app.backtest.engine import downsample

    return downsample(points, 1000) if points else points


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
