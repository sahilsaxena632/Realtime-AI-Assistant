"""Speaker voice calibration.

Records ~10s of mic-only audio while you introduce yourself, extracts a speaker
embedding with pyannote.audio (if available) and writes calibration.json. The
embedding + a default speaker id help the Deepgram fallback label [YOU] vs
[INTERVIEWER] correctly.

Run in the foreground:  python3 main.py --calibrate
"""

import json
import time

import numpy as np

import config
from logutil import log

try:
    import sounddevice as sd
except Exception:
    sd = None

RECORD_SECONDS = 10


def run_calibration(mic_index=None):
    config.ensure_dirs()
    if sd is None:
        print("sounddevice not available - cannot calibrate.")
        return False

    print("\n=== Voice Calibration ===")
    print(
        "Speak naturally for 10 seconds - say your name, introduce yourself, "
        "talk about anything.\n"
    )
    for i in (3, 2, 1):
        print(f"Recording starts in {i}...")
        time.sleep(1)
    print("Recording NOW. Speak!")

    frames = int(RECORD_SECONDS * config.SAMPLE_RATE)
    try:
        audio = sd.rec(
            frames,
            samplerate=config.SAMPLE_RATE,
            channels=1,
            dtype="int16",
            device=mic_index,
        )
        sd.wait()
    except Exception as e:
        print(f"Recording failed: {e}")
        log(f"calibration: recording failed: {e}")
        return False

    print("Recording complete. Processing...")
    pcm = audio.reshape(-1).astype(np.int16)

    embedding = _extract_embedding(pcm)

    data = {
        "created_at": time.time(),
        "sample_rate": config.SAMPLE_RATE,
        # During calibration only you speak; Deepgram typically labels the first
        # speaker as 0, so default [YOU] = speaker 0.
        "you_speaker_id": 0,
        "embedding": embedding,
        "has_embedding": embedding is not None,
    }
    try:
        with open(config.CALIBRATION_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        print(f"Calibration saved to {config.CALIBRATION_FILE}")
        log("calibration: saved")
        return True
    except Exception as e:
        print(f"Failed to save calibration: {e}")
        log(f"calibration: save failed: {e}")
        return False


def _extract_embedding(pcm):
    """Return a speaker embedding as a list of floats, or None if unavailable."""
    try:
        import torch
        from pyannote.audio import Inference, Model

        model = Model.from_pretrained("pyannote/embedding")
        inference = Inference(model, window="whole")
        waveform = torch.from_numpy(pcm.astype(np.float32) / 32768.0).unsqueeze(0)
        emb = inference({"waveform": waveform, "sample_rate": config.SAMPLE_RATE})
        return np.asarray(emb).reshape(-1).tolist()
    except Exception as e:
        log(f"calibration: embedding extraction skipped ({e})")
        print(
            "Note: pyannote embedding unavailable (needs model access / HF token).\n"
            "Saved a default speaker mapping instead."
        )
        return None
