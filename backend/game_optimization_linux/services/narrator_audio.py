"""Qt audio output for synthesized narrator PCM."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
import logging
from threading import Lock
import time
from typing import Any

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, Qt, QTimer, Signal, Slot
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices, QtAudio

from game_optimization_linux.models.narrator import PcmAudio


logger = logging.getLogger(__name__)


_SAMPLE_WIDTHS = {"u8": 1, "s16le": 2, "s32le": 4, "f32le": 4}

# A sink may report marginally less processed audio than was submitted even on a
# clean finish, so require almost all of it rather than exactly all.
_COMPLETION_RATIO = 0.98

# Upper bound on the prefetch buffer. 1 MiB is about 23 s at 22050 Hz mono
# s16le, well beyond any subtitle (a long 11 s line is ~485 KB), and small enough
# that holding it costs nothing. Bounded so a malformed PcmAudio cannot ask the
# backend for an unreasonable allocation.
_MAX_BUFFER_BYTES = 1024 * 1024
_MAX_PENDING_UTTERANCES = 8
_REFILL_TIMEOUT = 1.0


def _expected_seconds(audio: PcmAudio) -> float:
    """Duration implied by the PCM itself, independent of backend state."""

    width = _SAMPLE_WIDTHS.get(audio.sample_format.casefold(), 2)
    frame_bytes = max(1, width * max(1, audio.channels))
    return len(audio.samples) / frame_bytes / max(1, audio.sample_rate)


@dataclass(slots=True)
class _Playback:
    audio: PcmAudio
    volume: float
    request_id: int
    started_callback: Callable[[float], None]
    completed_callback: Callable[[], None]
    error_callback: Callable[[str], None]
    queued_at: float
    started_at: float = 0.0
    first_processed_at: float | None = None
    last_pcm_probe_at: float | None = None
    # Diagnostics only: lets one request_id be tied to the phrase it spoke, so a
    # re-recognised variant of the same subtitle is identifiable in the log.
    text: str = ""
    terminal_callback: Callable[[], None] | None = None
    first_pcm_callback: Callable[[], None] | None = None
    diagnostic_context: dict[str, object] = field(default_factory=dict)

    @property
    def expected_seconds(self) -> float:
        """Duration implied by the PCM itself, independent of backend state."""

        return _expected_seconds(self.audio)


@dataclass(frozen=True, slots=True)
class PlaybackRecord:
    """One playback attempt, for diagnosing truncated speech."""

    request_id: int
    pcm_bytes: int
    pcm_bytes_read: int
    sample_rate: int
    channels: int
    sample_format: str
    bytes_per_sample: int
    expected_seconds: float
    processed_seconds: float
    elapsed_seconds: float
    terminal_state: str
    backend_error: str
    stop_requested: bool
    superseded: bool
    result: str
    reason: str


class QtNarratorAudioOutput(QObject):
    """Play PCM without temporary files using a bounded dialogue FIFO."""

    provider_id = "qt-audio"

    _playRequested = Signal(object)
    _stopRequested = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        sink_factory: Callable[..., Any] | None = None,
        device_provider: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        diagnostic: Callable[..., None] | None = None,
        diagnostic_context: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        super().__init__(parent)
        # Injectable so truncated playback can be reproduced without a device.
        self._sink_factory = sink_factory or QAudioSink
        self._device_provider = device_provider or QMediaDevices.defaultAudioOutput
        self._clock = clock
        self._diagnostic = diagnostic
        self._diagnostic_context = diagnostic_context
        self._history: deque[PlaybackRecord] = deque(maxlen=10)
        self._completed_count = 0
        self._interrupted_count = 0
        self._stop_requested = False
        self._sink: QAudioSink | None = None
        self._buffer: QBuffer | None = None
        self._bytes: QByteArray | None = None
        self._current: _Playback | None = None
        self._pending: _Playback | None = None
        self._waiting: deque[_Playback] = deque()
        self._submitted_lock = Lock()
        self._submitted_seconds = 0.0
        self._superseded_count = 0
        self._playRequested.connect(
            self._queue_playback, Qt.ConnectionType.QueuedConnection
        )
        self._stopRequested.connect(self._stop_all, Qt.ConnectionType.QueuedConnection)
        self._pcm_probe = QTimer(self)
        self._pcm_probe.setInterval(10)
        self._pcm_probe.timeout.connect(self._observe_first_pcm)
        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(30)
        self._idle_timer.timeout.connect(self._retry_idle)
        self._idle_position = -1
        self._idle_since = 0.0

    @property
    def available(self) -> bool:
        return True

    @property
    def completed_count(self) -> int:
        """Utterances whose PCM was actually played to the end."""

        return self._completed_count

    @property
    def interrupted_count(self) -> int:
        """Utterances that stopped before their PCM was consumed."""

        return self._interrupted_count

    @property
    def playback_history(self) -> tuple[PlaybackRecord, ...]:
        return tuple(self._history)

    @property
    def last_playback(self) -> PlaybackRecord | None:
        return self._history[-1] if self._history else None

    @property
    def superseded_count(self) -> int:
        """Number of synthesized, not-yet-playing lines replaced by newer work."""

        return self._superseded_count

    @property
    def queue_depth(self) -> int:
        return int(self._current is not None) + int(self._pending is not None) + len(self._waiting)

    @property
    def queued_seconds(self) -> float:
        current = self._current
        pending = self._pending
        waiting = tuple(self._waiting)
        with self._submitted_lock:
            submitted = self._submitted_seconds
        # Called by the inference thread: do not touch the Qt sink here.
        remaining = max(0.0, current.expected_seconds - max(0.0, self._clock() - current.started_at)) if current is not None else 0.0
        return remaining + submitted + (pending.expected_seconds if pending else 0.0) + sum(item.expected_seconds for item in waiting)

    def play(
        self,
        audio: PcmAudio,
        *,
        volume: float,
        request_id: int,
        started_callback: Callable[[float], None],
        completed_callback: Callable[[], None],
        error_callback: Callable[[str], None],
        text: str = "",
        terminal_callback: Callable[[], None] | None = None,
        first_pcm_callback: Callable[[], None] | None = None,
    ) -> None:
        logger.debug(
            "Narrator playback event=submitted request=%d expected_ms=%.0f text=%r",
            request_id,
            _expected_seconds(audio) * 1000.0,
            text[:120],
        )
        with self._submitted_lock:
            self._submitted_seconds += _expected_seconds(audio)
        self._playRequested.emit(
            _Playback(
                audio=audio,
                volume=max(0.0, min(1.0, float(volume))),
                request_id=request_id,
                started_callback=started_callback,
                completed_callback=completed_callback,
                error_callback=error_callback,
                queued_at=self._clock(),
                text=str(text),
                terminal_callback=terminal_callback,
                first_pcm_callback=first_pcm_callback,
                diagnostic_context=(self._diagnostic_context()
                                    if self._diagnostic is not None and self._diagnostic_context else {}),
            )
        )

    def stop(self) -> None:
        self._stopRequested.emit()

    def _trace(self, event: str, playback: _Playback, **fields: object) -> None:
        if self._diagnostic is not None:
            try:
                self._diagnostic(event, **playback.diagnostic_context,
                                 phrase_id=playback.request_id, **fields)
            except Exception:
                logger.exception("Experimental audio observer failed")

    def _observe_first_pcm(self):
        playback = self._current
        if playback is None or self._sink is None:
            self._pcm_probe.stop()
            return
        now = self._clock()
        previous = playback.last_pcm_probe_at or playback.started_at
        processed = self._processed_seconds(self._sink)
        playback.last_pcm_probe_at = now
        if processed > 0 and playback.first_processed_at is None:
            playback.first_processed_at = now
            self._trace("playback_first_processed_pcm", playback,
                        observed_at_monotonic=playback.first_processed_at,
                        submitted_at_monotonic=playback.started_at,
                        processed_ms=processed*1000,
                        pcm_bytes_read=self._buffer.pos() if self._buffer is not None else 0,
                        pcm_bytes=len(playback.audio.samples),
                        audio_queue_wait_ms=(playback.first_processed_at-playback.queued_at)*1000,
                        observation_interval_ms=max(0., (now-previous)*1000),
                        observation_target_interval_ms=10,
                        previous_observation_at_monotonic=previous,
                        evidence="QAudioSink.processedUSecs>0; not acoustic measurement")
            self._pcm_probe.stop()
            if playback.first_pcm_callback is not None:
                try:
                    playback.first_pcm_callback()
                except Exception:
                    logger.exception("Narrator first PCM playback observer failed")

    @Slot(object)
    def _queue_playback(self, playback: object) -> None:
        if not isinstance(playback, _Playback):
            return
        with self._submitted_lock:
            self._submitted_seconds = max(0.0, self._submitted_seconds - playback.expected_seconds)
        self._trace("sink_submitted", playback, queue_depth=self.queue_depth,
                    expected_ms=playback.expected_seconds * 1000, pcm_bytes=len(playback.audio.samples))
        if self._current is not None:
            # Preserve rapid exchanges in order, including identical text from
            # different occurrences. Only explicit bounded overflow drops work.
            if self._pending is not None:
                if len(self._waiting) >= max(1, getattr(playback.audio, "pending_limit", _MAX_PENDING_UTTERANCES)) - 1:
                    self._superseded_count += 1
                    logger.warning("Narrator playback event=queue_overflow dropped_request=%d", self._pending.request_id)
                    self._trace("audio_dropped", self._pending, reason="queue_overflow",
                                pcm_bytes=len(self._pending.audio.samples))
                    if self._waiting:
                        self._pending = self._waiting.popleft()
                    else:
                        self._pending = playback
                        return
                self._waiting.append(playback)
                return
            logger.debug(
                "Narrator playback event=queued request=%d "
                "waits_for_request_id=%d actively_playing_text=%r text=%r",
                playback.request_id,
                self._current.request_id,
                self._current.text[:120],
                playback.text[:120],
            )
            self._pending = playback
            return
        self._start(playback)

    @Slot()
    def _stop_all(self) -> None:
        self._pcm_probe.stop()
        self._idle_timer.stop()
        # An explicit stop is intentional. Record it so the terminal state that
        # follows is classified as a stop rather than a truncated utterance, and
        # so it never triggers a replay.
        current = self._current
        if current is not None:
            self._stop_requested = True
            self._record(
                current,
                processed_seconds=self._processed_seconds(self._sink)
                if self._sink is not None
                else 0.0,
                elapsed_seconds=(
                    max(0.0, self._clock() - current.started_at)
                    if current.started_at
                    else 0.0
                ),
                terminal_state="StopRequested",
                backend_error="NoError",
                result="stopped",
                reason="session or user stop",
            )
        for waiting in ([self._pending] if self._pending else []) + list(self._waiting):
            self._trace("audio_dropped", waiting, reason="session_or_user_stop", pcm_bytes=len(waiting.audio.samples))
        self._pending = None
        self._waiting.clear()
        self._current = None
        sink = self._sink
        buffer = self._buffer
        self._sink = None
        self._buffer = None
        self._bytes = None
        if sink is not None:
            sink.stateChanged.disconnect(self._state_changed)
            sink.stop()
            sink.deleteLater()
        if buffer is not None:
            buffer.close()
            buffer.deleteLater()

    def _start(self, playback: _Playback) -> None:
        audio_format = QAudioFormat()
        audio_format.setSampleRate(playback.audio.sample_rate)
        audio_format.setChannelCount(playback.audio.channels)
        sample_formats = {
            "u8": QAudioFormat.SampleFormat.UInt8,
            "s16le": QAudioFormat.SampleFormat.Int16,
            "s32le": QAudioFormat.SampleFormat.Int32,
            "f32le": QAudioFormat.SampleFormat.Float,
        }
        sample_format = sample_formats.get(playback.audio.sample_format.casefold())
        if sample_format is None:
            self._notify_failure(
                playback,
                f"Unsupported narrator PCM format: {playback.audio.sample_format}",
            )
            return
        audio_format.setSampleFormat(sample_format)
        device = self._device_provider()
        if device.isNull():
            self._notify_failure(playback, "No audio output device is available")
            return
        if not device.isFormatSupported(audio_format):
            self._notify_failure(
                playback,
                "The audio output does not support the narrator PCM format",
            )
            return
        self._bytes = QByteArray(playback.audio.samples)
        self._buffer = QBuffer(self._bytes, self)
        if not self._buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            self._buffer = None
            self._bytes = None
            self._notify_failure(
                playback, "Narrator PCM playback buffer could not be opened"
            )
            return
        self._sink = self._sink_factory(device, audio_format, self)
        # Request a large prefetch to reduce underruns. Qt may allocate less.
        self._sink.setBufferSize(min(len(playback.audio.samples), _MAX_BUFFER_BYTES))
        self._sink.setVolume(playback.volume)
        self._sink.stateChanged.connect(self._state_changed)
        playback.started_at = self._clock()
        self._stop_requested = False
        self._current = playback
        self._idle_position = -1
        self._sink.start(self._buffer)
        logger.debug(
            "Narrator playback event=started request=%d expected_ms=%.0f "
            "buffer_bytes=%d text=%r",
            playback.request_id,
            playback.expected_seconds * 1000.0,
            len(playback.audio.samples),
            playback.text[:120],
        )
        if self._current is playback:
            self._trace("sink_started", playback,
                        audio_queue_wait_ms=max(0.0, (playback.started_at - playback.queued_at) * 1000),
                        expected_ms=playback.expected_seconds * 1000,
                        pcm_bytes_read=self._buffer.pos(), pcm_bytes=len(playback.audio.samples),
                        sink_buffer_bytes=self._sink.bufferSize())
            if playback.first_processed_at is None and (self._diagnostic is not None or playback.first_pcm_callback is not None):
                self._pcm_probe.start()
            playback.started_callback(
                max(0.0, (self._clock() - playback.queued_at) * 1000.0)
            )

    @Slot()
    def _retry_idle(self) -> None:
        if self._sink is None or self._sink.state() != QtAudio.State.IdleState:
            self._idle_timer.stop()
            return
        if self._buffer is not None and not self._buffer.atEnd():
            # Wake the existing pull stream at the current QBuffer position.
            # Restarting the stream could replay data already handed to Qt.
            self._buffer.readyRead.emit()
        self._state_changed()

    @Slot()
    def _state_changed(self) -> None:
        sink = self._sink
        if sink is None:
            return
        self._observe_first_pcm()
        state = sink.state()
        if state not in {QtAudio.State.IdleState, QtAudio.State.StoppedState}:
            return
        current = self._current
        # IdleState only means the sink has no data to process right now. That
        # happens on an underrun as well as at true end of stream, so it must
        # never count as success on its own: compare what the sink actually
        # processed against the duration the PCM implies.
        processed_seconds = 0.0
        pcm_bytes_read = 0
        backend_error = "NoError"
        expected_seconds = 0.0
        elapsed_seconds = 0.0
        if current is not None:
            processed_seconds = self._processed_seconds(sink)
            backend_error = self._error_name(sink)
            expected_seconds = current.expected_seconds
            pcm_bytes_read = self._buffer.pos() if self._buffer is not None else 0
            if current.started_at:
                elapsed_seconds = max(0.0, self._clock() - current.started_at)
        input_drained = current is not None and pcm_bytes_read == len(current.audio.samples)
        waiting_for_refill = (
            current is not None and not input_drained and state == QtAudio.State.IdleState
            and backend_error in {"NoError", "UnderrunError"}
        )
        if waiting_for_refill:
            now = self._clock()
            if pcm_bytes_read != self._idle_position:
                self._idle_position, self._idle_since = pcm_bytes_read, now
                self._trace("sink_waiting_for_refill", current,
                            pcm_bytes_read=pcm_bytes_read, pcm_bytes=len(current.audio.samples),
                            processed_ms=processed_seconds * 1000)
            if now - self._idle_since < _REFILL_TIMEOUT:
                if not self._idle_timer.isActive():
                    self._idle_timer.start()
                return
        self._idle_timer.stop()
        self._pcm_probe.stop()
        errored = backend_error != "NoError"
        played_everything = (
            expected_seconds <= 0.0
            or processed_seconds >= expected_seconds * _COMPLETION_RATIO
        )
        completed = (
            current is not None
            and state == QtAudio.State.IdleState
            and not errored
            and input_drained
            and played_everything
        )
        if current is not None:
            if completed:
                result, reason = "completed", ""
            elif self._stop_requested:
                result, reason = "stopped", "session or user stop"
            elif waiting_for_refill:
                result, reason = (
                    "interrupted", f"Audio refill stalled after {pcm_bytes_read} of {len(current.audio.samples)} PCM bytes"
                )
            elif errored:
                result, reason = "interrupted", f"backend error {backend_error}"
            else:
                result, reason = (
                    "interrupted",
                    f"{state.name} after {processed_seconds:.2f}s of "
                    f"{expected_seconds:.2f}s of PCM",
                )
            self._record(
                current,
                processed_seconds=processed_seconds,
                elapsed_seconds=elapsed_seconds,
                terminal_state=state.name,
                backend_error=backend_error,
                result=result,
                reason=reason,
            )
            if result == "completed":
                self._completed_count += 1
            elif result == "interrupted":
                self._interrupted_count += 1
                logger.warning(
                    "Narrator audio playback interrupted request=%d %s "
                    "(processed=%.2fs expected=%.2fs elapsed=%.2fs state=%s "
                    "error=%s)",
                    current.request_id,
                    reason,
                    processed_seconds,
                    expected_seconds,
                    elapsed_seconds,
                    state.name,
                    backend_error,
                )
        if current is not None and (errored or waiting_for_refill):
            self._notify_failure(current, "Narrator audio playback failed")
        pending = self._pending
        self._pending = self._waiting.popleft() if self._waiting else None
        self._current = None
        sink = self._sink
        buffer = self._buffer
        self._sink = None
        self._buffer = None
        self._bytes = None
        if sink is not None:
            sink.stateChanged.disconnect(self._state_changed)
            sink.stop()
            sink.deleteLater()
        if buffer is not None:
            buffer.close()
            buffer.deleteLater()
        if completed and current is not None:
            self._notify_completed(current)
        while pending is not None:
            self._start(pending)
            if self._current is not None:
                break
            # A failed device/format must not strand the rest of the FIFO.
            pending = self._pending
            self._pending = self._waiting.popleft() if self._waiting else None

    @staticmethod
    def _similarity(previous: str, current: str) -> str:
        """Closeness of a replacement to what it replaced, for the log only.

        Uses difflib, which is already how the pipeline compares phrases. It is
        reported, never used to decide anything here.
        """

        if not previous or not current:
            return "n/a"
        ratio = SequenceMatcher(None, previous, current).ratio()
        return f"{ratio:.3f}"

    @staticmethod
    def _processed_seconds(sink: Any) -> float:
        """How much audio the backend actually processed."""

        try:
            return max(0.0, float(sink.processedUSecs()) / 1_000_000.0)
        except Exception:
            return 0.0

    @staticmethod
    def _error_name(sink: Any) -> str:
        try:
            error = sink.error()
        except Exception:
            return "NoError"
        if error == QtAudio.Error.NoError:
            return "NoError"
        return getattr(error, "name", str(error))

    def _record(
        self,
        playback: _Playback,
        *,
        processed_seconds: float,
        elapsed_seconds: float,
        terminal_state: str,
        backend_error: str,
        result: str,
        reason: str,
    ) -> None:
        audio = playback.audio
        width = _SAMPLE_WIDTHS.get(audio.sample_format.casefold(), 2)
        pcm_bytes_read = self._buffer.pos() if self._buffer is not None else 0
        self._trace("sink_terminal", playback, result=result, reason=reason,
                    pcm_frames=len(audio.samples) // (width * max(1, audio.channels)),
                    qt_processed_frames=round(processed_seconds * audio.sample_rate),
                    processed_ms=processed_seconds * 1000,
                    expected_ms=playback.expected_seconds * 1000,
                    pcm_bytes_read=pcm_bytes_read, pcm_bytes=len(audio.samples),
                    elapsed_ms=elapsed_seconds * 1000, terminal_state=terminal_state,
                    backend_error=backend_error, stop_requested=self._stop_requested)
        # One line per terminal event, carrying everything needed to attribute a
        # perceived interruption to the right cause.
        logger.debug(
            "Narrator playback event=%s request=%d stop_reason=%r "
            "played_ms=%.0f expected_ms=%.0f pcm_bytes_read=%d pcm_bytes=%d terminal_state=%s backend_error=%s "
            "stop_requested=%s text=%r",
            result,
            playback.request_id,
            reason,
            processed_seconds * 1000.0,
            playback.expected_seconds * 1000.0,
            pcm_bytes_read,
            len(audio.samples),
            terminal_state,
            backend_error,
            self._stop_requested,
            playback.text[:120],
        )
        self._history.append(
            PlaybackRecord(
                request_id=playback.request_id,
                pcm_bytes=len(audio.samples),
                pcm_bytes_read=pcm_bytes_read,
                sample_rate=audio.sample_rate,
                channels=audio.channels,
                sample_format=audio.sample_format,
                bytes_per_sample=width,
                expected_seconds=playback.expected_seconds,
                processed_seconds=processed_seconds,
                elapsed_seconds=elapsed_seconds,
                terminal_state=terminal_state,
                backend_error=backend_error,
                stop_requested=self._stop_requested,
                superseded=False,
                result=result,
                reason=reason,
            )
        )
        # Completion means all PCM played; terminal also includes underrun,
        # backend stop without an error, and explicit cancellation. Balancing
        # must release on all of those without treating them as successful speech.
        if playback.terminal_callback is not None:
            try:
                playback.terminal_callback()
            except Exception:
                logger.exception("Narrator terminal playback observer failed")

    def _notify_failure(self, playback: _Playback, message: str) -> None:
        self._trace("sink_failure", playback, message=message)
        try:
            playback.error_callback(message)
        except Exception:
            return

    @staticmethod
    def _notify_completed(playback: _Playback) -> None:
        try:
            playback.completed_callback()
        except Exception:
            return


__all__ = ["QtNarratorAudioOutput"]
