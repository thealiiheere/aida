"""Calls to the store backend that the phone system makes itself, not through Aida."""

import asyncio
import logging
import re

import httpx2

from .config import VoiceSettings

log = logging.getLogger("aida.voice.backend")

REPORT_ATTEMPTS = 3
E164 = re.compile(r"\+\d{7,15}")
# What Twilio sends when a caller hides their number: the keypad spellings of
# anonymous, restricted, blocked, unknown and unavailable. Many people share these.
HIDDEN_NUMBERS = {"+266696687", "+7378742833", "+2562533", "+8566524", "+86282452253"}


def caller_phone(number: str) -> str | None:
    return number if E164.fullmatch(number) and number not in HIDDEN_NUMBERS else None


class StoreBackend:
    def __init__(
        self, settings: VoiceSettings, transport: httpx2.AsyncBaseTransport | None = None
    ):
        key = settings.agent_api_key
        self._client = httpx2.AsyncClient(
            base_url=settings.backend_url,
            headers={"X-API-Key": key.get_secret_value()} if key else {},
            timeout=15,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def find_customer(self, phone: str) -> dict | None:
        """The Telegram account linked to a phone number. None if there isn't one."""
        try:
            response = await self._client.post(
                "/api/customers/lookup",
                json={"phone": phone},
                timeout=3,  # the caller is listening to ringing
            )
            if response.is_success:
                return response.json()
            if response.status_code != 404:
                log.warning("customer lookup failed: HTTP %s", response.status_code)
        except (httpx2.HTTPError, ValueError) as exc:
            log.warning("customer lookup failed: %s", type(exc).__name__)
        return None

    async def report_call(self, report: dict) -> bool:
        """Send the seller the orders and transcript of a finished call."""
        return await self._deliver("/api/admin/notify-order", {"call": report})

    async def call_ended(self, call: dict) -> bool:
        """Have a finished call logged and summarised."""
        # Claude writes the summary while this waits.
        return await self._deliver("/api/telephony/call-ended", call, timeout=90)

    async def _deliver(self, path: str, body: dict, timeout: float = 15) -> bool:
        for attempt in range(1, REPORT_ATTEMPTS + 1):
            wait = attempt
            try:
                response = await self._client.post(path, json=body, timeout=timeout)
                if response.is_success:
                    return True
                problem = f"HTTP {response.status_code}: {response.text[:300]}"
                if response.status_code == 429:
                    wait = int(response.headers.get("Retry-After", 60))
                elif response.status_code < 500:
                    break  # trying again can't help
            except httpx2.HTTPError as exc:
                problem = type(exc).__name__
            if attempt < REPORT_ATTEMPTS:
                await asyncio.sleep(wait)
        log.error("%s not delivered (%s)", path, problem)
        return False
