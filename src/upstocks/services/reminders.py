"""Morning broker-login reminders on Telegram, and the Upstox push approval at 08:30.

Only people with at least one live switch on at a broker they aren't logged in to today are
reminded. Brokers require a person to log in themselves each day, so this never logs in for them.
"""
import asyncio
from datetime import timedelta
import logging

import httpx

from .. import brokers, live

log = logging.getLogger(__name__)
REMIND_AT = ("08:30", "08:45", "09:00", "09:10")
FINAL_AT = "09:15"


class ReminderService:
    def __init__(self, app):
        self.app = app
        self._task = None

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="login-reminders")

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def send(self, chat_id, text):
        token = self.app.state.settings.telegram_bot_token
        if not (token and chat_id):
            return False
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                 json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
            return r.status_code == 200
        except httpx.HTTPError:
            log.warning("Telegram message failed")
            return False

    def _needs_login(self):
        """{user_id: [brokers with a live switch on but no valid token today]}."""
        trading, repo, vault = self.app.state.trading, self.app.state.repository, self.app.state.vault
        out = {}
        for acc in trading.state["accounts"].values():
            if not any(acc["live"].values()):
                continue
            row = repo.broker_account(acc["user"], acc["broker"]) or {}
            creds = brokers.Credentials(acc["broker"], row.get("api_key", ""), "", vault.open(row.get("access_token", "")),
                                        row.get("token_issued"))
            if not creds.token_valid(live.now_ist()):
                out.setdefault(acc["user"], []).append(acc["broker"])
        return out

    async def tick(self, hhmm):
        from ..api.accounts import request_upstox_approval
        missing = await asyncio.to_thread(self._needs_login)
        base = self.app.state.settings.public_url
        for uid, missing_brokers in missing.items():
            user = await asyncio.to_thread(self.app.state.repository.user, uid) or {}
            if hhmm == REMIND_AT[0] and "upstox" in missing_brokers:
                try:
                    await request_upstox_approval(self.app, uid)
                except brokers.BrokerError as exc:
                    log.warning("Upstox approval request failed for user %s: %s", uid, exc)
            links = "\n".join(f"• {brokers.LABELS[b]}: {base}/broker/{b}/login" for b in missing_brokers)
            if hhmm == FINAL_AT:
                text = f"⚠️ upstocks: still not logged in — today's live trades will be MISSED.\n{links}"
            else:
                approve = "\nUpstox: tap Approve in your Upstox app (sent now)." if hhmm == REMIND_AT[0] and "upstox" in missing_brokers else ""
                text = f"upstocks: log in for today's live trades ({hhmm} IST).{approve}\n{links}"
            await self.send(user.get("telegram_chat_id"), text)

    async def _loop(self):
        while True:
            now = live.now_ist()
            nxt = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
            await asyncio.sleep((nxt - now).total_seconds())
            hhmm = nxt.strftime("%H:%M")
            if nxt.weekday() < 5 and (hhmm in REMIND_AT or hhmm == FINAL_AT):
                try:
                    await self.tick(hhmm)
                except Exception:
                    log.exception("Login reminder failed")
