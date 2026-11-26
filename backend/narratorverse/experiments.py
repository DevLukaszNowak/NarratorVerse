"""Developer-only process switches and opt-in, private dialogue JSONL traces."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import logging
import os
from pathlib import Path
from queue import Queue, Full, Empty
from threading import Thread, Lock, Event
import time
import wave
from uuid import uuid4


@dataclass(frozen=True)
class ExperimentOptions:
    beta2_cooldown: bool = False
    translation: str = 'argos'
    quickmt_dir: Path | None = None
    trace_path: Path | None = None
    bypass_translation_cache: bool = False
    run_id: str = ''

    def session_settings(self, settings, provider_id):
        # Never save an experimental provider into normal user profiles.
        return replace(settings, translation_provider_id=provider_id) if self.translation == 'quickmt' else settings

    @classmethod
    def from_environment(cls, environ=None):
        env = os.environ if environ is None else environ
        def choice(key, default, choices):
            value = env.get('NARRATORVERSE_EXPERIMENT_' + key, default)
            if value not in choices:
                raise ValueError(f'Invalid experimental {key}: {value!r}')
            return value
        dedup = choice('DEDUP', 'current', ('current', 'beta2-cooldown'))
        translation = choice('TRANSLATION', 'argos', ('argos', 'quickmt'))
        cache = choice('CACHE', 'normal', ('normal', 'bypass'))
        def absolute(key):
            value = env.get('NARRATORVERSE_EXPERIMENT_' + key, '')
            if not value:
                return None
            path = Path(value)
            if not path.is_absolute():
                raise ValueError(f'Experimental {key} must be an absolute path')
            return path
        return cls(dedup == 'beta2-cooldown', translation, absolute('QUICKMT_DIR'),
                   absolute('TRACE'), cache == 'bypass',
                   env.get('NARRATORVERSE_EXPERIMENT_RUN', '') or uuid4().hex)


class DialogueTrace:
    """Nonblocking producer; bounded writer queue, never on the inference path.

    Full text is intentional and opt-in. Existing files/symlinks are refused.
    Trace loss is counted and reported, never silently labelled a complete run.
    """
    def __init__(self, path, *, run_id='', clock=time.monotonic):
        self.run_id = run_id or uuid4().hex
        self.clock = clock
        self.dropped = 0
        self.error = ''
        self._queue = Queue(maxsize=4096)
        self._closed = False
        self._producer_lock = Lock()
        self._stop = Event()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._directory = Path(path).parent
        self._stream = os.fdopen(fd, 'w', encoding='utf-8')
        self._thread = Thread(target=self._write, daemon=True, name='narratorverse-trace')
        self._thread.start()

    def emit(self, event, **fields):
        record = dict(fields, event=event, monotonic=self.clock(), run_id=self.run_id)
        # Serialize only enqueue/close, never file I/O. Nothing may be enqueued
        # after shutdown has established the end of this run.
        with self._producer_lock:
            if self._closed:
                return
            try:
                self._queue.put_nowait(record)
            except Full:
                self.dropped += 1

    def _write(self):
        try:
            while True:
                try:
                    record = self._queue.get(timeout=0.1)
                except Empty:
                    # A producer may enqueue between get() timing out and
                    # close() setting stop. After stop no producer can enqueue.
                    if self._stop.is_set() and self._queue.empty():
                        break
                    continue
                if record['event'] == 'ocr_wav':
                    pcm = record.pop('pcm')
                    name = f"{record['session_id']}-{record['phrase_id']}.wav"
                    try:
                        with (self._directory / name).open('xb') as output, wave.open(output, 'wb') as wav:
                            wav.setparams((record['channels'], 2, record['sample_rate'], 0, 'NONE', ''))
                            wav.writeframes(pcm)
                        record['file'] = name
                        record['frames'] = len(pcm) // (2 * record['channels'])
                    except OSError as error:
                        record['error'] = str(error)
                self._stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                self._stream.flush()
            self._stream.write(json.dumps(dict(event='trace_closed', run_id=self.run_id,
                                   dropped_events=self.dropped, monotonic=self.clock())) + '\n')
        except (OSError, ValueError, TypeError) as error:
            self.error = str(error)
            logging.getLogger(__name__).error('Experimental trace failed: %s', error)
        finally:
            self._stream.close()

    def close(self):
        with self._producer_lock:
            self._closed = True
            self._stop.set()
        # A full queue must not prevent shutdown signalling. The writer drains
        # it even when this bounded wait expires; no sentinel can be lost.
        self._thread.join(timeout=2)
        if self._thread.is_alive() or self.dropped or self.error:
            logging.getLogger(__name__).warning(
                'Experimental trace incomplete: dropped=%d error=%s writer_alive=%s',
                self.dropped, self.error, self._thread.is_alive())