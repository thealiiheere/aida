# Aida

Aida is an AI sales operator for a demo store.

| Folder | What it is |
|---|---|
| `app/` | Store backend (FastAPI + SQLite): catalog, orders, stock holds, Click and Payme webhooks, call summaries, Telegram messages, and the store console |
| `chat/` | Telegram chat: answers customers who write to the bot |
| `voice/` | Phone line: answers calls, through Twilio or a phone joined over Bluetooth, and runs the live conversation |
| `agent/` | What chat and phone share: Aida's prompts, the tools, and the connection to Claude |

## Run it

Needs [uv](https://docs.astral.sh/uv/).

```powershell
uv run uvicorn app.main:app --port 8000
```

To see whether every key in `.env` works, and what can run with them:

```powershell
uv run python check.py
```

It sends each service one small request (Claude a single word, Telegram the bot's name, VoiceLab "Salom") and says in plain words what to fix. Nobody is messaged.

Store console: <http://localhost:8000/console>. API docs: <http://localhost:8000/docs>. The database (`aida.db`) is created and seeded with 8 products on first start. One from an earlier version is upgraded if it has no orders; otherwise the server says what to do and stops.

## Claude, OpenAI or Gemini

Claude answers by default. To have OpenAI answer instead, in the chat, on calls and in call summaries, set in `.env`:

```
LLM_PROVIDER=openai
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5-mini
```

Aida keeps the conversation in Claude's format and translates it to OpenAI's Chat Completions API and back, so nothing else changes. The prompts were written and tested with Claude; with OpenAI, try a few conversations before relying on it. `uv run python check.py` tests whichever key is in use.

Gemini works the same way, through Google's OpenAI-compatible API:

```
LLM_PROVIDER=gemini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-3.8-flash
```

## Store console

<http://localhost:8000/console> shows what Aida and the store are doing, and updates every 3 seconds.

| Page | What it shows |
|---|---|
| Overview | Orders by status, each order's 15-minute hold counting down, stock on the shelf, the newest chats and calls, and what needs a person |
| Orders | Every order with its items, payment links and Click or Payme payments |
| Chats | Each Telegram conversation as Aida had it: what each side said, her tool calls, the store's notes, and whether a person has taken over |
| Calls | Finished phone calls with Claude's summary and the transcript |
| Shelf | Stock per product: on the shelf, held in unpaid orders, and sold |

- **It only reads.** The one action is **Open checkout**, which opens an order's demo payment page, the same one the customer gets. It disappears once Click or Payme is switched on.
- **Chats come from `chat.db`,** the chat's own database (`CHAT_DATABASE` if set). Until the chat has run, the page says so.
- **Calls appear when they end.** Calls in progress are only in the phone line's memory.
- **With `AGENT_API_KEY` set,** the page asks for the key and keeps it in that browser tab only.
- The fonts come from Google Fonts. Offline, the page falls back to the system font.

```powershell
uv run pytest
```

Start over with a fresh catalog (**deletes all orders**):

```powershell
uv run python -m app.seed --reset
```

## Uzbek, Russian or English

`STORE_LANGUAGE` (en, uz or ru) sets the store's own language:

- **The admin's Telegram messages**: new orders, hand-overs, payments and call reports.
- **The store console** opens in it. Whoever is looking can switch between O‘zbekcha, Русский and English in the sidebar (on a phone, under the menu), and the choice is remembered in that browser.
- **The demo checkout page** opens in it, with UZ · RU · EN links at the top; `?lang=ru` on a payment link picks one.
- **Customers' Telegram messages** (payment link, payment received, link expired) go out in the language the customer last wrote to the bot in, and in `STORE_LANGUAGE` if they haven't written.

What stays as it is: product names and categories (they're the catalog's own data), and Aida's tool notes in the console's chat view. Aida herself answers in whatever language the customer uses; see "Languages" under the Telegram chat and phone calls.

## Orders and payments

| When | What happens |
|---|---|
| Order created | Its items leave `stock_quantity` and are marked `reserved` for 15 minutes |
| Paid in time | Items become `sold`, the order `paid`. The customer and the admin are told on Telegram |
| Not paid in 15 minutes | The order becomes `cancelled_expired`, its stock goes back, and the customer is told. Checked every 60 seconds |
| Paid after that | Accepted if the payment had started in time and the items are still there. Otherwise the gateway is told to return the money, and the admin is warned if the items have sold |
| Escalated | Never expires: the stock stays held until a person decides |

Stock is taken once, when it is reserved. Paying doesn't take it a second time.

### Click and Payme

The webhooks speak each gateway's own protocol. They answer 503 until these are set in `.env`:

| | Webhook | Settings |
|---|---|---|
| Click | `POST /api/webhooks/click`, for both Prepare and Complete | `CLICK_SERVICE_ID`, `CLICK_SECRET_KEY`, `UZS_PER_USD` |
| Payme | `POST /api/webhooks/payme` | `PAYME_KEY`, `UZS_PER_USD` |

- **Amounts:** prices are in dollars and the gateways charge soum, so a payment must match the dollar total times `UZS_PER_USD`, rounded to a whole soum.
- **Order number:** send it as `merchant_trans_id` (Click) or `account.order_id` (Payme).
- **Proof:** Click's MD5 signature, or Payme's Basic key. Anything else is refused.
- **Duplicates:** a repeated request gets the same answer (Click's Complete gets "already paid"). Nothing is charged, deducted or announced twice. A second payment for a paid order is refused, so the gateway returns it.
- **Refunds:** Payme can't cancel a paid order here. It is told the order is already being shipped.

**Not checked against the real gateways.** Payme follows its published Merchant API. Click's documentation site was unreachable, so Click follows the source of [its own Django library](https://github.com/click-llc/click-integration-django). Run both in their sandboxes first.

**Payment links still open the demo checkout page.** Real Click and Payme links aren't generated yet. Once either key is set, the demo page stops taking payments, because it would be a way to get things for free.

## Call summaries

`POST /api/telephony/call-ended` logs a finished call, has Claude summarise it, and sends the admin the result. The phone line calls it after every call.

```json
{"call_id": "CA123", "caller_telegram_id": "123456789", "raw_audio_transcript": "Caller: Hi...", "call_duration": 134}
```

- The transcript is saved before anything else, so a failure later doesn't lose it.
- **Outcome** comes from the store's records where they can tell. A transcript saying "order placed" isn't enough.
- Without `ANTHROPIC_API_KEY`, or if Claude fails, the admin gets the transcript instead.
- The same `call_id` is only reported once. If Telegram failed, sending it again retries.
- It needs `X-API-Key`. Twilio's and LiveKit's own webhooks don't send that or this format, so they need something in between, as the phone line does for Twilio.

## Rate limits

Five calls a minute per Telegram ID, counted separately for each endpoint that acts for a customer: orders, order lists, order status, invoices, admin notifications, escalations and call reports. One order uses three of them, so a shared limit would block ordinary customers. Catalog searches aren't limited, and phone calls are limited to five a minute per caller.

Limits are kept in memory, so they reset on restart.

## Telegram chat

Answers customers who write to the bot, with Claude and the store's tools, in the customer's language.

1. Follow [Turn on Telegram](#turn-on-telegram), and set `ANTHROPIC_API_KEY` in `.env`.
2. Start the backend, then the chat:

   ```powershell
   uv run python -m chat
   ```

3. Open the bot and press **Start**.

Aida doesn't serve customers in the admin's own chat. To try it from the admin's account, make a group the admin chat. The bot's privacy mode can stay on there.

| When | Aida |
|---|---|
| Customer presses Start | Greets them at once, without asking Claude |
| Several messages in a row | Waits for a one-second pause and answers them together |
| Customer confirms an order | Places it, sends the payment link in the same chat, and notifies the admin |
| Customer says they've paid | Checks the order before answering. Only the customer's own orders are visible |
| Order paid, expired, or placed by phone | Knows at the customer's next message |
| Photo, file or voice message | Says it can't be opened. Locations can be read |
| Customer asks for a person | Alerts the admin and goes quiet in that chat |
| Claude fails | Apologises. The second time in a row, hands over |
| Six hours of silence | Starts a new conversation, with the end of the last one as a note |
| The program restarts | Finishes answers less than 10 minutes old. An order isn't placed twice |

### Languages

Aida speaks English, Uzbek and Russian, and follows the customer:

- **Her answers** are in whatever language the customer writes or speaks.
- **The chat's fixed lines** (the greeting on **Start**, the apology when Claude can't answer, the hand-over notice) are in the language of the customer's last message. The greeting, sent before they've written anything, is in their Telegram app's language. `CHAT_LANGUAGE` (`en`, `uz` or `ru`) is used when neither says.
- **On the phone**, `VOICE_LANGUAGE` is the language she greets in. Once she answers in another of the three, the voice, her fillers and goodbyes, and how numbers are read switch with her.

Russian and Uzbek are told apart by script and by common words. A message with only a product name in it keeps the language already in use.

### Taking over from Aida

After a hand-over, everything the customer sends is forwarded to the admin chat.

- **Answer:** use Telegram's Reply on any forwarded message, or on the alert. The customer gets a copy, whatever it contains.
- **Give it back:** reply `/resume` to any of them. Aida then sees what was written by both sides. If the admin writes nothing for 24 hours, Aida comes back at the customer's next message.
- **Step in uninvited:** replying to an order notification works the same way, and Aida goes quiet.

Anyone in the admin group can do this.

### Limits

- **Not tried with the real Telegram or Claude.** The tests use simulations of both.
- **Text only.** Voice messages aren't transcribed, and edits to sent messages are ignored.
- **Some lines are always English:** the greeting, apologies, the hand-over line when Claude can't write one, and the store's own payment and expiry messages.
- **One copy at a time.** Telegram gives a bot's messages to one program. A second copy or a webhook makes this one miss messages, and the log says so.
- **Ten answers a minute per customer.** More wait their turn. The newest ten messages are read, up to 2,000 characters each.
- **Phone calls aren't remembered in chat,** apart from the orders placed on them.
- **Conversations are kept** in `chat.db`, unencrypted, and sent to Claude. Nothing deletes them.

## Phone calls

Audio from Twilio goes to Deepgram Flux (or ElevenLabs Scribe), the transcript to Claude, and each finished sentence to ElevenLabs Flash v2.5 and back to the caller, all streamed.

1. Copy `.env.example` to `.env` and fill in the phone section.
2. Start the backend, then the phone line:

   ```powershell
   uv run uvicorn voice.server:app --port 8001
   ```

3. Give port 8001 a public HTTPS address with a tunnel such as ngrok or cloudflared, and put it in `VOICE_PUBLIC_URL`. Restart the phone line.
4. In the Twilio console, set your number's **A call comes in** webhook to `VOICE_PUBLIC_URL` + `/voice/incoming`, method POST.
5. Link a customer's phone to their Telegram account. They must also have pressed **Start** in your bot, or Telegram won't deliver the payment link.

   ```powershell
   Invoke-RestMethod http://localhost:8000/api/customers -Method Put -ContentType application/json -Body '{"phone": "+998901234567", "telegram_id": "123456789"}'
   ```

`http://localhost:8001/health` says whether the line is ready. If it isn't, the log names the missing settings.

### With Gemini Live

Gemini Live hears the caller, thinks and speaks in one model, so it replaces speech to text, the AI and text to speech at once. In `.env`:

```
VOICE_ENGINE=gemini_live
GEMINI_API_KEY=...
```

No VoiceLab, Deepgram, ElevenLabs, Claude or OpenAI key is needed for calls. `GEMINI_VOICE` picks the voice (Kore by default) and `GEMINI_LIVE_MODEL` the model. Aida's phone prompt, the store tools, hand-overs, transcripts and call summaries work as before. How it differs from the pipeline:

- **Gemini decides when the caller has finished and when they cut in**, and it picks the language from what it hears (Uzbek, Russian or English). `VOICE_LANGUAGE` only sets the greeting and the fixed hand-over lines.
- **Prices and numbers are spoken by Gemini itself**, not turned into Uzbek words first.
- **The transcript is Gemini's own**, written down from the audio, so it can differ from what was really said.
- **Long calls are kept going**: Google moves a session after about ten minutes, and Aida reconnects and carries on where it left off.

`uv run python check.py` opens a Live session and has Gemini say a word. The Live API is still a preview at Google, and a whole call over the real service hasn't been tried yet.

### In Uzbek, with VoiceLab

[VoiceLab](https://voicelab.uz) both hears and speaks Uzbek (and Russian and English). In `.env`:

```
VOICE_LANGUAGE=uz
STT_PROVIDER=voicelab
TTS_PROVIDER=voicelab
VOICELAB_API_KEY=...
```

Then Aida greets, fills pauses and says goodbye in Uzbek, Claude is told to answer in Uzbek (or in Russian or English if the caller uses them), and prices and numbers are spoken as Uzbek words. Deepgram and ElevenLabs keys aren't needed.

The key needs the `tts:realtime` and `stt:realtime` permissions. `VOICELAB_VOICE` picks the voice (Gulnoza by default, who speaks all three languages). `uv run python check.py` has VoiceLab speak "Salom." and hear it back, which proves both halves work; a whole call over the real service hasn't been tried yet. How it differs from Deepgram and ElevenLabs:

- **The end of a phrase is found by loudness.** VoiceLab's realtime recognition writes down one finished phrase at a time and says nothing while someone talks, so Aida listens for a pause (`VOICELAB_PAUSE_MS`, 600 ms by default) and then sends the phrase. A noisy line can make that pause hard to find.
- **Cutting in takes half a second of talking.** Shorter sounds, like "ha" or "mhm", don't stop her; the words aren't known yet at that point, so a long "ha, ha, mayli" does.
- **Interruptions are placed by estimate.** There are no word timings, so the cut-off point is worked out from how much of the sentence's audio was heard.
- **Every transcript counts as understood.** VoiceLab gives no confidence score, so Aida never says she couldn't hear and never hands over for that reason.
- **Recognition listens for the call's current language.** It starts with `VOICE_LANGUAGE` and switches when Aida does. Russian heard as Uzbek comes back in Latin letters, which the AI usually still understands.
- **Twilio's own fallback lines stay in English.** Twilio's voice can't speak Uzbek. They are only used when the line isn't set up or breaks down.

### Over Bluetooth, on your own phone

Instead of a Twilio number, Aida can answer an ordinary mobile phone, with its SIM card, joined to a computer over Bluetooth the way a car's hands-free kit is. Customers call your normal local number.

It needs **Linux**: a Raspberry Pi 4 or 5, or an old laptop, near the phone and always on. Windows doesn't let programs answer a phone's calls. The phone can be Android or iPhone.

**The quick way.** Copy the project folder to the Pi (with your `.env`) and run:

```
bash deploy/setup-pi.sh
```

It installs everything below, and sets up the store, the Telegram chat and the phone line to start at boot and restart if they stop. At the end it prints how to pair the phone and check the keys. Logs: `journalctl --user -u aida-phone -f` (or `aida-store`, `aida-chat`).

**By hand:**

1. Install the Bluetooth hands-free stack. On Raspberry Pi OS or Debian:

   ```
   sudo apt install bluez ofono pulseaudio-module-bluetooth
   ```

   With PipeWire instead of PulseAudio, install `pipewire-pulse` and `libspa-0.2-bluetooth`, and set PipeWire to use oFono for hands-free calls.
2. Pair the phone: `bluetoothctl`, then `scan on`, `pair <address>`, `trust <address>`, `connect <address>`. On the phone, allow calls and contacts for this device.
3. Check that oFono sees it: `dbus-send --system --print-reply --dest=org.ofono / org.ofono.Manager.GetModems` should list a modem with `org.ofono.VoiceCallManager`. If the modem isn't powered or online, oFono's `enable-modem` and `online-modem` test scripts turn it on.
4. Put the store backend's address in `BACKEND_URL` (the store can run on another computer) and the speech settings in `.env`.
5. Start the line:

   ```
   uv run --group bluetooth python -m voice.bluetooth
   ```

When a call rings, Aida answers it, and the call is handled exactly like a Twilio one, with the same summaries and Telegram messages. `VOICE_PUBLIC_URL`, `TWILIO_AUTH_TOKEN` and a tunnel aren't needed.

Differences from Twilio:

- **One call at a time.** A second caller hears whatever the phone does while busy.
- **No transfer to `ADMIN_PHONE`.** A hand-over goes to the admin on Telegram, and Aida tells the caller they'll be contacted.
- **The phone must stay within Bluetooth range**, about 10 metres, and charged.
- **Bluetooth call audio is narrowband**, like an ordinary phone call.
- **Not tried on real hardware.** The bridge is tested with a simulated phone. The D-Bus calls follow oFono's documented API, and the audio uses PulseAudio's `parec` and `pacat`, but the first real call may need adjusting.

### What happens on a call

| When | Aida |
|---|---|
| Caller talks over Aida | Stops at once, and Claude's history is cut to the words that had played |
| Caller says "uh-huh" during an answer | Keeps going |
| Caller says "yes" over a question | Takes it as the answer |
| Interrupted while placing an order | Goes quiet, but the order still finishes and is remembered |
| Can't make out the caller | Asks again, then offers Telegram or the manager; the third time in a row, hands over |
| Caller goes quiet | Checks twice, then says goodbye |
| Caller asks for a person | Alerts the admin with the order, address, reason and last words, then transfers to `ADMIN_PHONE` if set |
| Caller says they've paid | Checks the order before answering. Only the caller's own orders are visible |
| Caller's phone isn't linked to Telegram | Takes the details and hands them to the admin, because an order needs a Telegram account |
| Claude, speech recognition or the voice fails | Twilio's own voice apologises, and the admin is alerted |
| Call ends | Sends the admin a summary. If there was an order, also the order and transcript |
| Sixth call in a minute from one caller | Gets a busy tone |

### Latency

The target is 800 ms from the end of the caller's turn to Aida's first sound. Each reply's time is logged, with a warning when it misses.

This server's own share measures about 60 ms in the tests. The rest is the providers, and **it hasn't been measured with real ones**. If it's too slow:

- `AIDA_FAST_MODE=true` (about twice the price) or `AIDA_MODEL=claude-haiku-5-5`.
- Lower `STT_CONFIDENCE`: turns end sooner, with more risk of cutting in.
- Prefer Deepgram over Scribe, which waits for half a second of silence.

### Limits

- **Twilio or Bluetooth only.** Telegram bots can't take voice calls, so Telegram calls would need a bridge from a user account. That isn't built.
- **English, Uzbek and Russian.** Uzbek needs VoiceLab. Deepgram Flux only hears English, so a line that should switch languages needs VoiceLab (or ElevenLabs Scribe) for speech recognition. VoiceLab's realtime recognition hears one language per call, the one in `VOICE_LANGUAGE`; how well it hears a caller who switches hasn't been tried.
- **One process.** Live calls are kept in memory, so run a single worker.
- **Caller ID can be faked.** A spoofed number could place an unpaid order for someone else's account. The payment link still only goes to the linked Telegram chat.
- **Callers are transcribed.** Every transcript is stored in the database and sent to Claude to be summarised. Transcripts of calls with an order also go to the admin's Telegram. Check whether you must tell callers.

## Turn on Telegram

Until this is done, admin messages are dry runs: logged and returned with `"delivered": false`.

1. Copy `.env.example` to `.env`.
2. Create a bot with [@BotFather](https://t.me/BotFather) and put its token in `TELEGRAM_BOT_TOKEN`.
3. As the admin, open the bot and press **Start**. For a group, add the bot to it.
4. Send a message, open `https://api.telegram.org/bot<TOKEN>/getUpdates`, and copy `chat.id` into `ADMIN_CHAT_ID`. Do this while the chat isn't running, because only one of them gets the message. In a group, send `/id@your_bot`, because the bot doesn't see ordinary messages there.
5. Set `ADMIN_USERNAME` so escalations tag the admin. This is required if the chat is a group.
6. Restart the server, and the chat if it's running.

## Endpoints and tools

| Aida's tool | Endpoint |
|---|---|
| `search_catalog` | `GET /api/products/search?q=&category=` |
| `check_stock` | `GET /api/products/{id}/stock?variant=` |
| `create_order` | `POST /api/orders` |
| `generate_invoice` | `POST /api/payments/create-invoice` |
| `get_order_status` | `GET /api/orders/{id}/status?customer_telegram_id=` |
| `notify_admin_order` | `POST /api/admin/notify-order` |
| `escalate_to_human` | `POST /api/admin/escalate` |
| (none) | `GET /api/orders/{id}`: order and payment status |
| (none) | `GET /api/customers/{telegram_id}/orders`: a customer's five newest orders |
| (none) | `PUT /api/customers`: link a phone number to a Telegram account |
| (none) | `POST /api/customers/lookup`: find who is calling |
| (none) | `POST /api/admin/notify-order` with `call`: report on a finished call |
| (none) | `POST /api/telephony/call-ended`: log and summarise a finished call |
| (none) | `POST /api/webhooks/click`, `POST /api/webhooks/payme`: payment confirmations |
| (none) | `GET /pay/{invoice_id}`: demo checkout page that marks the order paid |
| (none) | `GET /console`: the store console. `/` opens it |
| (none) | `GET /api/console`, `GET /api/console/chats/{chat_id}`, `GET /api/console/calls/{call_id}`: what the console shows |

Errors look like `{"detail": {"code": "insufficient_stock", "message": "..."}}`.

## Connect another channel

The chat and the phone line do this already:

```python
from agent.prompts import chat_prompt
from agent.tools import StoreTools

prompt = chat_prompt(store_name="Aida Demo Store", admin_name="our store manager")
tools = StoreTools(
    api_key="...",                          # AGENT_API_KEY, if set
    customer_telegram_id="123456789",       # from the Telegram session
    customer_telegram_username="dilnoza_k",
    live=True,                              # don't ask the model who the customer is
)
tools.definitions                            # tool schemas for the model
tools.customer_said("Do you have mugs?")     # each message, for hand-overs
tools.call("search_catalog", {"query": "mug"})
```

Make one `StoreTools` per chat. The customer id you pass overrides whatever the model supplies, so a caller can't order as someone else. Failures come back as `{"ok": false, "error": {...}}`.

Stop the AI when `escalate_to_human` returns `"pause_ai": true`. The store sends the payment link to the customer's Telegram chat itself if `live` is set. Otherwise send `payment_url` from `generate_invoice`.

## Before going beyond a demo

- **Demo checkout is fake.** Until a Click or Payme key is set, anyone with a link can mark that order paid.
- **Telegram messages aren't queued.** If Telegram is down when a payment or expiry happens, that message is only logged. The order itself is still right.
- **Stock is per product,** not per size or color. The chosen variant is saved on the order as text.
- **Set `AGENT_API_KEY`** before exposing the server, and set `PAYMENT_BASE_URL` to a public address so customers can open links on their phones.
