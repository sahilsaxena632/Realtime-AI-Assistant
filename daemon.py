"""Core orchestrator.

Owns the audio capture, the phone server and the active provider. Wires
provider events to server pushes, and handles automatic + manual switching
between the Gemini Live primary and the Deepgram fallback.
"""

import threading
import time

import config
from logutil import log
from audio import AudioCapture, list_devices
from server import PhoneServer
from providers import GeminiLiveProvider, DeepgramGroqProvider
from providers import gemini_live


class InterviewDaemon:
    def __init__(
        self,
        mic_index=None,
        sys_index=None,
        fallback_ai=None,
        server_key=None,
        mic_source=None,
        sys_source=None,
    ):
        self.mic_index = mic_index
        self.sys_index = sys_index
        self.fallback_ai = fallback_ai or config.DEFAULT_FALLBACK_AI

        self.audio = AudioCapture(
            mic_index=mic_index,
            sys_index=sys_index,
            mic_source=mic_source,
            sys_source=sys_source,
        )
        self.server = PhoneServer(server_key or config.SERVER_KEY)
        self.provider = None
        self._paused = False
        self._switch_lock = threading.Lock()
        self._stopped = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        log("daemon: starting")
        self._wire_server_controls()
        self.server.start()
        self.audio.start()

        # Choose starting provider: explicit preference, else Gemini if keyed.
        if config.PRIMARY_PROVIDER == "deepgram":
            log("daemon: PRIMARY_PROVIDER=deepgram; starting on fallback path")
            self._start_fallback()
        elif config.GEMINI_API_KEY:
            self._start_gemini()
        else:
            log("daemon: no Gemini key; starting on Deepgram fallback")
            self._start_fallback()

        log("daemon: running")

    def run_forever(self):
        try:
            while not self._stopped:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        log("daemon: stopping")
        try:
            if self.provider:
                self.provider.stop()
        except Exception:
            pass
        try:
            self.audio.stop()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Provider startup
    # ------------------------------------------------------------------
    def _start_gemini(self):
        provider = GeminiLiveProvider()
        self._wire_provider(provider)
        self.provider = provider
        self.server.push({"type": "provider", "name": "gemini-live"})
        provider.start(self.audio.get_sys_callback())  # interviewer (system) audio

    def _start_fallback(self):
        provider = DeepgramGroqProvider(fallback_ai=self.fallback_ai)
        self._wire_provider(provider)
        self.provider = provider
        self.server.push({"type": "provider", "name": "deepgram-fallback"})
        self.server.set_fallback_ai(self.fallback_ai)
        provider.start(self.audio.get_stereo_callback())  # labeled stereo

    # ------------------------------------------------------------------
    # Provider wiring
    # ------------------------------------------------------------------
    def _wire_provider(self, provider):
        provider.on_interviewer_question = lambda t: self.server.push(
            {"type": "chunk", "speaker": "INTERVIEWER", "text": t}
        )
        provider.on_your_speech = lambda t: self.server.push(
            {"type": "chunk", "speaker": "YOU", "text": t}
        )
        provider.on_ai_start = lambda: self.server.push(
            {"type": "ai_start", "provider": getattr(provider, "name", "")}
        )
        provider.on_ai_chunk = lambda t: self.server.push(
            {"type": "ai_chunk", "text": t}
        )
        provider.on_ai_done = lambda: self.server.push({"type": "ai_done"})
        provider.on_interim = lambda spk, t: self.server.push(
            {"type": "interim", "speaker": spk, "text": t}
        )
        provider.on_error = self._handle_provider_error
        provider.on_provider_name = self._handle_provider_name

    def _handle_provider_name(self, name):
        # A fallback provider reporting "gemini-live" means Gemini recovered.
        # Only auto-restore when Gemini is the user's configured primary.
        if (
            name == "gemini-live"
            and isinstance(self.provider, DeepgramGroqProvider)
            and config.PRIMARY_PROVIDER == "gemini"
        ):
            log("daemon: fallback signaled Gemini recovery; restoring primary")
            self._try_gemini_restore()
        else:
            self.server.push({"type": "provider", "name": name})

    def _handle_provider_error(self, error, fatal):
        log(f"daemon: provider error: {error}, fatal={fatal}")
        if not fatal:
            self.server.push({"type": "error", "message": str(error)})
            return
        if isinstance(self.provider, GeminiLiveProvider):
            log("daemon: switching to Deepgram fallback")
            self.server.push({"type": "provider", "name": "deepgram-fallback"})
            self._switch_to_fallback()
        elif isinstance(self.provider, DeepgramGroqProvider):
            if config.PRIMARY_PROVIDER == "gemini":
                log("daemon: fallback failed - retrying Gemini in 30s")
                threading.Timer(30, self._try_gemini_restore).start()
            else:
                log("daemon: fallback failed - restarting Deepgram in 10s")
                threading.Timer(10, self._restart_fallback).start()

    # ------------------------------------------------------------------
    # Switching
    # ------------------------------------------------------------------
    def _switch_to_fallback(self):
        with self._switch_lock:
            if isinstance(self.provider, DeepgramGroqProvider):
                return
            try:
                if self.provider:
                    self.provider.stop()
            except Exception:
                pass
            fallback = DeepgramGroqProvider(fallback_ai=self.fallback_ai)
            self._wire_provider(fallback)
            self.provider = fallback
            if self._paused:
                fallback.pause()
            self.server.set_fallback_ai(self.fallback_ai)
            fallback.start(self.audio.get_stereo_callback())  # labeled stereo
            log("daemon: now on Deepgram fallback")

    def _restart_fallback(self):
        if self._stopped:
            return
        with self._switch_lock:
            try:
                if self.provider:
                    self.provider.stop()
            except Exception:
                pass
            fallback = DeepgramGroqProvider(fallback_ai=self.fallback_ai)
            self._wire_provider(fallback)
            self.provider = fallback
            if self._paused:
                fallback.pause()
            self.server.set_fallback_ai(self.fallback_ai)
            fallback.start(self.audio.get_stereo_callback())
            log("daemon: restarted Deepgram fallback")

    def _try_gemini_restore(self):
        if self._stopped:
            return
        if not config.GEMINI_API_KEY:
            return
        if not gemini_live.test_connection():
            log("daemon: Gemini still unavailable")
            # Re-arm a later retry while we remain in fallback.
            if isinstance(self.provider, DeepgramGroqProvider):
                threading.Timer(30, self._try_gemini_restore).start()
            return
        with self._switch_lock:
            if isinstance(self.provider, GeminiLiveProvider):
                return
            try:
                if self.provider:
                    self.provider.stop()
            except Exception:
                pass
            provider = GeminiLiveProvider()
            self._wire_provider(provider)
            self.provider = provider
            if self._paused:
                provider.pause()
            self.server.push({"type": "provider", "name": "gemini-live"})
            provider.start(self.audio.get_sys_callback())  # interviewer (system) audio
            log("daemon: restored Gemini Live primary")

    def _manual_switch(self, target):
        if target in ("fallback", "deepgram", "deepgram-fallback"):
            if not isinstance(self.provider, DeepgramGroqProvider):
                self._switch_to_fallback()
        elif target in ("gemini", "gemini-live"):
            if not isinstance(self.provider, GeminiLiveProvider):
                self._try_gemini_restore()

    # ------------------------------------------------------------------
    # Control plane (server -> daemon)
    # ------------------------------------------------------------------
    def _wire_server_controls(self):
        self.server.on_pause = self.pause
        self.server.on_resume = self.resume
        self.server.on_clear = lambda: None
        self.server.on_switch_ai = self._set_fallback_ai
        self.server.on_switch_provider = self._manual_switch
        self.server.on_stop = self.stop
        self.server.get_devices = list_devices

    def _set_fallback_ai(self, ai):
        self.fallback_ai = ai
        if isinstance(self.provider, DeepgramGroqProvider):
            self.provider.set_ai(ai)

    def pause(self):
        self._paused = True
        self.audio.pause()
        if self.provider:
            self.provider.pause()
        log("daemon: paused")

    def resume(self):
        self._paused = False
        self.audio.resume()
        if self.provider:
            self.provider.resume()
        log("daemon: resumed")
