"""Gemini Live provider (primary path).

The current Gemini Live models output AUDIO only, so we request audio output
plus input/output transcription. We feed Gemini the interviewer's audio (the
system side); Gemini transcribes the question (inputTranscription) and speaks an
answer which we capture as text (outputTranscription) and stream to the phone.

Endpoint: v1beta BidiGenerateContent. Model: gemini-3.1-flash-live-preview
(configurable). TEXT response modality is not supported by these models.
"""

import asyncio
import base64
import json
import threading
import time

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except Exception:  # pragma: no cover
    websockets = None
    ConnectionClosed = Exception

import config
from logutil import log
from .base import BaseProvider

# WebSocket close codes that mean "configuration is wrong, do not retry".
_FATAL_CLOSE_CODES = {1002, 1003, 1007, 1008}


class GeminiLiveProvider(BaseProvider):
    name = "gemini-live"

    def __init__(self):
        self._loop = None
        self._thread = None
        self._ws = None
        self._running = False
        self._paused = False
        self._audio_q = None

        # Per-turn transcription state.
        self._input_buf = ""
        self._last_input_time = 0.0
        self._question_emitted = False
        self._answer_started = False

        # Answer timeout watchdog.
        self._awaiting_answer = False
        self._await_stamp = 0.0

        self._last_frame_sent = 0.0
        self._video_disabled = False

    # ------------------------------------------------------------------
    # BaseProvider API
    # ------------------------------------------------------------------
    def start(self, audio_callback):
        if websockets is None:
            self.emit_error("websockets library not installed", fatal=True)
            return
        if not config.GEMINI_API_KEY:
            self.emit_error("Gemini API key missing", fatal=True)
            return
        self._running = True
        audio_callback(self._on_pcm)
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()
        log("gemini: provider started")

    def stop(self):
        self._running = False
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
        log("gemini: provider stopped")

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    # ------------------------------------------------------------------
    # Audio intake (mixer thread)
    # ------------------------------------------------------------------
    def _on_pcm(self, pcm_bytes):
        if not self._running or self._loop is None or self._audio_q is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._audio_q.put_nowait, pcm_bytes)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Event loop
    # ------------------------------------------------------------------
    def _thread_main(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._audio_q = asyncio.Queue()
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:
            if self._running:
                log(f"gemini: loop crashed: {e}")
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _main(self):
        if not await self._connect_with_retries(initial=True):
            return
        while self._running:
            try:
                await asyncio.gather(
                    self._sender(), self._receiver(), self._watchdog()
                )
            except _Fatal as f:
                self.emit_error(str(f), fatal=True)
                return
            except _Reconnect:
                if not await self._connect_with_retries():
                    return
            except Exception as e:
                log(f"gemini: session error: {e}")
                if not await self._connect_with_retries():
                    return

    async def _connect_with_retries(self, initial=False):
        tries = config.GEMINI_RECONNECT_TRIES
        for attempt in range(tries):
            if not self._running:
                return False
            if not initial or attempt > 0:
                await asyncio.sleep(2 ** attempt)
            try:
                await self._connect()
                self.emit_provider_name("gemini-live")
                log("gemini: connected")
                return True
            except Exception as e:
                code = _close_code(e)
                log(f"gemini: connect attempt {attempt + 1}/{tries} failed: {e}")
                if code in _FATAL_CLOSE_CODES or _is_auth_error(e):
                    self.emit_error(f"gemini config error: {e}", fatal=True)
                    return False
                if _is_rate_limit(e):
                    self.emit_error("rate_limited", fatal=True)
                    return False
                continue
        self.emit_error("Gemini Live unavailable after 3 retries", fatal=True)
        return False

    async def _connect(self):
        url = f"{config.GEMINI_WS_URL}?key={config.GEMINI_API_KEY}"
        self._ws = await websockets.connect(url, max_size=None, ping_interval=20)
        setup_msg = {
            "setup": {
                "model": config.GEMINI_MODEL,
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "temperature": 0.3,
                },
                "systemInstruction": {
                    "parts": [
                        {
                            "text": config.build_system_prompt(
                                config.GEMINI_BASE_PROMPT
                            )
                        }
                    ]
                },
                "outputAudioTranscription": {},
                "inputAudioTranscription": {},
            }
        }
        await self._ws.send(json.dumps(setup_msg))
        # First server message should be setupComplete (or a fatal close).
        raw = await self._ws.recv()
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "ignore")
        data = json.loads(raw)
        if "setupComplete" not in data:
            if data.get("error"):
                raise _Fatal(str(data["error"]))
        self._video_disabled = False
        self._last_frame_sent = 0.0
        self._reset_turn()

    # ------------------------------------------------------------------
    # Sender
    # ------------------------------------------------------------------
    async def _sender(self):
        while self._running:
            pcm = await self._audio_q.get()
            if self._ws is None:
                continue
            data = base64.b64encode(pcm).decode("ascii")
            msg = {
                "realtimeInput": {
                    "audio": {"data": data, "mimeType": "audio/pcm;rate=16000"}
                }
            }
            try:
                await self._ws.send(json.dumps(msg))
                await self._maybe_send_frame()
            except ConnectionClosed as e:
                self._raise_for_close(e)
            except Exception as e:
                log(f"gemini: send failed: {e}")
                raise _Reconnect()

    async def _maybe_send_frame(self):
        """Attach the latest screen frame as realtime video, at most every 2s."""
        if self._video_disabled or self.frame_source is None:
            return
        now = time.time()
        if now - self._last_frame_sent < 2.0:
            return
        frame = self.get_frame(config.SCREEN_FRAME_MAX_AGE_SEC)
        if not frame:
            return
        self._last_frame_sent = now
        msg = {
            "realtimeInput": {
                "video": {
                    "data": base64.b64encode(frame).decode("ascii"),
                    "mimeType": "image/jpeg",
                }
            }
        }
        try:
            await self._ws.send(json.dumps(msg))
        except ConnectionClosed:
            raise
        except Exception as e:
            self._video_disabled = True
            log(f"gemini: video frames disabled for this session: {e}")

    # ------------------------------------------------------------------
    # Receiver
    # ------------------------------------------------------------------
    async def _receiver(self):
        while self._running:
            try:
                raw = await self._ws.recv()
            except ConnectionClosed as e:
                self._raise_for_close(e)
            except Exception as e:
                log(f"gemini: recv failed: {e}")
                raise _Reconnect()

            if isinstance(raw, (bytes, bytearray)):
                try:
                    raw = raw.decode("utf-8")
                except Exception:
                    continue
            try:
                data = json.loads(raw)
            except Exception:
                continue

            if data.get("error"):
                err = str(data["error"])
                if "video" in err.lower():
                    self._video_disabled = True
                    log(f"gemini: video frames disabled for this session: {err}")
                    continue
                raise _Fatal(err)
            self._handle_server_message(data)

    def _handle_server_message(self, data):
        sc = data.get("serverContent") or data.get("server_content")
        if not sc:
            return

        in_tr = sc.get("inputTranscription") or sc.get("input_transcription")
        if in_tr and in_tr.get("text"):
            self._input_buf += in_tr["text"]
            self._last_input_time = time.monotonic()
            self.emit_interim("INTERVIEWER", self._input_buf.strip())

        out_tr = sc.get("outputTranscription") or sc.get("output_transcription")
        if out_tr and out_tr.get("text"):
            if not self._answer_started:
                self._flush_question()
                self.emit_ai_start()
                self._answer_started = True
                self._awaiting_answer = False
            self.emit_ai_chunk(out_tr["text"])

        if sc.get("turnComplete") or sc.get("turn_complete"):
            if self._answer_started:
                self.emit_ai_done()
            self._reset_turn()

    def _flush_question(self):
        q = self._input_buf.strip()
        if q and not self._question_emitted:
            self.emit_interviewer(q)
            self._question_emitted = True
            self._awaiting_answer = True
            self._await_stamp = time.monotonic()

    def _reset_turn(self):
        self._input_buf = ""
        self._question_emitted = False
        self._answer_started = False
        self._awaiting_answer = False

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------
    async def _watchdog(self):
        while self._running:
            await asyncio.sleep(0.5)
            # Surface the question even if the model is slow / stays silent.
            if (
                self._input_buf
                and not self._question_emitted
                and (time.monotonic() - self._last_input_time) > 1.2
            ):
                self._flush_question()
            # Answer timeout.
            if (
                self._awaiting_answer
                and not self._answer_started
                and (time.monotonic() - self._await_stamp) > config.GEMINI_TIMEOUT_SEC
            ):
                log("gemini: answer timeout")
                self._awaiting_answer = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _raise_for_close(self, exc):
        code = _close_code(exc)
        if code in _FATAL_CLOSE_CODES:
            raise _Fatal(f"gemini closed (code {code}): {exc}")
        raise _Reconnect()


class _Reconnect(Exception):
    pass


class _Fatal(Exception):
    pass


def _close_code(exc):
    for attr in ("code", "rcvd", "sent"):
        obj = getattr(exc, attr, None)
        if obj is None:
            continue
        if isinstance(obj, int):
            return obj
        code = getattr(obj, "code", None)
        if isinstance(code, int):
            return code
    return None


def _is_auth_error(exc):
    s = str(exc).lower()
    return "401" in s or "403" in s or "api key" in s or "unauthorized" in s


def _is_rate_limit(exc):
    s = str(exc).lower()
    return "429" in s or ("rate" in s and "limit" in s)


def test_connection(timeout=6.0):
    """Probe whether Gemini Live accepts a setup handshake (AUDIO modality)."""
    if websockets is None or not config.GEMINI_API_KEY:
        return False

    async def _probe():
        url = f"{config.GEMINI_WS_URL}?key={config.GEMINI_API_KEY}"
        try:
            async with websockets.connect(url, max_size=None) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "setup": {
                                "model": config.GEMINI_MODEL,
                                "generationConfig": {"responseModalities": ["AUDIO"]},
                                "outputAudioTranscription": {},
                            }
                        }
                    )
                )
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", "ignore")
                data = json.loads(raw)
                return "setupComplete" in data
        except Exception:
            return False

    try:
        return asyncio.run(asyncio.wait_for(_probe(), timeout=timeout + 2))
    except Exception:
        return False
