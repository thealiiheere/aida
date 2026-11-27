"""One phone call: what the caller says, what Aida says back, and who is talking.

Four jobs run for the length of a call: reading from Twilio, streaming to
speech-to-text, handling what the caller says, and watching for silence. Aida's side
is one Turn at a time, with one task thinking and another speaking. Only the job that
handles caller events starts or interrupts turns, so those never race each other.
"""

import asyncio
import base64
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, suppress
from dataclasses import dataclass

from agent.history import Conversation, tool_uses
from agent.language import language_of
from agent.llm import Claude, LlmError, Reply
from agent.prompts import voice_prompt
from agent.tools import StoreTools, no_manager, run_tool, tool_result

from .config import VoiceSettings
from .playback import Playback, Sentence
from .speech import SentenceChunker, clean_transcript, is_backchannel, speakable
from .stt import Speech, SpeechToText, Utterance
from .tts import AudioChunk, TextToSpeech, TtsError

log = logging.getLogger("aida.voice")

GREETING = "Hello! Welcome to {store}. I'm Aida, your AI assistant. How can I help you today?"
FILLERS = ("One moment.", "Let me check.", "Just a second.")
HICCUP = "Sorry, I had a small hiccup. Could you say that once more?"
REFUSED = "I'm sorry, I can't help with that. What can I find for you in our store?"
# Handing over, depending on how the manager can reach the caller. The first is for when
# the line can be transferred but the manager's Telegram could not be reached.
TRANSFER_ONLY = "Let me connect you directly with {admin}. One moment please."
TRANSFER = "I've passed your order notes to {admin}. Connecting you right now, one moment please."
STEPPING_IN = (
    "I've passed your order notes to {admin}. "
    "They are stepping into our Telegram chat right now. Goodbye!"
)
CALLBACK = "I've passed your order notes to {admin}. They will get back to you. Goodbye!"
PASSED_ON = "I've passed your order notes to {admin}. Goodbye!"
SILENT = "I can't hear anyone, so I'll hang up now. Please call again any time. Goodbye!"

@dataclass(frozen=True)
class Lines:
    """What Aida says without asking Claude, in one language."""

    greeting: str
    fillers: tuple[str, ...]
    hiccup: str
    refused: str
    transfer_only: str
    transfer: str
    stepping_in: str
    callback: str
    passed_on: str
    silent: str


ENGLISH = Lines(
    GREETING, FILLERS, HICCUP, REFUSED, TRANSFER_ONLY, TRANSFER, STEPPING_IN, CALLBACK,
    PASSED_ON, SILENT,
)  # fmt: skip
_NOTES_PASSED = "Buyurtma ma'lumotlaringizni {admin}ga yubordim."
UZBEK = Lines(
    greeting=(
        "Assalomu alaykum! {store} do'koniga xush kelibsiz. Men Aida, sun'iy intellekt "
        "yordamchisiman. Sizga qanday yordam bera olaman?"
    ),
    fillers=("Bir daqiqa.", "Hozir tekshiraman.", "Bir soniya."),
    hiccup="Kechirasiz, kichik nosozlik bo'ldi. Iltimos, yana bir bor aytib bera olasizmi?",
    refused="Kechirasiz, bunga yordam bera olmayman. Do'konimizdan sizga nima topib beray?",
    transfer_only="Sizni hozir {admin} bilan bog'layman. Bir daqiqa kuting.",
    transfer=f"{_NOTES_PASSED} Hozir sizni ulayman, bir daqiqa kuting.",
    stepping_in=f"{_NOTES_PASSED} U hozir Telegram chatimizda sizga yozadi. Xayr!",
    callback=f"{_NOTES_PASSED} U siz bilan bog'lanadi. Xayr!",
    passed_on=f"{_NOTES_PASSED} Xayr!",
    silent=(
        "Sizni eshitmayapman, shuning uchun qo'ng'iroqni tugataman. "
        "Istalgan vaqtda qayta qo'ng'iroq qiling. Xayr!"
    ),
)
_NOTES_PASSED_RU = "Я передала данные вашего заказа: {admin}."
RUSSIAN = Lines(
    greeting=(
        "Здравствуйте! Добро пожаловать в {store}. Я Аида, ассистент с искусственным "
        "интеллектом. Чем могу помочь?"
    ),
    fillers=("Одну минуту.", "Сейчас проверю.", "Секунду."),
    hiccup="Извините, произошёл небольшой сбой. Повторите, пожалуйста, ещё раз.",
    refused="Извините, с этим я помочь не могу. Что подобрать для вас в нашем магазине?",
    transfer_only="Сейчас соединю вас: {admin}. Одну минуту.",
    transfer=f"{_NOTES_PASSED_RU} Соединяю вас, одну минуту.",
    stepping_in=f"{_NOTES_PASSED_RU} Вам сейчас напишут в нашем Telegram-чате. До свидания!",
    callback=f"{_NOTES_PASSED_RU} С вами свяжутся. До свидания!",
    passed_on=f"{_NOTES_PASSED_RU} До свидания!",
    silent="Вас не слышно, поэтому я завершаю звонок. Звоните в любое время. До свидания!",
)
LINES = {"en": ENGLISH, "uz": UZBEK, "ru": RUSSIAN}


UNCLEAR_NOTE = "The caller's last words could not be understood. This is attempt {attempt}."
UNCLEAR_REASON = "Repeated low transcription confidence"
SILENT_NOTE = "The caller has gone quiet. Check whether they are still there."
NO_MANAGER_NOTE = (
    "The caller could not be understood three times and no manager could be reached. "
    "Apologise and ask them to call again from somewhere quieter."
)

FRAME_BYTES = 800  # 100 ms
MAX_TOOL_ROUNDS = 8
UNCLEAR_LIMIT = 3
MAX_SILENT_PROMPTS = 2
# How long after the audio should have ended to keep waiting for Twilio to say so.
MARK_GRACE_SECONDS = 2


@dataclass(frozen=True)
class Caller:
    phone: str | None = None
    telegram_id: str | None = None
    telegram_username: str | None = None
    name: str | None = None


class Silence:
    """Queued when neither side has spoken for a while."""


class Turn:
    """One stretch of Aida talking, with the thinking and tool calls behind it."""

    def __init__(self, number: int, start: int, clock: Callable[[], float], asked_at: float | None):
        self.number = number
        self.start = start  # where its messages begin in the history
        self.asked_at = asked_at  # when the caller finished talking
        self.first_text_at: float | None = None
        self.playback = Playback(clock)
        self.queue: asyncio.Queue[Sentence | None] = asyncio.Queue()
        self.messages = 0  # assistant messages completed
        self.partial = ""  # sentences released from the message still streaming
        self.queued = 0
        self.thought_through = False
        self.using_tools = False
        self.interrupted = False
        self.heard: list[tuple[Sentence, str]] = []
        self.played_through = -1
        self.marked = asyncio.Event()
        self.task: asyncio.Task
        self.speaker: asyncio.Task

    @property
    def fully_heard(self) -> bool:
        return (
            self.thought_through
            and len(self.heard) == self.queued
            and all(text == sentence.raw for sentence, text in self.heard)
        )


class CallSession:
    def __init__(
        self,
        *,
        settings: VoiceSettings,
        stt: SpeechToText,
        tts: TextToSpeech,
        llm: Claude,
        tools: StoreTools,
        caller: Caller,
        call_sid: str,
        stream_sid: str,
        send: Callable[[dict], Awaitable[None]],
        clock: Callable[[], float] = time.monotonic,
    ):
        self._settings = settings
        # Follows the caller: whichever language Aida last answered in.
        self.language = settings.voice_language
        self._stt = stt
        self._tts = tts
        self._llm = llm
        self._tools = tools
        self._caller = caller
        self._send = send
        self._clock = clock
        self.call_sid = call_sid
        self.stream_sid = stream_sid

        self.conversation = Conversation()
        self.agent_speaking = False
        self.outcome = "completed"
        self.problem: str | None = None
        self.order_ids: list[int] = []
        self.transcript: list[dict] = []
        self.latencies_ms: list[int] = []
        self.duration_seconds = 0

        self._definitions = tools.definitions
        self._system = [
            {
                "type": "text",
                "text": voice_prompt(
                    settings.store_name, settings.admin_name, settings.voice_language
                ),
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": _call_context(caller)},
        ]
        self._events: asyncio.Queue[Speech | Utterance | Silence] = asyncio.Queue()
        self._ended = asyncio.Event()
        self._turn: Turn | None = None
        self._turns = 0
        self._closing: str | None = None  # Aida's last line; the caller is no longer handled
        self._started = self._quiet_since = clock()
        self._silent_prompts = 0
        self._unclear = 0
        self._llm_failures = 0

    # --- The call ---------------------------------------------------------------

    async def run(self, incoming: AsyncIterator[dict]) -> None:
        """Handle the call until the caller hangs up or Aida ends it."""
        self.conversation.add_note("The caller has just connected.")
        self._start_turn(say=self._lines_now.greeting.format(store=self._settings.store_name))
        jobs = [
            asyncio.create_task(job)
            for job in (
                self._read(incoming),
                self._stt.run(self._events.put_nowait),
                self._handle_events(),
                self._watch_silence(),
                self._ended.wait(),
            )
        ]
        warming = asyncio.create_task(self._llm.warm(self._system, self._definitions))
        try:
            finished, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
            for job in finished:
                if exc := job.exception():
                    log.error("call %s failed", self.call_sid, exc_info=exc)
                    self._give_up(f"{type(exc).__name__}: {exc}")
        finally:
            for job in (*jobs, warming):
                job.cancel()
            await asyncio.gather(*jobs, warming, return_exceptions=True)
            # A tool still running, such as an order, is allowed to finish.
            await self._interrupt()
            self.duration_seconds = int(self._clock() - self._started)
            if self.problem and self.outcome != "escalated":
                self.outcome = "failed"

    def report(self) -> dict:
        """The call, as POST /api/admin/notify-order expects it."""
        return {
            "call_id": self.call_sid,
            "duration_seconds": self.duration_seconds,
            "outcome": self.outcome,
            "order_ids": self.order_ids[-20:],
            "customer_phone": self._caller.phone,
            "customer_telegram_id": self._caller.telegram_id,
            "customer_telegram_username": self._caller.telegram_username,
            "customer_name": self._caller.name,
            "transcript": self.transcript[-2000:],
        }

    def ended(self) -> dict:
        """The call, as POST /api/telephony/call-ended expects it."""
        speakers = {"caller": "Caller", "aida": "Aida"}
        return {
            "call_id": self.call_sid,
            "caller_telegram_id": self._caller.telegram_id,
            "caller_phone": self._caller.phone,
            "raw_audio_transcript": "\n".join(
                f"{speakers[line['speaker']]}: {line['text']}" for line in self.transcript
            )[-200_000:],
            "call_duration": self.duration_seconds,
            "order_ids": self.order_ids[-20:],
            # A call that broke down was passed to the admin as well.
            "escalated": self.outcome in ("escalated", "failed"),
        }

    async def alert_problem(self) -> None:
        """Tag the admin after a call that broke down."""
        lines = [f"{line['speaker']}: {line['text']}" for line in self.transcript[-12:]]
        await self._call_tool(
            "escalate_to_human",
            {
                "reason": f"The call failed. {self.problem}"[:300],
                "context_summary": " / ".join(lines)[-3000:] or "Nothing had been said yet.",
            },
        )

    def _give_up(self, problem: str) -> None:
        if self.problem is None:
            log.error("call %s cannot continue: %s", self.call_sid, problem)
            self.problem = problem
        self._ended.set()

    async def _read(self, incoming: AsyncIterator[dict]) -> None:
        async for message in incoming:
            try:
                match message.get("event"):
                    case "media":
                        if message["media"].get("track", "inbound") == "inbound":
                            self._stt.feed(base64.b64decode(message["media"]["payload"]))
                    case "mark":
                        self._on_mark(message["mark"]["name"])
                    case "stop":
                        return
            except (KeyError, TypeError, AttributeError, ValueError):
                log.warning("ignored a malformed %s message", message.get("event"))

    async def _watch_silence(self) -> None:
        limit = self._settings.silence_seconds
        while True:
            await asyncio.sleep(limit / 8)
            if self._turn is None and self._clock() - self._quiet_since >= limit:
                self._quiet_since = self._clock()
                self._events.put_nowait(Silence())

    # --- The caller -------------------------------------------------------------

    async def _handle_events(self) -> None:
        while True:
            event = await self._events.get()
            if self._closing:
                continue
            if isinstance(event, Speech):
                await self._on_speech(clean_transcript(event.text))
            elif isinstance(event, Utterance):
                await self._on_utterance(clean_transcript(event.text), event.confidence)
            else:
                self._on_silence()

    async def _on_speech(self, text: str) -> None:
        """The caller has started talking. If Aida was too, stop."""
        if not text:
            return
        self._quiet_since = self._clock()
        if not is_backchannel(text):
            await self._interrupt()
            self._close_if_due()

    async def _on_utterance(self, text: str, confidence: float) -> None:
        """The caller has finished talking."""
        if not text:
            return
        asked_at = self._quiet_since = self._clock()
        # "Yeah" over a long answer means "go on". Over a question, it's the reply.
        if self._turn and is_backchannel(text) and not self._turn.playback.asked_question:
            log.debug("ignored a listening noise")
            return
        await self._interrupt()
        if self._close_if_due():
            return
        self._silent_prompts = 0
        self.transcript.append({"speaker": "caller", "text": text})
        self._tools.customer_said(text)
        if confidence >= self._settings.unclear_below:
            self._unclear = 0
            self.conversation.add_customer(text)
        else:
            self._unclear += 1
            if self._unclear < UNCLEAR_LIMIT:
                self.conversation.add_note(UNCLEAR_NOTE.format(attempt=self._unclear))
            elif await self._escalate_unclear():
                self._start_turn(say=self._closing)
                return
        self._start_turn(asked_at=asked_at)

    def _on_silence(self) -> None:
        if self._turn:
            return
        if self._silent_prompts < MAX_SILENT_PROMPTS:
            self._silent_prompts += 1
            self.conversation.add_note(SILENT_NOTE)
            self._start_turn()
        else:
            self.outcome = "silent"
            self._closing = self._lines_now.silent
            self._start_turn(say=self._closing)

    async def _escalate_unclear(self) -> bool:
        result = await self._call_tool(
            "escalate_to_human",
            {
                "reason": UNCLEAR_REASON,
                "context_summary": " / ".join(
                    f"{line['speaker']}: {line['text']}" for line in self.transcript[-12:]
                ),
            },
        )
        if not self._hand_off(result):
            self.conversation.add_note(NO_MANAGER_NOTE)
            return False
        return True

    async def _interrupt(self) -> None:
        """Stop Aida, and make the history match what the caller heard.

        Returns once the turn is fully settled. Safe to call again if cancelled.
        """
        turn = self._turn
        if turn is None:
            return
        if not turn.interrupted:
            turn.heard = turn.playback.heard()
            turn.interrupted = True
            self.agent_speaking = False
            turn.speaker.cancel()  # before the clear, so no audio can follow it
            if not turn.using_tools:
                turn.task.cancel()
            if turn.playback.started:
                await self._send({"event": "clear", "streamSid": self.stream_sid})
        await asyncio.wait([turn.task, turn.speaker])
        if self._turn is turn:
            self._settle(turn)

    def _settle(self, turn: Turn) -> None:
        self._turn = None
        self._quiet_since = self._clock()
        said = " ".join(text.strip() for _, text in turn.heard)
        if turn.fully_heard:
            self._record(said)
            return
        if turn.partial:
            self.conversation.add_assistant([{"type": "text", "text": turn.partial}])
        heard: dict[int, str] = {}
        for sentence, text in turn.heard:
            if sentence.message is not None:
                heard[sentence.message] = f"{heard.get(sentence.message, '')} {text.strip()}".strip()
        self.conversation.cut_turn(turn.start, heard)
        self._record(f"{said} —" if said else "")
        log.info(
            "turn %d interrupted, utteranceUntilInterrupt is %d characters", turn.number, len(said)
        )

    def _close_if_due(self) -> bool:
        """After an interruption that let a successful hand-off finish, say so and go."""
        if self._closing and self._turn is None:
            self._start_turn(say=self._closing)
        return self._closing is not None

    # --- Aida -------------------------------------------------------------------

    def _start_turn(self, say: str | None = None, asked_at: float | None = None) -> None:
        """Speak a fixed line, or answer the conversation so far."""
        self._turns += 1
        turn = Turn(self._turns, len(self.conversation.messages), self._clock, asked_at)
        self._turn = turn
        turn.speaker = asyncio.create_task(self._speak(turn))
        turn.task = asyncio.create_task(self._run_turn(turn, say))

    async def _run_turn(self, turn: Turn, say: str | None) -> None:
        try:
            if say:
                self._say(turn, say)
            else:
                await self._think(turn)
                if turn.interrupted:
                    return
                if self._closing:
                    self._say(turn, self._closing)
            turn.thought_through = True
            turn.queue.put_nowait(None)
            await turn.speaker
            await self._drain(turn)
        except Exception as exc:
            log.exception("turn %d failed", turn.number)
            self._give_up(f"Unexpected {type(exc).__name__}.")
            return
        if not turn.interrupted and not self.problem:
            self._finish(turn)

    def _finish(self, turn: Turn) -> None:
        self._turn = None
        self.agent_speaking = False
        self._quiet_since = self._clock()
        self._record(" ".join(sentence.raw.strip() for sentence in turn.playback.sentences))
        if self._closing:
            self._ended.set()

    async def _think(self, turn: Turn) -> None:
        for _ in range(MAX_TOOL_ROUNDS):
            reply = await self._ask(turn)
            calls = tool_uses(reply.content) if reply and reply.stop_reason == "tool_use" else []
            if not calls:
                return
            # From here until the results are stored, the turn can't be cancelled: an
            # order must never be placed without the conversation knowing about it.
            turn.using_tools = True
            if not turn.queued:
                fillers = self._lines_now.fillers
                self._enqueue(turn, fillers[turn.number % len(fillers)], None)
            results = await asyncio.gather(*(self._use_tool(call) for call in calls))
            turn.using_tools = False
            self.conversation.add_tool_results(list(results))
            if turn.interrupted or self._closing:
                return
        self._say(turn, self._lines_now.hiccup)

    async def _ask(self, turn: Turn) -> Reply | None:
        chunker = SentenceChunker()

        def on_text(text: str) -> None:
            if turn.first_text_at is None:
                turn.first_text_at = self._clock()
            for sentence in chunker.feed(text):
                self._release(turn, sentence)

        try:
            reply = await self._llm.reply(
                self._system, self._definitions, self.conversation, on_text
            )
        except LlmError as exc:
            log.warning("no reply from Claude: %s", exc)
            self._llm_failures += 1
            if self._llm_failures > 1:
                self._give_up("Claude is not answering.")
            else:
                self._say(turn, self._lines_now.hiccup)
            return None
        self._llm_failures = 0
        if reply.stop_reason == "refusal":
            self._say(turn, self._lines_now.refused)
            return None
        for sentence in chunker.flush():
            self._release(turn, sentence)
        turn.partial = ""
        content = reply.content
        if reply.stop_reason != "tool_use":
            # Cut off mid-call, so its input can't be trusted.
            content = [block for block in content if block["type"] != "tool_use"]
        if self.conversation.add_assistant(content):
            turn.messages += 1
        else:
            self._say(turn, self._lines_now.hiccup)
        return reply

    def _release(self, turn: Turn, text: str) -> None:
        turn.partial += text
        self._enqueue(turn, text, turn.messages)

    def _say(self, turn: Turn, line: str) -> None:
        """Speak a fixed line and remember it as something Aida said."""
        text = f"{turn.partial} {line}".strip()
        turn.partial = ""
        self.conversation.add_assistant([{"type": "text", "text": text}])
        chunker = SentenceChunker()
        for sentence in chunker.feed(line) + chunker.flush():
            self._enqueue(turn, sentence, turn.messages)
        turn.messages += 1

    @property
    def _lines_now(self) -> Lines:
        return LINES[self.language]

    def _enqueue(self, turn: Turn, text: str, message: int | None) -> None:
        language = self.language
        if message is not None:  # fillers are too short to tell
            language = language_of(text, self.language)
            if language != self.language:
                self.language = language
                self._stt.set_language(language)
        if spoken := speakable(text, language):
            turn.queued += 1
            sentence = Sentence(raw=text, spoken=spoken, message=message, language=language)
            turn.queue.put_nowait(sentence)

    def _record(self, text: str) -> None:
        if text := text.strip():
            self.transcript.append({"speaker": "aida", "text": text})

    # --- Tools ------------------------------------------------------------------

    async def _use_tool(self, call: dict) -> dict:
        result = await self._call_tool(call["name"], call["input"])
        if call["name"] == "escalate_to_human" and not self._hand_off(result):
            result = no_manager()
        return tool_result(call, result)

    async def _call_tool(self, name: str, arguments: dict) -> dict:
        result = await run_tool(self._tools, name, arguments)
        if name == "create_order" and result["ok"]:
            self.order_ids.append(result["data"]["order_id"])
        return result

    def _hand_off(self, result: dict) -> bool:
        """Start ending the call in favour of a person. False if none can be reached."""
        alerted = result["ok"] and result["data"].get("delivered")
        if self._settings.admin_phone:
            line = self._lines_now.transfer if alerted else self._lines_now.transfer_only
        elif not alerted:
            return False
        elif self._caller.telegram_id:
            line = self._lines_now.stepping_in
        else:
            line = self._lines_now.callback if self._caller.phone else self._lines_now.passed_on
        self._closing = line.format(admin=self._settings.admin_name)
        self.outcome = "escalated"
        return True

    # --- Audio out --------------------------------------------------------------

    async def _speak(self, turn: Turn) -> None:
        said = ""
        try:
            while (sentence := await turn.queue.get()) is not None:
                index = turn.playback.add(sentence)
                speech = self._tts.speak(sentence.spoken, said, language=sentence.language)
                async with aclosing(speech) as chunks:
                    async for chunk in chunks:
                        await self._send_audio(turn, sentence, chunk)
                sentence.complete = True
                mark = {"name": f"{turn.number}:{index}"}
                await self._send(
                    {"event": "mark", "streamSid": self.stream_sid, "mark": mark}
                )
                said = f"{said} {sentence.spoken}"[-300:].strip()
        except TtsError as exc:
            self._give_up(f"Text-to-speech failed. {exc}")

    async def _send_audio(self, turn: Turn, sentence: Sentence, chunk: AudioChunk) -> None:
        sentence.char_ends_ms += chunk.char_ends_ms
        for start in range(0, len(chunk.audio), FRAME_BYTES):
            frame = chunk.audio[start : start + FRAME_BYTES]
            if not turn.playback.started:
                self._on_first_audio(turn)
            turn.playback.sent(sentence, len(frame))
            await self._send(
                {
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": base64.b64encode(frame).decode()},
                }
            )

    def _on_first_audio(self, turn: Turn) -> None:
        self.agent_speaking = True
        if turn.asked_at is None:
            return
        latency = round((self._clock() - turn.asked_at) * 1000)
        self.latencies_ms.append(latency)
        if turn.first_text_at is None:
            part = "a filler while tools run"
        else:
            part = f"Claude's first words took {(turn.first_text_at - turn.asked_at) * 1000:.0f} ms"
        target = self._settings.target_latency_ms
        log.log(
            logging.INFO if latency <= target else logging.WARNING,
            "turn %d: first audio %d ms after the transcript (%s; target %d ms)",
            turn.number,
            latency,
            part,
            target,
        )

    def _on_mark(self, name: str) -> None:
        turn = self._turn
        number, _, index = name.partition(":")
        # After a clear, Twilio returns the marks it dropped. They name an interrupted
        # turn, whose heard text is already settled.
        if turn is None or number != str(turn.number):
            return
        if index.isdecimal() and int(index) < len(turn.playback.sentences):
            turn.playback.played(int(index))
            turn.played_through = max(turn.played_through, int(index))
            turn.marked.set()

    async def _drain(self, turn: Turn) -> None:
        """Wait until the caller has heard everything."""
        last = len(turn.playback.sentences) - 1
        with suppress(TimeoutError):
            async with asyncio.timeout(turn.playback.remaining_ms / 1000 + MARK_GRACE_SECONDS):
                while turn.played_through < last:
                    turn.marked.clear()
                    await turn.marked.wait()


def _call_context(caller: Caller) -> str:
    if not caller.telegram_id:
        return (
            "# This call\n\n"
            "No Telegram account is linked to this caller, so you can't place an order, send a "
            "payment link, look up a payment or ask them to type. Help them choose, take their name, the items and the delivery "
            "address, then use escalate_to_human with all of it in context_summary."
        )
    name = clean_transcript(caller.name or "")
    return "# This call\n\nThe caller's Telegram account is linked, so you can place orders." + (
        f" Their name on file is {name}; check it with them before ordering." if name else ""
    )
