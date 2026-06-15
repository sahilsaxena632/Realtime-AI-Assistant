"""Dual audio capture producing both a mono mix and a labeled stereo stream.

Two independent sources are captured separately:
  * mic    -> "YOU" (the candidate)        -> stereo channel 0 / left
  * system -> "INTERVIEWER" (call audio)   -> stereo channel 1 / right

Keeping them separate is what lets the app tell the two speakers apart
deterministically instead of guessing from voice. Capturing the system
*monitor* is non-destructive, so you still hear the meeting normally.

Two outputs are emitted every 20ms:
  * mono mix   -> Gemini Live (single end-to-end stream)
  * stereo     -> Deepgram multichannel (channel 0 = YOU, channel 1 = INTERVIEWER)

Capture backend:
  * Preferred: PulseAudio/PipeWire source names via `parec` subprocesses
    (lets us address the monitor source, which PortAudio cannot do by index).
  * Fallback: a single `sounddevice` input by index (mic-only / no routing).
"""

import collections
import queue
import shutil
import subprocess
import threading
import time

import numpy as np

try:
    import sounddevice as sd
except Exception:  # pragma: no cover
    sd = None

import config
from logutil import log


class _AGC:
    """Lightweight automatic gain control.

    Monitor/loopback sources are often heavily attenuated, which makes speech
    too quiet for transcription. This boosts quiet speech toward a target peak,
    never attenuates loud audio, caps the gain, and gates near-silence so it
    does not amplify background hiss.
    """

    def __init__(self, target=8000.0, max_gain=40.0, gate=12.0, smooth=0.25):
        self.target = target
        self.max_gain = max_gain
        self.gate = gate
        self.smooth = smooth
        self.gain = 1.0

    def process(self, frame_int16):
        xf = frame_int16.astype(np.float32)
        peak = float(np.abs(xf).max()) if xf.size else 0.0
        if peak < self.gate:
            target_gain = 1.0  # silence: don't amplify noise
        else:
            target_gain = max(1.0, min(self.max_gain, self.target / peak))
        self.gain += (target_gain - self.gain) * self.smooth
        out = np.clip(xf * self.gain, -32768, 32767)
        return out.astype(np.int16)


class AudioCapture:
    def __init__(self, mic_index=None, sys_index=None, mic_source=None, sys_source=None):
        self.mic_index = mic_index
        self.sys_index = sys_index
        self.mic_source = mic_source
        self.sys_source = sys_source

        self._mono_cb = None
        self._stereo_cb = None
        self._sysmono_cb = None
        self._cb_lock = threading.Lock()

        self._paused = False
        self._running = False

        self._mic_q: "queue.Queue[np.ndarray]" = queue.Queue()
        self._sys_q: "queue.Queue[np.ndarray]" = queue.Queue()

        self._procs = []
        self._sd_streams = []
        self._mixer_thread = None

        self._mono_silence = bytes(config.BYTES_PER_CHUNK)
        self._stereo_silence = bytes(config.BYTES_PER_CHUNK_STEREO)

        self._agc_enabled = config.AUDIO_AGC
        self._agc_mic = _AGC(target=config.AUDIO_TARGET_PEAK, max_gain=config.AUDIO_MAX_GAIN)
        self._agc_sys = _AGC(target=config.AUDIO_TARGET_PEAK, max_gain=config.AUDIO_MAX_GAIN)

        replay_len = int(2000 / config.CHUNK_MS)
        self._recent_mono = collections.deque(maxlen=replay_len)
        self._recent_stereo = collections.deque(maxlen=replay_len)
        self._recent_sysmono = collections.deque(maxlen=replay_len)

    # ------------------------------------------------------------------
    # Registration (providers call these via the daemon)
    # ------------------------------------------------------------------
    def get_chunk_callback(self):
        """Mono mix registrar (used by Gemini Live)."""
        return self._register_mono

    def get_stereo_callback(self):
        """Stereo registrar (used by Deepgram multichannel)."""
        return self._register_stereo

    def get_sys_callback(self):
        """System-audio-only mono registrar (used by Gemini = interviewer)."""
        return self._register_sysmono

    def _register_mono(self, callback):
        with self._cb_lock:
            self._mono_cb = callback
            recent = list(self._recent_mono)
        self._replay(callback, recent)

    def _register_stereo(self, callback):
        with self._cb_lock:
            self._stereo_cb = callback
            recent = list(self._recent_stereo)
        self._replay(callback, recent)

    def _register_sysmono(self, callback):
        with self._cb_lock:
            self._sysmono_cb = callback
            recent = list(self._recent_sysmono)
        self._replay(callback, recent)

    @staticmethod
    def _replay(callback, recent):
        for pcm in recent:
            try:
                callback(pcm)
            except Exception:
                break

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        self._running = True
        recorder = self._pick_recorder()
        want_sources = bool(self.mic_source or self.sys_source)

        if want_sources and recorder:
            if self.mic_source:
                self._spawn_capture("mic", self.mic_source, self._mic_q, recorder)
            if self.sys_source:
                self._spawn_capture("sys", self.sys_source, self._sys_q, recorder)
            log(
                f"audio: {recorder} capture mic={self.mic_source!r} "
                f"sys={self.sys_source!r}"
            )
        else:
            if want_sources and not recorder:
                log("audio: pw-record/parec not found; falling back to sounddevice")
            self._start_sounddevice()

        self._mixer_thread = threading.Thread(target=self._mixer_loop, daemon=True)
        self._mixer_thread.start()
        log("audio: capture started")

    def stop(self):
        self._running = False
        for p in self._procs:
            try:
                p.terminate()
            except Exception:
                pass
        self._procs = []
        for s in self._sd_streams:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        self._sd_streams = []
        log("audio: capture stopped")

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    # ------------------------------------------------------------------
    # Capture backends
    # ------------------------------------------------------------------
    @staticmethod
    def _pick_recorder():
        # pw-record (native PipeWire) is preferred: on PipeWire systems `parec`
        # often returns only silence, while pw-record streams real samples.
        if shutil.which("pw-record"):
            return "pw-record"
        if shutil.which("parec"):
            return "parec"
        return None

    def _capture_cmd(self, source, recorder):
        if recorder == "pw-record":
            return [
                "pw-record",
                "--target",
                source,
                "--rate",
                str(config.SAMPLE_RATE),
                "--channels",
                "1",
                "--format",
                "s16",
                "--raw",
                "--latency",
                "20ms",
                "-",
            ]
        return [
            "parec",
            "-d",
            source,
            "--format=s16le",
            f"--rate={config.SAMPLE_RATE}",
            "--channels=1",
            "--latency-msec=20",
        ]

    def _spawn_capture(self, name, source, q, recorder):
        cmd = self._capture_cmd(source, recorder)

        def worker():
            read_bytes = config.BYTES_PER_CHUNK
            while self._running:
                try:
                    proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
                    )
                    self._procs.append(proc)
                    log(f"audio[{name}]: {recorder} capturing {source}")
                    while self._running:
                        data = proc.stdout.read(read_bytes)
                        if not data:
                            break
                        q.put(np.frombuffer(data, dtype=np.int16).copy())
                except Exception as e:
                    log(f"audio[{name}]: {recorder} error: {e}")
                if not self._running:
                    break
                log(f"audio[{name}]: {recorder} stream ended; retry in 3s")
                time.sleep(3)

        threading.Thread(target=worker, daemon=True).start()

    def _start_sounddevice(self):
        if sd is None:
            log("audio: sounddevice unavailable and no pulse source set")
            return
        idx = self.mic_index if self.mic_index is not None else self.sys_index
        target_q = self._mic_q if self.mic_index is not None else self._sys_q

        def cb(indata, frames, time_info, status):
            if status:
                log(f"audio[sd]: status {status}")
            mono = indata[:, 0] if indata.ndim > 1 else indata
            target_q.put(mono.copy())

        while self._running:
            try:
                stream = sd.InputStream(
                    samplerate=config.SAMPLE_RATE,
                    blocksize=config.SAMPLES_PER_CHUNK,
                    device=idx,
                    channels=1,
                    dtype="int16",
                    callback=cb,
                )
                stream.start()
                self._sd_streams.append(stream)
                log(f"audio[sd]: opened device index={idx}")
                return
            except Exception as e:
                log(f"audio[sd]: open failed ({idx}): {e}; retry in 3s")
                time.sleep(3)

    # ------------------------------------------------------------------
    # Mixer
    # ------------------------------------------------------------------
    @staticmethod
    def _drain(q):
        buf = None
        while not q.empty():
            try:
                chunk = q.get_nowait()
            except queue.Empty:
                break
            buf = chunk if buf is None else np.concatenate([buf, chunk])
        return buf

    def _mixer_loop(self):
        period = config.CHUNK_MS / 1000.0
        n = config.SAMPLES_PER_CHUNK
        mic_acc = np.zeros(0, dtype=np.int16)
        sys_acc = np.zeros(0, dtype=np.int16)

        next_tick = time.monotonic()
        while self._running:
            next_tick += period
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)

            if self._paused:
                self._emit_mono(self._mono_silence, real=False)
                self._emit_stereo(self._stereo_silence, real=False)
                self._emit_sysmono(self._mono_silence, real=False)
                continue

            new_mic = self._drain(self._mic_q)
            new_sys = self._drain(self._sys_q)
            if new_mic is not None:
                mic_acc = np.concatenate([mic_acc, new_mic])
            if new_sys is not None:
                sys_acc = np.concatenate([sys_acc, new_sys])

            # Take one frame from each; zero-pad whichever side is short so the
            # two channels stay time-aligned and the stream stays real-time.
            if mic_acc.size >= n:
                mic_frame = mic_acc[:n]
                mic_acc = mic_acc[n:]
            else:
                mic_frame = np.zeros(n, dtype=np.int16)
            if sys_acc.size >= n:
                sys_frame = sys_acc[:n]
                sys_acc = sys_acc[n:]
            else:
                sys_frame = np.zeros(n, dtype=np.int16)

            # Boost quiet capture (e.g. attenuated monitor sources) so providers
            # can actually transcribe it.
            if self._agc_enabled:
                mic_frame = self._agc_mic.process(mic_frame)
                sys_frame = self._agc_sys.process(sys_frame)

            # Mono mix (averaged to avoid clipping) for Gemini.
            mono = ((mic_frame.astype(np.int32) + sys_frame.astype(np.int32)) // 2)
            mono = np.clip(mono, -32768, 32767).astype(np.int16)
            self._emit_mono(mono.tobytes(), real=True)

            # Interleaved stereo for Deepgram: L = mic (YOU), R = system (INTERVIEWER).
            stereo = np.empty(n * 2, dtype=np.int16)
            stereo[0::2] = mic_frame
            stereo[1::2] = sys_frame
            self._emit_stereo(stereo.tobytes(), real=True)

            # System-only mono for Gemini (the interviewer's side).
            self._emit_sysmono(sys_frame.tobytes(), real=True)

    def _emit_mono(self, pcm, real):
        if real:
            self._recent_mono.append(pcm)
        with self._cb_lock:
            cb = self._mono_cb
        if cb is not None:
            try:
                cb(pcm)
            except Exception as e:
                log(f"audio: mono callback error: {e}")

    def _emit_stereo(self, pcm, real):
        if real:
            self._recent_stereo.append(pcm)
        with self._cb_lock:
            cb = self._stereo_cb
        if cb is not None:
            try:
                cb(pcm)
            except Exception as e:
                log(f"audio: stereo callback error: {e}")

    def _emit_sysmono(self, pcm, real):
        if real:
            self._recent_sysmono.append(pcm)
        with self._cb_lock:
            cb = self._sysmono_cb
        if cb is not None:
            try:
                cb(pcm)
            except Exception as e:
                log(f"audio: sysmono callback error: {e}")


def list_devices():
    """Return a list of input-capable devices as dicts."""
    if sd is None:
        return []
    out = []
    try:
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            if d.get("max_input_channels", 0) > 0:
                out.append(
                    {
                        "index": i,
                        "name": d.get("name", ""),
                        "channels": d.get("max_input_channels", 0),
                        "default_samplerate": d.get("default_samplerate", 0),
                    }
                )
    except Exception as e:
        log(f"audio: list_devices error: {e}")
    return out


def list_pulse_sources():
    """Return PulseAudio/PipeWire sources via `pactl list sources short`."""
    if not shutil.which("pactl"):
        return []
    try:
        out = subprocess.check_output(
            ["pactl", "list", "sources", "short"], text=True, stderr=subprocess.DEVNULL
        )
    except Exception as e:
        log(f"audio: pactl list sources failed: {e}")
        return []
    sources = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            sources.append({"id": parts[0], "name": parts[1]})
    return sources


def default_sources():
    """Best-effort (mic_source, sys_source) using pactl defaults."""
    mic = sys_src = None
    if not shutil.which("pactl"):
        return mic, sys_src
    try:
        mic = subprocess.check_output(
            ["pactl", "get-default-source"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        mic = None
    try:
        sink = subprocess.check_output(
            ["pactl", "get-default-sink"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        if sink:
            sys_src = sink + ".monitor"
    except Exception:
        sys_src = None
    return mic, sys_src
