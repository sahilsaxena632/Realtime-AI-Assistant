"""Deepgram Nova-2 transcription + Groq/Claude/ChatGPT answers (fallback path).

Streams mixed PCM to Deepgram for diarized transcription, labels speakers via
the SmartChunker, and when the interviewer finishes a question generates a
streamed answer from the preferred AI provider (Groq -> Claude -> ChatGPT).

A background probe periodically checks whether Gemini Live has recovered and,
if so, signals the daemon (via on_provider_name) to switch back.
"""

import json
import os
import threading
import time

import config
from logutil import log
from chunker import SmartChunker
from .base import BaseProvider
from . import gemini_live

try:
    from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents
except Exception:  # pragma: no cover
    DeepgramClient = None
    LiveOptions = None
    LiveTranscriptionEvents = None


# Deterministic channel mapping: mic (channel 0) is always YOU, system audio
# (channel 1) is always the INTERVIEWER. No diarization guessing needed.
CHANNEL_MAP = {0: "YOU", 1: "INTERVIEWER"}


class DeepgramGroqProvider(BaseProvider):
    name = "deepgram-fallback"

    def __init__(self, fallback_ai=None):
        self.fallback_ai = fallback_ai or config.DEFAULT_FALLBACK_AI
        self._running = False
        self._paused = False
        self._dg = None
        self._conn = None
        self._silence = bytes(config.BYTES_PER_CHUNK_STEREO)

        self.chunker = SmartChunker(speaker_map=CHANNEL_MAP)
        self.chunker.on_interviewer_complete = self._on_interviewer
        self.chunker.on_your_complete = self._on_your
        self.chunker.on_interim = lambda spk, t: self.emit_interim(spk, t)

        self._history = []  # list of (speaker, text)
        self._pending = {}  # channel -> latest interim text not yet finalized
        self._tick_thread = None
        self._probe_thread = None
        self._answer_lock = threading.Lock()

    # ------------------------------------------------------------------
    # BaseProvider API
    # ------------------------------------------------------------------
    def start(self, audio_callback):
        self._running = True
        self.emit_provider_name("deepgram-fallback")

        if DeepgramClient is None:
            self.emit_error("deepgram-sdk not installed", fatal=True)
            return
        if not config.DEEPGRAM_API_KEY:
            self.emit_error("Deepgram API key missing", fatal=True)
            return

        try:
            self._connect_deepgram()
        except Exception as e:
            log(f"deepgram: connect failed: {e}")
            self.emit_error("deepgram connect failed", fatal=True)
            return

        audio_callback(self._on_pcm)

        self._tick_thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._tick_thread.start()
        # Only probe for Gemini recovery if Gemini is the configured primary.
        # When the user picks deepgram as primary we must NOT auto-switch away.
        if config.PRIMARY_PROVIDER == "gemini":
            self._probe_thread = threading.Thread(target=self._probe_loop, daemon=True)
            self._probe_thread.start()
        log(f"deepgram: provider started (ai={self.fallback_ai})")

    def stop(self):
        self._running = False
        try:
            if self._conn is not None:
                self._conn.finish()
        except Exception:
            pass
        log("deepgram: provider stopped")

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def set_ai(self, ai):
        self.fallback_ai = ai
        log(f"deepgram: fallback AI switched to {ai}")

    # ------------------------------------------------------------------
    # Deepgram connection
    # ------------------------------------------------------------------
    def _connect_deepgram(self):
        self._dg = DeepgramClient(config.DEEPGRAM_API_KEY)
        # websocket client (sync) in deepgram-sdk v3.
        try:
            self._conn = self._dg.listen.websocket.v("1")
        except Exception:
            self._conn = self._dg.listen.live.v("1")

        self._conn.on(LiveTranscriptionEvents.Transcript, self._on_transcript)
        self._conn.on(LiveTranscriptionEvents.UtteranceEnd, self._on_utterance_end)
        self._conn.on(LiveTranscriptionEvents.Error, self._on_dg_error)

        # Stereo multichannel: channel 0 = mic (YOU), channel 1 = system audio
        # (INTERVIEWER). This makes speaker labeling deterministic.
        options = LiveOptions(
            model=config.DEEPGRAM_MODEL,
            language="en",
            smart_format=True,
            multichannel=True,
            punctuate=True,
            interim_results=True,
            utterance_end_ms="1500",
            vad_events=True,
            encoding="linear16",
            sample_rate=config.SAMPLE_RATE,
            channels=2,
        )
        if not self._conn.start(options):
            raise RuntimeError("deepgram start() returned False")

    def _on_pcm(self, pcm_bytes):
        if not self._running or self._conn is None:
            return
        try:
            self._conn.send(pcm_bytes if not self._paused else self._silence)
        except Exception as e:
            log(f"deepgram: send error: {e}")

    # ------------------------------------------------------------------
    # Deepgram events
    # ------------------------------------------------------------------
    def _on_transcript(self, *args, **kwargs):
        result = kwargs.get("result") or (args[-1] if args else None)
        if result is None:
            return
        try:
            alt = result.channel.alternatives[0]
        except Exception:
            return
        transcript = (alt.transcript or "").strip()
        if not transcript:
            return
        is_final = bool(getattr(result, "is_final", False))

        # With multichannel, channel_index = [channel_number, total_channels].
        channel = 0
        ci = getattr(result, "channel_index", None)
        if isinstance(ci, (list, tuple)) and ci:
            channel = ci[0]

        if is_final:
            self._pending.pop(channel, None)
            self.chunker.add_words(channel, transcript, is_final=True)
        else:
            # Remember the latest interim so we can commit it on UtteranceEnd
            # even if Deepgram never sends a confident final (marginal audio).
            self._pending[channel] = transcript
            self.chunker.add_interim(channel, transcript)

    def _on_utterance_end(self, *args, **kwargs):
        # Commit any interim that never got a final, then flush.
        if self._pending:
            for ch, txt in list(self._pending.items()):
                self.chunker.add_words(ch, txt, is_final=False)
            self._pending.clear()
        self.chunker.utterance_end()

    def _on_dg_error(self, *args, **kwargs):
        err = kwargs.get("error") or (args[-1] if args else "unknown")
        log(f"deepgram: error event {err}")

    # ------------------------------------------------------------------
    # Chunker callbacks
    # ------------------------------------------------------------------
    def _on_interviewer(self, text):
        self.emit_interviewer(text)
        self._history.append(("INTERVIEWER", text))
        self._history = self._history[-config.MAX_HISTORY_CHUNKS:]
        threading.Thread(target=self._answer, args=(text,), daemon=True).start()

    def _on_your(self, text):
        self.emit_your_speech(text)
        self._history.append(("YOU", text))
        self._history = self._history[-config.MAX_HISTORY_CHUNKS:]

    # ------------------------------------------------------------------
    # AI answer generation
    # ------------------------------------------------------------------
    def _answer(self, question):
        if not self._answer_lock.acquire(blocking=False):
            return
        try:
            order = self._provider_order()
            self.emit_ai_start()
            for provider in order:
                try:
                    streamed = self._stream_provider(provider, question)
                    if streamed:
                        self.emit_ai_done()
                        return
                except Exception as e:
                    log(f"ai[{provider}]: failed: {e}; trying next")
                    continue
            self.emit_ai_chunk("[no AI provider available]")
            self.emit_ai_done()
        finally:
            self._answer_lock.release()

    def _provider_order(self):
        base = ["groq", "claude", "chatgpt"]
        pref = self.fallback_ai if self.fallback_ai in base else "groq"
        return [pref] + [p for p in base if p != pref]

    def _build_messages(self):
        history_lines = []
        for spk, txt in self._history:
            history_lines.append(f"[{spk}]: {txt}")
        return "\n".join(history_lines)

    def _stream_provider(self, provider, question):
        context = self._build_messages()
        user_content = (
            f"Recent conversation:\n{context}\n\n"
            f"Answer the interviewer's latest question: {question}"
        )
        if provider == "groq":
            return self._stream_openai_like(
                "groq", config.GROQ_API_KEY, config.GROQ_MODEL, user_content
            )
        if provider == "chatgpt":
            return self._stream_openai_like(
                "openai", config.OPENAI_API_KEY, config.OPENAI_MODEL, user_content
            )
        if provider == "claude":
            return self._stream_claude(user_content)
        return False

    def _stream_openai_like(self, kind, api_key, model, user_content):
        if not api_key:
            raise RuntimeError(f"{kind} api key missing")
        if kind == "groq":
            from groq import Groq

            client = Groq(api_key=api_key)
        else:
            from openai import OpenAI

            client = OpenAI(api_key=api_key)

        stream = client.chat.completions.create(
            model=model,
            temperature=0.3,
            max_tokens=400,
            stream=True,
            messages=[
                {
                    "role": "system",
                    "content": config.build_system_prompt(
                        config.FALLBACK_BASE_PROMPT
                    ),
                },
                {"role": "user", "content": user_content},
            ],
        )
        got = False
        for event in stream:
            try:
                delta = event.choices[0].delta.content
            except Exception:
                delta = None
            if delta:
                got = True
                self.emit_ai_chunk(delta)
        return got

    def _stream_claude(self, user_content):
        if not config.CLAUDE_API_KEY:
            raise RuntimeError("claude api key missing")
        from anthropic import Anthropic

        client = Anthropic(api_key=config.CLAUDE_API_KEY)
        got = False
        with client.messages.stream(
            model=config.CLAUDE_MODEL,
            max_tokens=400,
            temperature=0.3,
            system=config.build_system_prompt(config.FALLBACK_BASE_PROMPT),
            messages=[{"role": "user", "content": user_content}],
        ) as stream:
            for text in stream.text_stream:
                if text:
                    got = True
                    self.emit_ai_chunk(text)
        return got

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------
    def _tick_loop(self):
        while self._running:
            time.sleep(0.3)
            try:
                self.chunker.tick()
            except Exception:
                pass

    def _probe_loop(self):
        """Every GEMINI_RESTORE_INTERVAL_SEC, see if Gemini Live recovered."""
        while self._running:
            for _ in range(config.GEMINI_RESTORE_INTERVAL_SEC):
                if not self._running:
                    return
                time.sleep(1)
            if not self._running:
                return
            if config.GEMINI_API_KEY and gemini_live.test_connection():
                log("deepgram: Gemini Live recovered; signaling restore")
                self.emit_provider_name("gemini-live")
                return
