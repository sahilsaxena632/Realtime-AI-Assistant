from abc import ABC, abstractmethod


class BaseProvider(ABC):
    """
    All providers must implement this interface.
    The daemon talks only to this interface -
    swapping primary/fallback is transparent.
    """

    # Events the provider fires (set these as callables from daemon.py):
    on_interviewer_question = None  # (text: str) - complete interviewer question
    on_your_speech = None  # (text: str) - your speech detected
    on_ai_chunk = None  # (text: str) - streaming AI answer chunk
    on_ai_done = None  # () - AI answer complete
    on_ai_start = None  # () - AI answer started
    on_interim = None  # (speaker: str, text: str) - live partial transcript
    on_error = None  # (error: str, fatal: bool)
    on_provider_name = None  # (name: str) - fired when provider identity changes

    @abstractmethod
    def start(self, audio_callback):
        """
        Start the provider.
        audio_callback: callable that provider registers with so it receives
                        raw PCM audio chunks from audio.py
        """
        raise NotImplementedError

    @abstractmethod
    def stop(self):
        raise NotImplementedError

    @abstractmethod
    def pause(self):
        raise NotImplementedError

    @abstractmethod
    def resume(self):
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Safe event helpers so subclasses never crash on an unset callback.
    # ------------------------------------------------------------------
    def _fire(self, cb, *args):
        if cb is not None:
            try:
                cb(*args)
            except Exception:
                pass

    def emit_interviewer(self, text):
        self._fire(self.on_interviewer_question, text)

    def emit_your_speech(self, text):
        self._fire(self.on_your_speech, text)

    def emit_ai_start(self):
        self._fire(self.on_ai_start)

    def emit_ai_chunk(self, text):
        self._fire(self.on_ai_chunk, text)

    def emit_ai_done(self):
        self._fire(self.on_ai_done)

    def emit_interim(self, speaker, text):
        self._fire(self.on_interim, speaker, text)

    def emit_error(self, error, fatal=False):
        self._fire(self.on_error, error, fatal)

    def emit_provider_name(self, name):
        self._fire(self.on_provider_name, name)
