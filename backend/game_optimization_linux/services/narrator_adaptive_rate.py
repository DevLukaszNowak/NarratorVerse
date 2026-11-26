"""Bounded, utterance-level catch-up; never alters PCM already playing."""
from __future__ import annotations


class AdaptiveSpeechRate:
    RISE_STEP = 0.10
    FALL_STEP = 0.05
    QUEUE_GRACE_SECONDS = 0.35
    FULL_PRESSURE_SECONDS = 2.5

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.boost = 0.0
        self._previous_at: float | None = None
        self._previous_duration = 0.0
        self._pressure_count = 0
        self._phrase_id: int | None = None

    def select(self, *, base_rate: float, enabled: bool, accepted_at: float,
               text: str, backlog: bool, phrase_id: int, max_boost: float = 0.50,
               pending_audio_seconds: float = 0.0) -> float:
        if not enabled or max_boost == 0:
            self.reset()
            return base_rate
        gap = None if self._previous_at is None else accepted_at - self._previous_at
        if gap is not None and gap < 0:
            self.reset()
            gap = None

        queue_pressure = (pending_audio_seconds - self.QUEUE_GRACE_SECONDS) / self.FULL_PRESSURE_SECONDS
        cadence_pressure = 0.0
        if gap is not None and self._previous_duration > 0:
            cadence_pressure = (0.9 - gap / self._previous_duration) / 0.45
        pressure = min(1.0, max(float(backlog), queue_pressure, cadence_pressure))
        self._pressure_count = min(2, self._pressure_count + 1) if pressure > 0 else 0

        # A single overlap does not start catch-up. Limit both the per-phrase
        # change and the stored boost to what the speech engine can deliver.
        ceiling = min(max_boost, 2.0 / base_rate - 1.0)
        target = ceiling * pressure
        if pressure == 0 and gap is not None:
            idle = max(0.0, gap - self._previous_duration - 3.0)
            self.boost = max(0.0, self.boost - idle * 0.025)
        # Steps scale with the user's ceiling, so +30% and +60% are both
        # reached within a few dialogue lines and released gradually.
        if self._pressure_count >= 2 and target > self.boost:
            self.boost = min(target, self.boost + max(self.RISE_STEP, ceiling / 3))
        elif target < self.boost:
            self.boost = max(target, self.boost - max(self.FALL_STEP, ceiling / 8))
        self.boost = min(self.boost, ceiling)
        rate = min(2.0, base_rate * (1.0 + self.boost))
        self._previous_at = accepted_at
        # Estimate until the provider returns the actual PCM duration.
        self._previous_duration = max(0.5, len(text) / (13.0 * rate))
        self._phrase_id = phrase_id
        return rate

    def synthesized(self, phrase_id: int, duration_seconds: float) -> None:
        # Late/stale inference must not overwrite the newer phrase's estimate.
        if phrase_id == self._phrase_id and 0.3 <= duration_seconds <= 120.0:
            self._previous_duration = duration_seconds
