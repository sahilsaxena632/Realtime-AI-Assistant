"""Smart text chunker and speaker labeler for the Deepgram fallback path.

Deepgram emits interim/final transcripts with per-word speaker ids. This module
accumulates words per speaker, splits on speaker change or silence, labels the
speaker as [YOU] or [INTERVIEWER] (optionally informed by calibration), and
decides when an interviewer chunk is "complete" enough to trigger an AI answer.
"""

import time

import config


class SmartChunker:
    def __init__(self, speaker_map=None):
        # speaker_map: {deepgram_speaker_id: "YOU"|"INTERVIEWER"}
        # Default convention: speaker 0 = YOU, speaker 1 = INTERVIEWER.
        self.speaker_map = speaker_map or {0: "YOU", 1: "INTERVIEWER"}

        self._cur_speaker = None
        self._buffer = ""
        self._last_word_time = time.monotonic()

        # Callbacks wired by the provider.
        self.on_interviewer_complete = None  # (text)
        self.on_your_complete = None  # (text)
        self.on_interim = None  # (label, text)

    def label_for(self, speaker_id):
        return self.speaker_map.get(speaker_id, "INTERVIEWER" if speaker_id else "YOU")

    def add_interim(self, speaker_id, text):
        """Live partial transcript - just surface it, do not commit."""
        label = self.label_for(speaker_id)
        if self.on_interim:
            self.on_interim(label, text)

    def add_words(self, speaker_id, text, is_final=False):
        """Add finalized words for a speaker, splitting on speaker change."""
        label = self.label_for(speaker_id)
        now = time.monotonic()

        # Speaker changed mid-stream: flush whatever we had.
        if self._cur_speaker is not None and label != self._cur_speaker:
            self._flush()

        self._cur_speaker = label
        if text:
            self._buffer = (self._buffer + " " + text).strip() if self._buffer else text
            self._last_word_time = now

        if is_final:
            self._flush()

    def tick(self):
        """Call periodically; flushes the buffer after a silence gap."""
        if not self._buffer:
            return
        if time.monotonic() - self._last_word_time >= config.SILENCE_SPLIT_SEC:
            self._flush()

    def utterance_end(self):
        """Deepgram UtteranceEnd event - definitive flush point."""
        self._flush()

    def _flush(self):
        text = self._buffer.strip()
        speaker = self._cur_speaker
        self._buffer = ""
        if not text or speaker is None:
            return

        word_count = len(text.split())
        if speaker == "INTERVIEWER":
            if word_count >= config.MIN_CHUNK_WORDS and self.on_interviewer_complete:
                self.on_interviewer_complete(text)
        else:
            if self.on_your_complete:
                self.on_your_complete(text)
