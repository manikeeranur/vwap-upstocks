"""Sign-in, and each person's broker keys, daily broker logins and Telegram reminders."""
import asyncio
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import accounts, brokers, upstox
from ..observability import redact
from ..schemas import BrokerKeys, IndTotp, IndToken, Login, TelegramSetting
from .common import templates

router = APIRouter()
log = logging.getLogger(__name__)
# Upstox push approvals waiting for the notifier: api_key -> [(user_id, requested_at)]
_pending_upstox: dict[str, list[tuple[int, float]]] = {}


def _urls(request):
    base = request.app.state.settings.public_url
    return {"upstox_redirect": f"{base}/broker/upstox/callback", "upstox_notifier": f"{base}/broker/upstox/notify",
            "kite_redirect": f"{base}/broker/kite/callback"}


# ── Sign in / out ───────────────────────────────────────────────────────────
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/paper"):
    next = next if next.startswith("/") and not next.startswith("//") else "/paper"
    if getattr(request.app.state, "auto_user", None) is not None:
        return RedirectResponse(next, status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": next})


@router.post("/api/login")
async def login(body: Login, request: Request):
    user = await asyncio.to_thread(request.app.state.repository.user, username=body.username.strip())
    ok = user is not None and await asyncio.to_thread(accounts.check_password, body.password, user["password_hash"])
    if not ok:
        await asyncio.sleep(1)   # slow down guessing
        raise HTTPException(401, "Wrong username or password")
    response = JSONResponse({"ok": True, "username": user["username"]})
    response.set_cookie(accounts.SESSION_COOKIE, request.app.state.signer.session(user["id"]),
                        max_age=accounts.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")
    await asyncio.to_thread(request.app.state.repository.event, "login", f"{user['username']} signed in", component="application")
    return response


@router.post("/api/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(accounts.SESSION_COOKIE)
    return response


# ── Broker accounts ─────────────────────────────────────────────────────────
@router.get("/brokers", response_class=HTMLResponse)
async def brokers_page(request: Request):
    return templates.TemplateResponse(request, "brokers.html", {"urls": _urls(request)})


def _profile_of(row):
    try:
        p = json.loads(row.get("profile") or "{}")
    except ValueError:
        p = {}
    avatar = p.get("avatar") or ""
    return {"name": p.get("name") or "",
            "avatar": avatar if avatar.startswith("https://") else "", "user_id": row.get("broker_user") or ""}


def _status(request, user_id):
    repo, vault = request.app.state.repository, request.app.state.vault
    out = {}
    for b in brokers.BROKERS:
        row = repo.broker_account(user_id, b) or {}
        creds = brokers.Credentials(b, row.get("api_key", ""), vault.open(row.get("api_secret", "")),
                                    vault.open(row.get("access_token", "")), row.get("token_issued"))
        until = creds.valid_until()
        key = creds.api_key
        out[b] = {"has_key": bool(key),
                  "has_secret": bool(creds.api_secret), "logged_in": creds.token_valid(),
                  "valid_until": until.isoformat(timespec="minutes") if until else None,
                  "broker_user": row.get("broker_user") or "", "profile": _profile_of(row)}
    user = repo.user(user_id) or {}
    return {"market_calendar": request.app.state.exchange_calendar.day(upstox.today_ist()),
            "brokers": out, "telegram": bool(user.get("telegram_chat_id")), "username": user.get("username"),
            "urls": _urls(request), "telegram_bot": bool(request.app.state.settings.telegram_bot_token)}


@router.get("/api/brokers")
async def brokers_status(request: Request):
    return await asyncio.to_thread(_status, request, request.state.user_id)


@router.post("/api/brokers/keys")
async def save_keys(body: BrokerKeys, request: Request):
    secret = request.app.state.vault.seal(body.api_secret) if body.api_secret else None
    await asyncio.to_thread(request.app.state.repository.save_broker_keys, request.state.user_id, body.broker,
                            body.api_key.strip() if body.api_key else None, secret)
    return await asyncio.to_thread(_status, request, request.state.user_id)


def _creds(request, user_id, broker):
    row = request.app.state.repository.broker_account(user_id, broker) or {}
    vault = request.app.state.vault
    return brokers.Credentials(broker, row.get("api_key", ""), vault.open(row.get("api_secret", "")),
                               vault.open(row.get("access_token", "")), row.get("token_issued"))


async def _save_token(request, user_id, broker, token, profile):
    issued = brokers.now_iso()
    await asyncio.to_thread(request.app.state.repository.save_broker_token, user_id, broker,
                            request.app.state.vault.seal(token), issued, profile["user_id"], profile)
    username = (await asyncio.to_thread(request.app.state.repository.user, user_id) or {}).get("username", user_id)
    await request.app.state.trading._activity(f"{username} · {brokers.LABELS[broker]}: logged in for today")


@router.get("/broker/{broker}/login")
async def broker_login(broker: str, request: Request):
    if broker not in brokers.BROKERS:
        raise HTTPException(404, "Unknown broker")
    if broker == "indmoney":   # no login page: the token is pasted or generated on My brokers
        return RedirectResponse("/brokers#indmoney", status_code=303)
    creds = await asyncio.to_thread(_creds, request, request.state.user_id, broker)
    if not (creds.api_key and creds.api_secret):
        return RedirectResponse(f"/brokers?missing={broker}", status_code=303)
    state = request.app.state.signer.state(request.state.user_id)
    url = (brokers.upstox_login_url(creds.api_key, _urls(request)["upstox_redirect"], state) if broker == "upstox"
           else brokers.kite_login_url(creds.api_key, state))
    return RedirectResponse(url, status_code=303)


def _done(ok: bool, message: str):
    return RedirectResponse(f"/brokers?{'ok' if ok else 'error'}={message}", status_code=303)


@router.get("/broker/upstox/callback")
async def upstox_callback(request: Request, code: str = "", state: str = ""):
    uid = request.app.state.signer.verify(state, "o")
    if uid is None or uid != request.state.user_id or not code:
        return _done(False, "Upstox login expired or belongs to another person. Try again.")
    creds = await asyncio.to_thread(_creds, request, uid, "upstox")
    try:
        token, profile = await brokers.upstox_exchange_code(creds.api_key, creds.api_secret, code, _urls(request)["upstox_redirect"])
    except brokers.BrokerError as exc:
        return _done(False, redact(str(exc)))
    await _save_token(request, uid, "upstox", token, profile)
    return _done(True, "Upstox logged in for today")


@router.get("/broker/kite/callback")
async def kite_callback(request: Request, request_token: str = "", status: str = "", state: str = ""):
    uid = request.app.state.signer.verify(state, "o")
    if uid is None or uid != request.state.user_id or status != "success" or not request_token:
        return _done(False, "Kite login expired, was cancelled or belongs to another person. Try again.")
    creds = await asyncio.to_thread(_creds, request, uid, "kite")
    try:
        token, profile = await brokers.kite_exchange_token(creds.api_key, creds.api_secret, request_token)
    except brokers.BrokerError as exc:
        return _done(False, redact(str(exc)))
    await _save_token(request, uid, "kite", token, profile)
    return _done(True, "Kite logged in for today")


async def request_upstox_approval(app, user_id):
    """Push an approval to the person's Upstox app; the token comes back to /broker/upstox/notify."""
    row = await asyncio.to_thread(app.state.repository.broker_account, user_id, "upstox") or {}
    key, secret = row.get("api_key", ""), app.state.vault.open(row.get("api_secret", ""))
    if not (key and secret):
        raise brokers.BrokerError("Add your Upstox API key and secret first")
    data = await brokers.upstox_request_token(key, secret)
    _pending_upstox.setdefault(key, []).append((user_id, time.time()))
    return data


@router.post("/api/brokers/upstox/request")
async def upstox_request(request: Request):
    try:
        data = await request_upstox_approval(request.app, request.state.user_id)
    except brokers.BrokerError as exc:
        raise HTTPException(502, redact(str(exc)))
    return {"ok": True, "expires": data.get("authorization_expiry")}


@router.post("/broker/upstox/notify")
async def upstox_notify(request: Request):
    """Upstox posts the approved token here. Accepted only for a request this app made in the last 20 minutes,
    and only after Upstox itself confirms the token belongs to the expected Upstox user."""
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"ok": False}, status_code=400)
    key, token = str(body.get("client_id", "")), str(body.get("access_token", ""))
    waiting = [(u, t) for u, t in _pending_upstox.get(key, []) if time.time() - t < 1200]
    if body.get("message_type") != "access_token" or not token or not waiting:
        return JSONResponse({"ok": False}, status_code=202)
    try:
        profile = await brokers.upstox_profile(token)
    except brokers.BrokerError:
        return JSONResponse({"ok": False}, status_code=202)
    for uid, _ in waiting:
        row = await asyncio.to_thread(request.app.state.repository.broker_account, uid, "upstox") or {}
        if row.get("broker_user") and row["broker_user"] != profile["user_id"]:
            continue   # this API key's token belongs to someone else
        await _save_token(request, uid, "upstox", token, profile)
        _pending_upstox[key] = [(u, t) for u, t in _pending_upstox.get(key, []) if u != uid]
        return {"ok": True}
    return JSONResponse({"ok": False}, status_code=202)


async def _ind_save(request, token):
    try:
        profile = await brokers.ind_profile(token)
    except brokers.BrokerError as exc:
        raise HTTPException(400, redact(str(exc)))
    await asyncio.to_thread(request.app.state.repository.save_broker_keys, request.state.user_id, "indmoney")
    await _save_token(request, request.state.user_id, "indmoney", token, profile)
    return await asyncio.to_thread(_status, request, request.state.user_id)


@router.post("/api/brokers/indmoney/token")
async def ind_token(body: IndToken, request: Request):
    return await _ind_save(request, body.token.strip())


@router.post("/api/brokers/indmoney/totp")
async def ind_totp(body: IndTotp, request: Request):
    creds = await asyncio.to_thread(_creds, request, request.state.user_id, "indmoney")
    if not creds.api_key:
        raise HTTPException(400, "Save your INDstocks Client ID first")
    try:
        token = await brokers.ind_generate_token(creds.api_key, body.mpin, body.totp)
    except brokers.BrokerError as exc:
        raise HTTPException(400, redact(str(exc)))
    return await _ind_save(request, token)


@router.post("/api/brokers/telegram")
async def telegram(body: TelegramSetting, request: Request):
    await asyncio.to_thread(request.app.state.repository.set_telegram, request.state.user_id, body.chat_id)
    if body.chat_id and request.app.state.reminders:
        await request.app.state.reminders.send(body.chat_id, "upstocks: daily login reminders are on for this chat.")
    return await asyncio.to_thread(_status, request, request.state.user_id)
