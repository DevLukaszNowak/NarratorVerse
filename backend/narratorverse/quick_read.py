"""Direct text-to-speech adapter. No capture, OCR, translation or profile writes."""
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, Signal, Qt
from game_optimization_linux.config import CONFIG_DIR
from game_optimization_linux.services.narrator_persistence import _atomic_json_write
from game_optimization_linux.services.narrator_tts import PIPER_DEFAULT_VOICE_ID, PIPER_PROVIDER_ID
from .chatterbox_tts import CHATTERBOX_ID, CUSTOM_ID as CHATTERBOX_CUSTOM_ID
from .supertonic_tts import COMPONENT_ID as SUPERTONIC_COMPONENT, SUPERTONIC_ID, VOICE_ID, CUSTOM_ID as SUPERTONIC_CUSTOM_ID
from .tts_router import OcrTtsProvider, provider_epoch, resolve_speech, synthesis_options

EXAMPLE = 'Cześć! Jestem Voidek. Wklej tekst, a przeczytam go na głos.'
DEFAULTS = {'tts_provider_id': PIPER_PROVIDER_ID, 'voice_id': PIPER_DEFAULT_VOICE_ID,
            'chatterbox_voice_id': 'builtin', 'supertonic_voice_id': VOICE_ID,
            'speech_rate': 1.0, 'volume': .85, 'noise_scale': None, 'noise_w_scale': None}
MAX_TEXT = 10000


def validate_options(values):
    result = {**DEFAULTS, **values}
    for key, low, high in (('speech_rate', .5, 2), ('volume', 0, 1)):
        value = result[key]
        if isinstance(value, bool): raise ValueError('Nieprawidłowe tempo lub głośność')
        value = float(value)
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError('Tempo: 0,5–2; głośność: 0–1')
        result[key] = value
    for key in ('noise_scale', 'noise_w_scale'):
        value = result[key]
        if value is not None:
            if type(value) not in (int, float) or not 0 <= value <= 2:
                raise ValueError('Zmienność głosu musi mieścić się między 0 a 2.')
            result[key] = float(value)
    for key in ('voice_id', 'chatterbox_voice_id', 'supertonic_voice_id'):
        value = result[key]
        if not isinstance(value, str) or len(value) > 128 or any(c in value for c in '\r\n\0'):
            raise ValueError('Wybierz głos')
        result[key] = value or DEFAULTS[key]
    if result['chatterbox_voice_id'] not in ('02', 'builtin') and not CHATTERBOX_CUSTOM_ID.fullmatch(result['chatterbox_voice_id']):
        raise ValueError('Wybierz głos Chatterbox')
    if result['supertonic_voice_id'] != VOICE_ID and not SUPERTONIC_CUSTOM_ID.fullmatch(result['supertonic_voice_id']):
        raise ValueError('Wybierz głos Supertonic 3')
    if result['tts_provider_id'] not in ('', PIPER_PROVIDER_ID, CHATTERBOX_ID, SUPERTONIC_ID):
        raise ValueError('Wybierz silnik mowy')
    result['tts_provider_id'] = result['tts_provider_id'] or PIPER_PROVIDER_ID
    return {key: result[key] for key in DEFAULTS}


def chunks(text):
    if not isinstance(text, str) or not text.strip(): raise ValueError('Wpisz tekst do przeczytania')
    if len(text) > MAX_TEXT: raise ValueError('Tekst może mieć najwyżej 10 000 znaków')
    words = re.findall(r'\S+', text)
    parts, current = [], ''
    for word in words:
        # Bound even a single enormous word; do not allocate minutes of PCM at once.
        for offset in range(0, len(word), 350):
            piece = word[offset:offset + 350]
            if current and len(current) + len(piece) + 1 > 350:
                parts.append(current); current = ''
            current = (current + ' ' + piece).strip()
    if current: parts.append(current)
    return parts


class QuickRead(QObject):
    completed = Signal(object)

    def __init__(self, tts, audio, emit, path=CONFIG_DIR / 'quick-read.json'):
        super().__init__()
        self.tts, self.audio, self.emit, self.path = tts, audio, emit, Path(path)
        self.options = dict(DEFAULTS)
        if self.path.exists(): self.options = validate_options(json.loads(self.path.read_text()))
        self.state, self.error, self.required = 'idle', '', ''
        self.generation, self.index, self.parts = 0, 0, []
        self.provider, self.voice_id = None, ''
        self.cancelled = Event()
        self.closed = False
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='narratorverse-quick')
        self.completed.connect(self._finished, Qt.ConnectionType.QueuedConnection)

    @property
    def busy(self): return self.state in ('synthesizing', 'playing', 'stopping')

    def snapshot(self):
        return {'state': self.state, 'error': self.error, 'required_component': self.required,
                'options': dict(self.options), 'part': min(self.index + 1, len(self.parts)), 'parts': len(self.parts)}

    def notify(self): self.emit('quick_read', self.snapshot())

    def save(self, values):
        if self.busy: raise RuntimeError('Zatrzymaj odczyt przed zmianą ustawień')
        options = validate_options({**self.options, **values})
        _atomic_json_write(self.path, options)
        self.options = options
        self.notify()
        return self.snapshot()

    def start(self, text, options, *, live_reading=False, component_job=False):
        if self.closed: raise RuntimeError('Silnik został zamknięty')
        if self.busy: raise RuntimeError('Trwa odczyt. Najpierw naciśnij Stop.')
        if live_reading: raise RuntimeError('Wstrzymaj czytanie napisów przed Szybkim odczytem. Źródło może pozostać otwarte.')
        if component_job: raise RuntimeError('Poczekaj na zakończenie operacji komponentu')
        parts = chunks(text)
        selected = validate_options({**self.options, **options})
        provider, voice = resolve_speech(self.tts, selected)
        try:
            OcrTtsProvider.require_voice(provider, voice)
        except RuntimeError as error:
            self.state = 'error'
            self.required = {SUPERTONIC_ID: SUPERTONIC_COMPONENT, CHATTERBOX_ID: 'tts.chatterbox'}.get(
                selected['tts_provider_id'], 'tts.voidek' if voice == DEFAULTS['voice_id'] else 'custom-voice')
            self.error = str(error)
            self.notify()
            raise
        if not self.audio.available:
            self.state, self.required, self.error = 'error', 'audio.qt-pcm', 'Brak urządzenia wyjścia dźwięku. Sprawdź ustawienia dźwięku systemu.'
            self.notify(); raise RuntimeError(self.error)
        self.save(selected)
        self.generation += 1
        self.cancelled = Event()
        self.provider, self.voice_id = provider, voice
        self.parts, self.index, self.error, self.required = parts, 0, '', ''
        self._synthesize()
        return self.snapshot()

    def _synthesize(self):
        self.state = 'synthesizing'; self.notify()
        generation, cancelled, options = self.generation, self.cancelled, dict(self.options)
        text = self.parts[self.index]
        provider, voice_id = self.provider, self.voice_id
        arguments = synthesis_options(provider, options, voice_id, provider_epoch(provider))
        def work():
            if cancelled.is_set(): return None
            return provider.synthesize(text, **arguments)
        future = self.pool.submit(work)
        def done(f):
            try: payload = {'generation': generation, 'pcm': f.result(), 'volume': options['volume']}
            except Exception as error: payload = {'generation': generation, 'error': str(error)}
            self.completed.emit(payload)
        future.add_done_callback(done)

    def _finished(self, result):
        if self.closed or result['generation'] != self.generation: return
        if result.get('stopped'):
            self.state = 'stopped'; self.notify(); return
        if result.get('error'):
            self.provider.close()
            self.state, self.error = 'error', result['error']; self.notify(); return
        pcm = result.get('pcm')
        if pcm is None: return
        generation = self.generation
        self.audio.play(pcm, volume=result['volume'], request_id=generation,
                        started_callback=lambda *_: self._playing(generation),
                        completed_callback=lambda: self._next(generation),
                        error_callback=lambda message: self._audio_error(generation, message))

    def _playing(self, generation):
        if generation != self.generation: return
        self.state = 'playing'; self.notify()

    def _next(self, generation):
        if generation != self.generation: return
        self.index += 1
        if self.index < len(self.parts): self._synthesize()
        else:
            self.provider.close()
            self.state = 'finished'; self.notify()

    def _audio_error(self, generation, message):
        if generation != self.generation: return
        self.provider.close()
        self.state, self.error = 'error', message; self.notify()

    def stop(self):
        if not self.busy: return self.snapshot()
        self.generation += 1; generation = self.generation
        self.cancelled.set()
        self.state = 'stopping'; self.notify()
        self.audio.stop()
        self.provider.cancel()
        future = self.pool.submit(self.provider.close)
        future.add_done_callback(lambda _: self.completed.emit({'generation': generation, 'stopped': True}))
        return self.snapshot()

    def shutdown(self):
        self.stop(); self.closed = True
        self.pool.shutdown(wait=True, cancel_futures=False)
