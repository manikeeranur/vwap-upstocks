"""Application composition root: configuration, lifecycle, and HTTP boundaries."""
import asyncio
from contextlib import asynccontextmanager
import logging
import sqlite3

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import httpx
import uvicorn

from . import upstox
from .accounts import Signer, Vault
from .api import accounts, market, momentum, operations, pages, research, trading
from .config import AppSettings
from .database import Database, ProcessLock
from .middleware import RequestLoggingMiddleware
from .observability import SERVER_LOG_CONFIG, close_logging, configure_logging, redact
from .repository import ConfigurationConflict, Repository
from .security import DashboardGuard, SessionAuth
from .stocks.domain import PortfolioError, StaleRevision
from .services.backtests import BacktestService
from .services.momentum import MomentumService
from .services.momentum_live import MomentumLiveService
from .services.reminders import ReminderService
from .services.trading import TradingService
from .services.quotes import QuoteService

log = logging.getLogger("upstocks.app")


def create_app(settings: AppSettings | None = None) -> FastAPI:
    settings = settings or AppSettings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        lock = ProcessLock(settings.database_path)
        lock.acquire()
        handlers = []
        trading_service = backtests = quotes = momentum_service = reminders = momentum_live = None
        app.state.ready = False
        try:
            handlers = configure_logging(settings)
            database = Database(settings.database_path, settings.sqlite_timeout)
            await asyncio.to_thread(database.initialize)
            repository = Repository(database)
            await asyncio.to_thread(repository.migrate_legacy, settings.legacy_dir)
            await asyncio.to_thread(repository.ensure_presets)
            await asyncio.to_thread(repository.recover)
            upstox.configure(settings.cache_dir)
            vault = await asyncio.to_thread(Vault, settings.database_path.parent / "secret.key")
            app.state.vault = vault
            app.state.signer = Signer(await asyncio.to_thread(repository.session_secret))
            auto = settings.auto_login and await asyncio.to_thread(repository.user, username=settings.auto_login)
            app.state.auto_user = auto["id"] if auto else None
            trading_service = await asyncio.to_thread(TradingService, repository, vault)
            backtests = BacktestService(repository, settings.max_backtests)
            app.state.database = database
            app.state.repository = repository
            app.state.dashboard_token = await asyncio.to_thread(repository.dashboard_token)
            app.state.export_lock = asyncio.Lock()
            app.state.audit_failures = 0
            app.state.trading = trading_service
            app.state.exchange_calendar = trading_service.calendar
            quotes = QuoteService(trading_service)
            app.state.quotes = quotes
            app.state.backtests = backtests
            momentum_service = MomentumService(settings.cache_dir, settings.database_path.parent / "momentum_runs")
            app.state.momentum = momentum_service
            reminders = ReminderService(app)
            app.state.reminders = reminders
            momentum_live = MomentumLiveService(app)
            await asyncio.to_thread(momentum_live.load)
            app.state.momentum_live = momentum_live
            if settings.scanner_enabled:
                trading_service.start()
                reminders.start()
                momentum_live.start()
            app.state.ready = True
            await asyncio.to_thread(repository.event, "startup", "Application started", details={"implementation": repository.implementation})
            log.info("Application started")
            yield
        except Exception:
            log.exception("Application lifecycle failed")
            raise
        finally:
            app.state.ready = False
            try:
                if momentum_live:
                    await momentum_live.stop()
                if reminders:
                    await reminders.stop()
                if trading_service:
                    await trading_service.stop()
                if quotes:
                    await quotes.stop()
                if backtests:
                    await backtests.stop()
                if momentum_service:
                    await momentum_service.stop()
                await upstox.close()
                if trading_service:
                    await asyncio.to_thread(trading_service.repository.event, "shutdown", "Application stopped")
                log.info("Application stopped")
            finally:
                close_logging(handlers)
                lock.release()

    app = FastAPI(title="Upstocks", version="0.2.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.state.settings = settings
    app.state.ready = False
    app.add_middleware(SessionAuth)
    app.add_middleware(DashboardGuard, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(RequestLoggingMiddleware)
    for router in (accounts.router, pages.router, trading.router, market.router, research.router, momentum.router, operations.router):
        app.include_router(router)

    @app.exception_handler(PortfolioError)
    async def stock_error(request: Request, exc: PortfolioError):
        return JSONResponse({"detail": redact(str(exc)), "request_id": getattr(request.state, "request_id", "")},
                            status_code=409 if isinstance(exc, StaleRevision) else 422)

    @app.exception_handler(ConfigurationConflict)
    async def configuration_conflict(request: Request, exc: ConfigurationConflict):
        return JSONResponse({"detail": str(exc), "state": request.app.state.trading.snapshot(getattr(request.state, "user_id", None))["state"]}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Do not echo raw request input (which may contain credentials).
        fields = [{"field": ".".join(map(str, error["loc"])), "message": redact(error["msg"])} for error in exc.errors()]
        return JSONResponse({"detail": "; ".join(f"{e['field']}: {e['message']}" for e in fields),
                             "errors": fields}, status_code=422)

    @app.exception_handler(upstox.UpstoxError)
    async def broker_error(request: Request, exc: upstox.UpstoxError):
        log.warning("Broker request failed: %s", redact(str(exc)))
        message = redact(str(exc))
        return JSONResponse({"detail": message, "error": message}, status_code=502)

    @app.exception_handler(httpx.RequestError)
    async def network_error(request: Request, exc: httpx.RequestError):
        log.warning("Broker connection failed: %s", type(exc).__name__)
        return JSONResponse({"detail": "Unable to reach the broker. Try again shortly."}, status_code=502)

    @app.exception_handler(sqlite3.Error)
    async def database_error(request: Request, exc: sqlite3.Error):
        log.exception("Database operation failed")
        return JSONResponse({"detail": "Storage unavailable. Check application logs before retrying an order action.",
                             "request_id": request.state.request_id}, status_code=503)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", "")
        return JSONResponse({"detail": "Unexpected server error. See application logs.", "request_id": rid},
                            status_code=500, headers={"X-Request-ID": rid, "Cache-Control": "no-store"})

    @app.get("/health/live", include_in_schema=False)
    async def liveness():
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def readiness(request: Request):
        if not app.state.ready:
            return JSONResponse({"status": "starting"}, status_code=503)
        health = await asyncio.to_thread(request.app.state.repository.health)
        scanner = request.app.state.trading._task
        workers = {
            "scanner": request.app.state.trading.worker_health(),
            "momentum": request.app.state.momentum_live.worker_health(),
            "reminders": request.app.state.reminders.worker_health(),
        }
        healthy = not settings.scanner_enabled or all(w["running"] and not w.get("stale") for w in workers.values())
        return JSONResponse({"status": "ok" if healthy else "degraded", **health,
                             "scanner": "running" if scanner and not scanner.done() else "disabled" if not settings.scanner_enabled else "stopped",
                             "workers": workers,
                             "active_backtests": len(request.app.state.backtests.tasks),
                             "people": len(request.app.state.trading.state["users"]),
                             "audit_failures": request.app.state.audit_failures}, status_code=200 if healthy else 503)

    return app


app = create_app()


def main():
    settings = AppSettings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, workers=1,
                access_log=False, log_level=settings.log_level.lower(), log_config=SERVER_LOG_CONFIG)


if __name__ == "__main__":
    main()
