"""Works out what the caller has heard, so an interruption can be cut at the right word.

Audio is sent far faster than it plays and waits in Twilio's buffer, so "sent" is not
"heard". Twilio's mark messages say when a sentence finished playing; between marks,
the position comes from the clock.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

BYTES_PER_MS = 8  # 8 kHz, one byte per sample
TYPICAL_CHARS_PER_MS = 15 / 1000


@dataclass
class Sentence:
    raw: str  # as the model wrote it
    spoken: str  # as sent to text-to-speech
    message: int | None = None  # which of the turn's assistant messages; None for fillers
    language: str | None = None  # for text-to-speech; None means the line's own
    audio_ms: float = 0
    char_ends_ms: list[float] = field(default_factory=list)
    complete: bool = False

    def heard_after(self, ms: float) -> str:
        """The whole words spoken in the first `ms` milliseconds."""
        if self.complete and ms >= self.audio_ms - 1:
            return self.raw
        if self.char_ends_ms:
            count = sum(end <= ms for end in self.char_ends_ms)
        elif self.complete and self.audio_ms:
            count = int(len(self.spoken) * ms / self.audio_ms)
        else:
            # Still being synthesized and no timings: assume a normal speaking pace.
            count = int(ms * TYPICAL_CHARS_PER_MS)
        if count >= len(self.spoken):
            return self.raw
        text = self.spoken[:count]
        if text and text[-1].isalnum() and self.spoken[count].isalnum():
            text = text[: text.rfind(" ") + 1]
        return text.rstrip()


class Playback:
    def __init__(self, clock: Callable[[], float]):
        self._clock = clock
        self.sentences: list[Sentence] = []
        self._sent_ms = 0.0
        # At time _base_at (seconds) the caller was _base_ms into the audio.
        self._base_ms = 0.0
        self._base_at: float | None = None

    @property
    def started(self) -> bool:
        return self._base_at is not None

    @property
    def position_ms(self) -> float:
        if self._base_at is None:
            return 0.0
        elapsed = (self._clock() - self._base_at) * 1000
        return min(self._sent_ms, self._base_ms + elapsed)

    @property
    def remaining_ms(self) -> float:
        return self._sent_ms - self.position_ms

    def add(self, sentence: Sentence) -> int:
        self.sentences.append(sentence)
        return len(self.sentences) - 1

    def sent(self, sentence: Sentence, audio_bytes: int) -> None:
        now = self._clock()
        if self._base_at is None or self.remaining_ms <= 0:
            # First audio, or the caller ran out and has been hearing silence.
            self._base_ms, self._base_at = self._sent_ms, now
        duration = audio_bytes / BYTES_PER_MS
        self._sent_ms += duration
        sentence.audio_ms += duration

    def played(self, index: int) -> None:
        """Twilio says this sentence has finished playing."""
        end = sum(s.audio_ms for s in self.sentences[: index + 1])
        self._base_ms, self._base_at = min(end, self._sent_ms), self._clock()

    def heard(self) -> list[tuple[Sentence, str]]:
        """Each sentence the caller has heard any of, with the part they heard."""
        position, start, result = self.position_ms, 0.0, []
        for sentence in self.sentences:
            if position <= start:
                break
            if text := sentence.heard_after(position - start):
                result.append((sentence, text))
            start += sentence.audio_ms
        return result

    @property
    def asked_question(self) -> bool:
        """True once a question has started playing, so "yes" may be the answer to it."""
        position, start = self.position_ms, 0.0
        for sentence in self.sentences:
            if position <= start:
                break
            if sentence.raw.rstrip().endswith("?"):
                return True
            start += sentence.audio_ms
        return False
