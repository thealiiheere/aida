"""Checks every key and address in .env, and says what works.

    uv run python check.py

Each service gets one small request: the AI (Claude, OpenAI or Gemini) is asked for a
word, Telegram for the bot's name, VoiceLab speaks "Salom." and is asked to hear it back,
and Gemini Live (if VOICE_ENGINE is gemini_live) is asked to say a word.
Nothing is sent to customers or the admin.
"""

import asyncio
import json
import sys
from dataclasses import dataclass

import anthropic
import httpx2
from websockets.asyncio.client import connect

from agent.openai_llm import PROVIDERS, as_openai
from app.config import BASE_DIR, Settings
from voice.audio import phone_to_pcm16k
from voice.config import VoiceSettings
from voice.tts import TtsError, VoiceLabTts


@dataclass
class Result:
    name: str
    ok: bool | None  # None: not set up, which is fine
    detail: str


def check_llm(settings: VoiceSettings, http_client: httpx2.Client | None = None) -> Result:
    """Whichever AI LLM_PROVIDER names."""
    if settings.llm_provider in ("openai", "gemini"):
        return check_openai(settings, http_client)
    return check_claude(settings, http_client)


def check_openai(settings: VoiceSettings, http_client: httpx2.Client | None = None) -> Result:
    """OpenAI, or Gemini through its OpenAI-compatible API."""
    name, key_name = PROVIDERS.get(settings.llm_provider, PROVIDERS["openai"])
    if name != "OpenAI":
        return _check_compatible(name, key_name, as_openai(settings), http_client)
    key = settings.openai_api_key
    model = settings.openai_model
    if not key:
        return Result("OpenAI", False, "OPENAI_API_KEY is empty. Aida can't answer anyone.")
    body = {"model": model, "messages": [{"role": "user", "content": "Reply with the word OK."}]}
    if settings.openai_reasoning_effort:
        body["reasoning_effort"] = settings.openai_reasoning_effort
    client = http_client or httpx2.Client(timeout=30)
    try:
        response = client.post(
            settings.openai_base_url.rstrip("/") + "/chat/completions", json=body,
            headers={"Authorization": f"Bearer {key.get_secret_value()}"},
        )  # fmt: skip
    except httpx2.HTTPError:
        return Result("OpenAI", False, "Couldn't reach OpenAI. Is this computer online?")
    finally:
        client.close()
    if response.is_success:
        return Result("OpenAI", True, f"answers, with {model}")
    try:
        error = response.json().get("error") or {}
    except ValueError:
        error = {}
    code, message = str(error.get("code") or ""), str(error.get("message") or "")[:200]
    if response.status_code == 401:
        return Result("OpenAI", False, "OPENAI_API_KEY was rejected. Make a new key at "
                      "platform.openai.com and paste it in.")  # fmt: skip
    if code == "insufficient_quota":
        return Result("OpenAI", False, "The OpenAI account has no credit left. Add some under "
                      "Billing at platform.openai.com.")  # fmt: skip
    if response.status_code == 404 or code == "model_not_found":
        return Result("OpenAI", False, f"There is no model called {model}, or this key can't use "
                      "it. Fix OPENAI_MODEL.")  # fmt: skip
    if response.status_code == 429:
        return Result("OpenAI", False, "Too many requests just now. Try again in a minute.")
    if "reasoning_effort" in message:
        return Result("OpenAI", False, f"{model} doesn't reason, so leave OPENAI_REASONING_EFFORT "
                      "empty.")  # fmt: skip
    return Result("OpenAI", False, f"OpenAI answered HTTP {response.status_code}: {message}")


def _check_compatible(
    name: str, key_name: str, settings: VoiceSettings, http_client: httpx2.Client | None
) -> Result:
    key, model = settings.openai_api_key, settings.openai_model
    if not key:
        return Result(name, False, f"{key_name} is empty. Aida can't answer anyone.")
    body = {"model": model, "messages": [{"role": "user", "content": "Reply with the word OK."}]}
    if settings.openai_reasoning_effort:
        body["reasoning_effort"] = settings.openai_reasoning_effort
    client = http_client or httpx2.Client(timeout=30)
    try:
        response = client.post(
            settings.openai_base_url.rstrip("/") + "/chat/completions", json=body,
            headers={"Authorization": f"Bearer {key.get_secret_value()}"},
        )  # fmt: skip
    except httpx2.HTTPError:
        return Result(name, False, f"Couldn't reach {name}. Is this computer online?")
    finally:
        client.close()
    if response.is_success:
        return Result(name, True, f"answers, with {model}")
    return Result(name, False, _api_problem(name, key_name, model, response))


def _api_problem(name: str, key_name: str, model: str, response: httpx2.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = {}
    body = body[0] if isinstance(body, list) and body else body
    error = body.get("error") if isinstance(body, dict) else None
    message = str((error or {}).get("message") or "")[:200]
    status = response.status_code
    if status in (400, 401, 403) and "key" in message.lower():
        return f"{key_name} was rejected. Make a new key and paste it in."
    if status == 404:
        return f"There is no model called {model}, or this key can't use it."
    if status == 429:
        return f"{name} says too many requests or no quota left. Check the account's limits."
    return f"{name} answered HTTP {status}: {message}"


def check_claude(settings: VoiceSettings, http_client: httpx2.Client | None = None) -> Result:
    key = settings.anthropic_api_key
    if not key:
        return Result("Claude", False, "ANTHROPIC_API_KEY is empty. Aida can't answer anyone.")
    client = anthropic.Anthropic(
        api_key=key.get_secret_value(), base_url=settings.anthropic_base_url,
        max_retries=0, timeout=30, http_client=http_client,
    )  # fmt: skip
    try:
        client.messages.create(
            model=settings.aida_model, max_tokens=16,
            messages=[{"role": "user", "content": "Reply with the word OK."}],
        )  # fmt: skip
    except anthropic.AuthenticationError:
        return Result("Claude", False, "ANTHROPIC_API_KEY was rejected. Make a new key at "
                      "console.anthropic.com and paste it in.")  # fmt: skip
    except anthropic.NotFoundError:
        return Result("Claude", False, f"There is no model called {settings.aida_model}. "
                      "Fix AIDA_MODEL.")  # fmt: skip
    except anthropic.PermissionDeniedError as exc:
        return Result("Claude", False, f"This key may not use {settings.aida_model}: {_said(exc)}")
    except anthropic.BadRequestError as exc:
        if "credit" in str(exc).lower() or "billing" in str(exc).lower():
            return Result("Claude", False, "The Anthropic account has no credit left. Add some "
                          "under Billing at console.anthropic.com.")  # fmt: skip
        return Result("Claude", False, f"Claude refused the request: {_said(exc)}")
    except anthropic.RateLimitError:
        return Result("Claude", False, "Too many requests just now, or a spending limit was "
                      "reached. Try again in a minute; if it persists, check "
                      "console.anthropic.com.")  # fmt: skip
    except anthropic.APIConnectionError:
        return Result("Claude", False, "Couldn't reach Anthropic. Is this computer online?")
    except anthropic.APIStatusError as exc:
        status = exc.status_code
        return Result("Claude", False, f"Anthropic answered HTTP {status}. Try again later.")
    finally:
        client.close()
    return Result("Claude", True, f"answers, with {settings.aida_model}")


def _said(exc: anthropic.APIStatusError) -> str:
    body = exc.body if isinstance(exc.body, dict) else {}
    return str((body.get("error") or {}).get("message") or exc.status_code)[:200]


def check_telegram(store: Settings, transport: httpx2.BaseTransport | None = None) -> Result:
    token = store.telegram_bot_token
    if not token:
        return Result("Telegram", False, "TELEGRAM_BOT_TOKEN is empty. Get one from @BotFather.")
    base = f"{store.telegram_api_url}/bot{token.get_secret_value()}"
    try:
        with httpx2.Client(timeout=15, transport=transport) as client:
            me = client.get(f"{base}/getMe")
            if me.status_code == 401:
                return Result("Telegram", False, "TELEGRAM_BOT_TOKEN was rejected. Copy it "
                              "again from @BotFather.")  # fmt: skip
            bot = me.json().get("result") or {}
            if not me.is_success or not bot:
                return Result("Telegram", False, f"Telegram answered HTTP {me.status_code}.")
            name = f"@{bot.get('username')}"
            if not store.admin_chat_id:
                return Result("Telegram", False, f"The bot {name} works, but ADMIN_CHAT_ID is "
                              "empty, so the admin gets no orders.")  # fmt: skip
            chat = client.get(f"{base}/getChat", params={"chat_id": store.admin_chat_id})
    except httpx2.HTTPError:
        return Result("Telegram", False, "Couldn't reach Telegram. Is this computer online?")
    if not chat.is_success:
        return Result("Telegram", False, f"The bot {name} works, but can't see the admin chat "
                      f"{store.admin_chat_id}. Message the bot from that chat (or add it to the "
                      "group), and check ADMIN_CHAT_ID.")  # fmt: skip
    return Result("Telegram", True, f"the bot {name} works and can reach the admin chat")


def check_gemini_live(settings: VoiceSettings) -> Result:
    """Open a Gemini Live session, as a call would, and have it say one word."""
    name = "Gemini Live"
    if settings.voice_engine != "gemini_live":
        return Result(name, None, "not used (VOICE_ENGINE isn't gemini_live)")
    if not settings.gemini_api_key:
        return Result(name, False, "GEMINI_API_KEY is empty. Make a key at aistudio.google.com.")
    key = settings.gemini_api_key.get_secret_value()
    url = (f"{settings.gemini_live_url.rstrip('/')}/ws/google.ai.generativelanguage.v1beta."
           f"GenerativeService.BidiGenerateContent?key={key}")  # fmt: skip
    setup = {"setup": {
        "model": f"models/{settings.gemini_live_model}",
        "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {
            "prebuiltVoiceConfig": {"voiceName": settings.gemini_voice}}}},
    }}  # fmt: skip
    hello = {"clientContent": {"turns": [{"role": "user", "parts": [{"text": "Say: Salom."}]}],
                               "turnComplete": True}}  # fmt: skip

    async def run() -> Result:
        audio = 0
        async with connect(url, open_timeout=10, max_size=None) as ws:
            await ws.send(json.dumps(setup))
            ready = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if "setupComplete" not in ready:
                return Result(name, False, f"Gemini didn't start a session: {str(ready)[:200]}")
            await ws.send(json.dumps(hello))
            async with asyncio.timeout(20):
                async for raw in ws:
                    content = json.loads(raw).get("serverContent") or {}
                    for part in (content.get("modelTurn") or {}).get("parts") or []:
                        audio += len((part.get("inlineData") or {}).get("data") or "")
                    if content.get("turnComplete"):
                        break
        if not audio:
            return Result(name, False, "Gemini answered without any audio.")
        return Result(name, True, f"speaks, with {settings.gemini_live_model} "
                      f"and the voice {settings.gemini_voice}")  # fmt: skip

    try:
        return asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - any failure is reported the same way
        reason = getattr(getattr(exc, "rcvd", None), "reason", "") or str(exc)
        problem = f"{type(exc).__name__}: {reason}".replace(key, "...")[:200]
        if "API key" in problem or "1007" in problem or "403" in problem:
            return Result(name, False, "Gemini rejected GEMINI_API_KEY, or it can't use the Live "
                          f"API. Make a new key at aistudio.google.com. ({problem})")  # fmt: skip
        if "not found" in problem.lower() or "1008" in problem:
            return Result(name, False, f"There is no Live model called "
                          f"{settings.gemini_live_model}, or this key can't use it. ({problem})")  # fmt: skip
        return Result(name, False, f"Gemini Live failed: {problem}")


def check_voicelab(settings: VoiceSettings) -> Result:
    if settings.voice_engine == "gemini_live":
        return Result("VoiceLab", None, "not used (VOICE_ENGINE is gemini_live)")
    if "voicelab" not in (settings.tts_provider, settings.stt_provider):
        return Result("VoiceLab", None, "not used (TTS_PROVIDER and STT_PROVIDER aren't voicelab)")
    if not settings.voicelab_api_key:
        return Result("VoiceLab", False, "VOICELAB_API_KEY is empty. Make a key in your account "
                      "at voicelab.uz/app.")  # fmt: skip

    async def run() -> Result:
        tts = VoiceLabTts(settings)
        try:
            audio = b"".join([chunk.audio async for chunk in tts.speak("Salom.")])
        except TtsError as exc:
            return Result("VoiceLab", False, _voicelab_problem("speaking", str(exc)))
        finally:
            await tts.close()
        base = settings.voicelab_url.replace("http", "ws", 1).rstrip("/")
        key = settings.voicelab_api_key.get_secret_value()
        start = {"type": "start", "language": settings.voice_language,
                 "audio_format": "pcm_s16le", "sample_rate": 16000, "channels": 1}  # fmt: skip
        try:
            async with connect(
                f"{base}/v1/stt/stream", additional_headers={"xvl-api-key": key}, open_timeout=10
            ) as ws:
                ready = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if ready.get("event") != "ready":
                    return Result("VoiceLab", False, _voicelab_problem("listening", str(ready)))
                await ws.send(json.dumps(start))
                pcm = phone_to_pcm16k(audio)
                for offset in range(0, len(pcm), 3200):
                    await ws.send(pcm[offset : offset + 3200])
                await ws.send(json.dumps({"type": "commit"}))
                heard = json.loads(await asyncio.wait_for(ws.recv(), 20))
        except Exception as exc:  # noqa: BLE001 - any failure is reported the same way
            problem = f"{type(exc).__name__}: {exc}"[:200]
            return Result("VoiceLab", False, _voicelab_problem("listening", problem))
        if heard.get("event") != "final":
            problem = f"{heard.get('code')}: {heard.get('message')}"[:200]
            return Result("VoiceLab", False, _voicelab_problem("listening", problem))
        return Result("VoiceLab", True, f"speaks, and heard its own \"Salom.\" as "
                      f"\"{heard.get('text', '')}\"")  # fmt: skip

    return asyncio.run(run())


def _voicelab_problem(doing: str, problem: str) -> str:
    if "insufficient_credits" in problem or "402" in problem:
        return "VoiceLab says the balance is empty. Top up at voicelab.uz/app."
    if "invalid_api_key" in problem or "401" in problem or "403" in problem:
        return "VoiceLab rejected VOICELAB_API_KEY. Make a new key at voicelab.uz/app."
    return f"VoiceLab failed while {doing}: {problem}"


def check_store(settings: VoiceSettings, transport: httpx2.BaseTransport | None = None) -> Result:
    key = settings.agent_api_key
    headers = {"X-API-Key": key.get_secret_value()} if key else {}
    try:
        with httpx2.Client(base_url=settings.backend_url, timeout=5, transport=transport) as client:
            response = client.get("/api/console", params={"limit": 1}, headers=headers)
    except httpx2.HTTPError:
        return Result("Store", False, f"Nothing answers at {settings.backend_url}. Start it with: "
                      "uv run uvicorn app.main:app --port 8000")  # fmt: skip
    if response.status_code == 401:
        return Result("Store", False, "The store wants a different AGENT_API_KEY. The chat, the "
                      "phone line and the store must share one .env.")  # fmt: skip
    if not response.is_success:
        return Result("Store", False, f"The store answered HTTP {response.status_code}.")
    return Result("Store", True, f"running at {settings.backend_url}")


def lines(results: dict[str, Result], voice: VoiceSettings) -> list[str]:
    """What can run, from what works."""
    works = {name for name, result in results.items() if result.ok}
    if works & {"Claude", "OpenAI", "Gemini"}:
        works.add("AI")
    if voice.voice_engine == "gemini_live":
        speech_ok, phone_needs = "Gemini Live" in works, {"Store"}
    else:
        speech_ok = not voice.missing_speech and (
            "voicelab" not in (voice.tts_provider, voice.stt_provider) or "VoiceLab" in works
        )
        phone_needs = {"AI", "Store"}
    ready = {
        "Telegram chat": {"AI", "Telegram", "Store"} <= works,
        "Phone over Bluetooth": phone_needs <= works and speech_ok,
        "Phone over Twilio": phone_needs <= works and speech_ok and not voice.missing,
    }
    out = [f"{name}: {'ready' if ok else 'not ready'}" for name, ok in ready.items()]
    if voice.missing_speech:
        out.append(f"The phone line still needs: {', '.join(voice.missing_speech)}")
    return out


def main() -> int:
    if not (BASE_DIR / ".env").exists():
        print("There is no .env file. Copy .env.example to .env and fill it in.")
        return 1
    store, voice = Settings(), VoiceSettings()
    print("Checking .env...\n")
    results = {}
    for check in (
        lambda: check_llm(voice),
        lambda: check_telegram(store),
        lambda: check_voicelab(voice),
        lambda: check_gemini_live(voice),
        lambda: check_store(voice),
    ):
        result = check()
        results[result.name] = result
        mark = {True: "OK  ", False: "FAIL", None: "--  "}[result.ok]
        print(f"  {mark}  {result.name}: {result.detail}")
    print()
    for line in lines(results, voice):
        print(f"  {line}")
    return 0 if all(r.ok is not False for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
