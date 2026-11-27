from typing import Literal

from pydantic import Field, SecretStr

from agent.config import AidaSettings


class VoiceSettings(AidaSettings):
    # What Aida speaks first on the phone: "en", "uz" (Uzbek, Latin script) or "ru".
    # After that she follows the caller.
    voice_language: Literal["en", "uz", "ru"] = "en"

    # What runs a call. "pipeline": speech to text, the AI (LLM_PROVIDER) and text to
    # speech, each from the providers below. "gemini_live": Gemini Live hears, thinks and
    # speaks by itself, so only GEMINI_API_KEY is needed and STT/TTS settings are unused.
    voice_engine: Literal["pipeline", "gemini_live"] = "pipeline"
    gemini_live_url: str = "wss://generativelanguage.googleapis.com"
    gemini_live_model: str = "gemini-3.8-live"
    # One of Gemini's prebuilt voices: Kore, Aoede, Leda, Puck, Charon, Fenrir...
    gemini_voice: str = "Kore"

    # Telephony
    voice_public_url: str | None = None
    twilio_auth_token: SecretStr | None = None
    admin_phone: str | None = None

    # Speech to text
    stt_provider: Literal["deepgram", "elevenlabs", "voicelab"] = "deepgram"
    stt_confidence: float = Field(default=0.85, ge=0.5, le=1)
    unclear_below: float = Field(default=0.5, ge=0, le=1)
    deepgram_api_key: SecretStr | None = None
    deepgram_url: str = "wss://api.deepgram.com"
    deepgram_model: str = "flux-general-en"

    # Text to speech
    tts_provider: Literal["elevenlabs", "voicelab"] = "elevenlabs"
    elevenlabs_api_key: SecretStr | None = None
    elevenlabs_url: str = "https://api.elevenlabs.io"
    elevenlabs_voice_id: str | None = None
    elevenlabs_tts_model: str = "eleven_flash_v2_5"
    elevenlabs_stt_model: str = "scribe_v2_realtime"

    # VoiceLab: both speech to text and text to speech
    voicelab_api_key: SecretStr | None = None
    voicelab_url: str = "https://api.voicelab.uz"
    # Gulnoza, who speaks Uzbek, Russian and English. Others: GET /v1/voices.
    voicelab_voice: str = "voice_01J9NEUTRAL0000000000000001"
    voicelab_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    # VoiceLab hears whole phrases, so the end of one is found here: this much quiet.
    voicelab_pause_ms: int = Field(default=600, ge=200, le=3000)

    # A phone joined over Bluetooth (voice/bluetooth.py), instead of Twilio. Left
    # empty, the first oFono modem that takes calls and the first Bluetooth
    # microphone and speaker in PulseAudio or PipeWire are used.
    bluetooth_modem: str | None = None
    bluetooth_source: str | None = None
    bluetooth_sink: str | None = None

    silence_seconds: float = Field(default=8, gt=0)
    target_latency_ms: int = 800

    @property
    def missing(self) -> list[str]:
        """Settings a live Twilio call can't work without."""
        twilio = {
            "VOICE_PUBLIC_URL": self.voice_public_url,
            "TWILIO_AUTH_TOKEN": self.twilio_auth_token,
        }
        return self.missing_speech + [name for name, value in twilio.items() if not value]

    @property
    def missing_speech(self) -> list[str]:
        """Settings the speech providers can't work without, on any kind of line."""
        if self.voice_engine == "gemini_live":
            return [] if self.gemini_api_key else ["GEMINI_API_KEY"]
        needed = {}
        if "elevenlabs" in (self.tts_provider, self.stt_provider):
            needed["ELEVENLABS_API_KEY"] = self.elevenlabs_api_key
        if self.tts_provider == "elevenlabs":
            needed["ELEVENLABS_VOICE_ID"] = self.elevenlabs_voice_id
        if "voicelab" in (self.tts_provider, self.stt_provider):
            needed["VOICELAB_API_KEY"] = self.voicelab_api_key
        if self.stt_provider == "deepgram":
            needed["DEEPGRAM_API_KEY"] = self.deepgram_api_key
        return [name for name, value in needed.items() if not value]
