"""Qt backend event loop, managed via JSON stdin/stdout, without QML/game hooks."""
from __future__ import annotations
import json
import hashlib
import logging
import os
from pathlib import Path
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from itertools import count
from threading import Thread, Event
import time

from PySide6.QtCore import QObject, QTimer, Signal, Qt
from PySide6.QtGui import QGuiApplication
from game_optimization_linux import config
from game_optimization_linux.models.narrator import NormalizedRect
from game_optimization_linux.services.narrator_audio import QtNarratorAudioOutput
from game_optimization_linux.services.narrator_capture import PortalScreenCaptureProvider
from game_optimization_linux.services.narrator_portal import QtPortalScreenCastBackend
from game_optimization_linux.services.narrator_ocr import TesseractOcrProvider
from game_optimization_linux.services.narrator_rapidocr import RapidOcrProvider
from game_optimization_linux.services.narrator_translation import ArgosCTranslate2TranslationProvider
from game_optimization_linux.services.narrator_tts import PiperPolishTtsProvider, validate_piper_voice_config
from .pipeline import StandalonePipeline
from game_optimization_linux.services.narrator_persistence import CaptureGrantRepository
from game_optimization_linux.services.narrator_region import encode_region_preview
from .profiles import Profiles
from .protocol import Writer, decode, MAX_FRAME
from .components import ComponentManager
from .chatterbox_tts import CHATTERBOX_ID, ChatterboxTtsProvider
from .chatterbox_components import MODEL_COMPONENT as CHATTERBOX_MODEL, VULKAN_COMPONENT as CHATTERBOX_VULKAN
from .supertonic_tts import (COMPONENT_ID as SUPERTONIC_COMPONENT, SUPERTONIC_ID,
                            STYLE_FILENAME, VOICE_ID as SUPERTONIC_VOICE,
                            SupertonicTtsProvider, supertonic_component)
from .tts_router import OcrTtsProvider
from game_optimization_linux.services.narrator_components import DEFAULT_NARRATOR_COMPONENTS
from .seed import seed
from .quick_read import QuickRead, validate_options
from . import filechooser
from .runtime_checks import native_runtimes, tesseract_runtime
from .audio_balance import AudioBalance, list_targets, normalize_speech
from .experiments import ExperimentOptions, DialogueTrace
from .asr_panel import AsrPanel
from .asr_assets import ASR_MODEL_ID, ASR_RUNTIME_ID, asr_component_definitions
from .asr_process import asr_runtime_available, legacy_asr_requested, list_applications

class NoGameRequirement:
    def is_active(self, _profile_id): return True

class CaptureGate:
    """Keep portal open while editing/paused; never expose uncropped web capture."""
    def __init__(self, provider):
        self.provider, self.reading, self.frame = provider, False, None
        self.epoch = 0
    def __getattr__(self, name): return getattr(self.provider, name)
    def start(self, request, *, frame_callback, state_callback):
        self.frame = None
        self.epoch += 1
        epoch = self.epoch
        def receive(frame):
            if epoch != self.epoch: return
            self.frame = frame
            if self.reading: frame_callback(frame)
        def state_changed(state, message):
            if epoch == self.epoch: state_callback(state, message)
        self.provider.start(request, frame_callback=receive, state_callback=state_changed)
    def stop(self):
        self.epoch += 1
        self.reading = False
        self.frame = None
        self.provider.stop()

class AuxiliaryAudio:
    def __init__(self, gate):
        self.gate = gate

    def __getattr__(self, name):
        return getattr(self.gate, name)

    def play(self, pcm, **kwargs):
        self.gate.play_output(pcm, **kwargs)


class AudioGate:
    def __init__(self, audio, capture, balance=None):
        self.audio, self.capture, self.balance = audio, capture, balance
        self.auxiliary = AuxiliaryAudio(self)
        self.normalize = False
        self.record_pcm = None
        self.tokens = count(1)

    def __getattr__(self, name):
        return getattr(self.audio, name)

    def play(self, pcm, **kwargs):
        if self.capture.reading:
            pcm = normalize_speech(pcm) if self.normalize else pcm
            if self.record_pcm and pcm.provider_id == SUPERTONIC_ID:
                self.record_pcm(pcm, kwargs['request_id'])
            self.play_output(pcm, **kwargs)

    def play_output(self, pcm, **kwargs):
        if self.balance is not None:
            epoch, token = self.balance.epoch, next(self.tokens)
            completed, failed = kwargs['completed_callback'], kwargs['error_callback']
            terminal = kwargs.get('terminal_callback')
            first_pcm = kwargs.get('first_pcm_callback')
            released = False

            def on_first_pcm():
                if not released and self.balance.epoch == epoch and kwargs['volume'] > 0 and any(pcm.samples):
                    self.balance.started(epoch, token, request_id=kwargs['request_id'])
                    self.balance.tick(True)
                if first_pcm is not None:
                    first_pcm()

            def release():
                nonlocal released
                if not released:
                    released = True
                    if self.balance.epoch == epoch:
                        self.balance.finished(epoch, token)
                        self.balance.tick(True)

            def on_terminal():
                release()
                if terminal is not None:
                    terminal()

            def on_complete():
                release()
                completed()

            def on_error(message):
                release()
                if self.balance.epoch == epoch:
                    self.balance.reset()
                    self.balance.tick(False)
                failed(message)

            kwargs = {**kwargs, 'first_pcm_callback': on_first_pcm,
                      'completed_callback': on_complete, 'error_callback': on_error,
                      'terminal_callback': on_terminal}
        self.audio.play(pcm, **kwargs)

    def stop(self):
        if self.balance is not None:
            self.balance.reset()
            self.balance.tick(False)
        self.audio.stop()


def create_dialogue_trace(options, *, asr_diagnostics=False):
    if not options.trace_path:return None
    try:
        return DialogueTrace(options.trace_path,run_id=options.run_id)
    except OSError:
        if not asr_diagnostics:raise
        logging.getLogger(__name__).exception('ASR diagnostics unavailable; processing continues without trace')
        return None


class Backend(QObject):
    request = Signal(object)
    completed = Signal(object)
    def __init__(self, app, writer):
        super().__init__()
        self.app, self.writer = app, writer
        self.experiments = ExperimentOptions.from_environment()
        asr_diagnostics=os.environ.get('NARRATORVERSE_ASR_DIAGNOSTICS')=='1'
        self.trace = create_dialogue_trace(self.experiments,asr_diagnostics=asr_diagnostics)
        if asr_diagnostics and self.trace:
            try:
                paths=[Path(__file__),Path(__file__).with_name('asr_panel.py'),Path(__file__).with_name('asr_worker.py')]
                self.trace.emit('asr_diagnostic_runtime',pid=os.getpid(),
                                sources={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})
            except OSError:
                logging.getLogger(__name__).exception('ASR runtime fingerprint unavailable')
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix='narratorverse-adapter')
        seed()
        self.profiles = Profiles()
        self.capture = CaptureGate(PortalScreenCaptureProvider(QtPortalScreenCastBackend()))
        self.ocr = TesseractOcrProvider()
        self.rapid = RapidOcrProvider()
        self.argos = ArgosCTranslate2TranslationProvider()
        self.translator = self.argos
        if self.experiments.translation == 'quickmt':
            from .quickmt import QuickMTTranslationProvider
            directory = self.experiments.quickmt_dir or config.DATA_DIR / 'experiments/quickmt-en-pl'
            self.translator = QuickMTTranslationProvider(directory)
        self.tts = PiperPolishTtsProvider()
        root = Path(__file__).resolve().parents[2]
        self.chatterbox = ChatterboxTtsProvider(root / 'experiments/chatterbox-v3-vulkan',
            custom_dir=config.DATA_DIR / 'chatterbox-voices')
        self.supertonic = SupertonicTtsProvider(config.NARRATOR_COMPONENTS_DIR / SUPERTONIC_COMPONENT,
            config.DATA_DIR / 'supertonic3' / STYLE_FILENAME, runtime=root / '.runtime/supertonic3')
        self.ocr_tts = OcrTtsProvider(self.tts, self.chatterbox, self.supertonic)
        self.balance = AudioBalance(config.STATE_DIR)
        self.audio = AudioGate(QtNarratorAudioOutput(
            diagnostic=self.record_ocr_event,
            diagnostic_context=lambda: dict(self.pipeline.diagnostic_context(),
                origin='dialogue' if self.capture.reading else 'auxiliary')), self.capture, self.balance)
        if self.trace and os.environ.get('NARRATORVERSE_OCR_WAV') == '1':
            self.audio.record_pcm = lambda pcm, request: self.trace.emit(
                'ocr_wav', pcm=pcm.samples, channels=pcm.channels, sample_rate=pcm.sample_rate,
                phrase_id=request, **self.pipeline.diagnostic_context())
        self.quick = QuickRead(self.ocr_tts, self.audio.auxiliary, self.emit)
        self.asr_panel = AsrPanel(self.translator, self.ocr_tts, self.audio.auxiliary, self.emit,
                                 diagnostic=self.trace.emit if asr_diagnostics and self.trace else None)
        self.voice_job = False
        self.voice_job_generation = None
        self.voice_preview_provider = None
        self.voice_cancelled = Event()
        self.components_refreshing = False
        self.chatterbox_refreshing = False
        self.region_confirmed = False
        self.region_required = False
        self.pipeline = StandalonePipeline(self.capture, self.ocr, self.translator, self.ocr_tts, self.audio, NoGameRequirement(), ocr_providers=(self.rapid,),
            beta2_cooldown=self.experiments.beta2_cooldown,
            diagnostic=self.record_ocr_event,
            bypass_translation_cache=self.experiments.bypass_translation_cache)
        self.manager = ComponentManager(definitions=(*DEFAULT_NARRATOR_COMPONENTS,
            supertonic_component(), *asr_component_definitions()), progress=lambda size,total:
            self.emit('download', {'bytes': size, 'total': total, 'component': self.component_job}))
        self.component_job = ''
        self.component_action = ''
        self.component_list = []
        self.preview_pending = False
        self.preview_future = None
        self.closed = False
        self.voice_generation = 0
        self.preview_generation = 0
        self.request.connect(self.handle, Qt.ConnectionType.QueuedConnection)
        self.completed.connect(self.finish_job, Qt.ConnectionType.QueuedConnection)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(150)
        self.apply_ocr()
        self.refresh_components(check_chatterbox=True)

    def record_ocr_event(self, event, **data):
        if event in ('dialogue_dropped', 'audio_dropped') and self.ocr_tts.provider_id == SUPERTONIC_ID:
            logging.info('Supertonic 3 event=%s session=%s phrase_id=%s reason=%s',
                         event, data.get('session_id'), data.get('phrase_id'), data.get('reason'))
        if self.trace:
            self.trace.emit(event, **data)

    def emit(self, event, data):
        self.writer.send({'event': event, 'data': data})

    def async_job(self, request_id, fn, after=None, generation=None):
        future = self.pool.submit(fn)
        def done(f):
            try: result = {'id': request_id, 'result': f.result(), 'after': after}
            except Exception as error:
                logging.exception('Background operation failed')
                result = {'id': request_id, 'error': str(error), 'after': after}
            result['generation'] = generation
            self.completed.emit(result)
        future.add_done_callback(done)

    def finish_job(self, result):
        after = result.pop('after', None)
        generation = result.pop('generation', None)
        if after == 'voice' and generation == self.voice_generation and 'error' in result:
            self.voice_job = False
            self.voice_job_generation = None
            self.voice_preview_provider = None
        stale = self.closed or (after == 'voice' and generation != self.voice_generation) or (after == 'preview' and generation != self.preview_generation)
        if stale:
            if result.get('id') is not None:
                if after == 'voice' and not self.closed:
                    self.writer.send({'id': result['id'], 'result': False})
                else:
                    self.writer.send({'id': result['id'], 'error': 'Operacja anulowana'})
            return
        if after == 'components':
            component = self.component_job
            self.component_job = ''
            self.component_action = ''
            if 'error' in result: self.manager.failures[component] = result['error']
            else: self.manager.failures.pop(component, None)
            self.refresh_components()
        if after in ('component_status', 'chatterbox_status'):
            chatterbox_result = after == 'chatterbox_status'
            if chatterbox_result:
                self.chatterbox_refreshing = False
            else:
                self.components_refreshing = False
            if 'result' in result:
                chatterbox_ids = (CHATTERBOX_MODEL, CHATTERBOX_VULKAN)
                if chatterbox_result:
                    self.component_list = [row for row in self.component_list if row['component_id'] not in chatterbox_ids] + list(result['result'])
                else:
                    self.component_list = result['result'] + [row for row in self.component_list if row['component_id'] in chatterbox_ids]
                self.emit('components', self.component_list)
                self.emit('profiles', self.view())
        if after == 'preview': self.preview_future = None
        if after == 'voice' and 'result' in result:
            pcm, volume = result.pop('result')
            def finished(message=None):
                if generation != self.voice_generation: return
                self.voice_job = False
                self.voice_job_generation = None
                self.voice_preview_provider = None
                self.emit('voice_preview', {'state': 'error' if message else 'finished'})
                if message: self.emit('error', {'message': message})
            self.audio.auxiliary.play(pcm, volume=volume, request_id=0,
                started_callback=lambda *_: self.emit('voice_preview', {'state': 'playing'}),
                completed_callback=finished, error_callback=finished)
            result['result'] = True
        if after == 'voices':
            self.voice_job = False
            self.emit('profiles', self.view())
        if after == 'preview' and 'result' in result:
            self.emit('preview', result['result'])
        elif result.get('id') is not None: self.writer.send(result)
        elif 'error' in result: self.emit('error', {'message': result['error']})

    def view(self):
        def installed(voice):
            if not self.tts.voice_available(voice.voice_id): return False
            try:
                model, configuration = self.tts._voice_paths(voice)
                validate_piper_voice_config(configuration)
                if not model.stat().st_size: return False
                if not voice.custom:
                    component = next((c for c in self.component_list if c['component_id'] == voice.component_id), None)
                    return bool(component and component.get('installed'))
                return True
            except (OSError, ValueError): return False
        supertonic = next((component for component in self.component_list
                           if component['component_id'] == SUPERTONIC_COMPONENT), None)
        voices = [asdict(v) | {'installed': installed(v)} for v in self.tts.voices]
        chatterbox_component, chatterbox_issue = self.chatterbox.component_issue
        engines = [
            {'id': '', 'name': 'Piper (CPU)', 'description': 'Niskie wymagania. Szybki odczyt',
             'voices': voices, 'voice_key': 'voice_id',
             'ready': self.tts.available, 'default_voice': self.tts.default_voice_id, 'component': 'tts.voidek'},
            {'id': SUPERTONIC_ID, 'name': 'Supertonic 3 (CPU)',
             'description': 'Nieco wyższe wymagania. Bardziej naturalna barwa', 'voices': self.supertonic.voice_choices,
             'voice_key': 'supertonic_voice_id', 'default_voice': SUPERTONIC_VOICE,
             'ready': bool(supertonic and supertonic['installed']), 'component': SUPERTONIC_COMPONENT},
            {'id': CHATTERBOX_ID, 'name': 'Chatterbox (Vulkan GPU)',
             'description': 'Wysokie wymagania. Korzysta z karty graficznej także podczas gry',
             'voices': self.chatterbox.voice_choices,
             'voice_key': 'chatterbox_voice_id', 'default_voice': 'builtin', 'ready': not chatterbox_issue,
             'component': chatterbox_component, 'message': chatterbox_issue},
        ]
        return {**self.profiles.data, 'voices': voices, 'engines': engines, 'asr_options': self.profiles.asr_options(),
                'asr_components': [] if legacy_asr_requested() else [ASR_RUNTIME_ID, ASR_MODEL_ID],
                'paths': {'config': str(config.CONFIG_DIR), 'data': str(config.DATA_DIR), 'cache': str(config.CACHE_DIR), 'state': str(config.STATE_DIR)}}

    def refresh_components(self, *, check_chatterbox=False):
        if self.components_refreshing or self.component_job: return
        self.components_refreshing = True
        def refresh():
            native = native_runtimes()
            tesseract = tesseract_runtime(self.ocr.executable)
            runtime = {'capture.portal-pipewire': self.capture.capabilities().available,
                       'audio.qt-pcm': self.audio.available,
                       'ocr.english-local': tesseract and self.ocr.language_available('en'),
                       'ocr.polish-local': tesseract and self.ocr.language_available('pl'),
                       'ocr.rapidocr-local': native['rapidocr'] and self.rapid.available,
                       'translation.opus-en-pl': native['translation'] and self.argos.available,
                       'tts.voidek': native['piper'] and self.tts.voice_available('voidek-pl-e991'),
                       SUPERTONIC_COMPONENT: self.supertonic.runtime_available(),
                       ASR_RUNTIME_ID: asr_runtime_available(), ASR_MODEL_ID: True}
            return [self.manager.verified_status(c.component_id, runtime.get(c.component_id, False)) for c in self.manager.list_components()]
        self.async_job(None, refresh, 'component_status')
        if check_chatterbox and not self.chatterbox_refreshing:
            self.chatterbox_refreshing = True
            self.async_job(None, self.chatterbox.component_statuses, 'chatterbox_status')

    def apply_ocr(self):
        options = self.profiles.selected()['ocr']
        mode = options.get('mode', 'automatic')
        if mode not in ('automatic', 'classic'): raise ValueError('Unknown OCR mode')
        self.ocr.set_settings_mode(mode)

    def stop(self):
        self.cancel_voice_preview()
        self.preview_generation += 1
        self.preview_future = None
        self.capture.reading = False
        self.preview_pending = False
        self.pipeline.stop()
        self.chatterbox.cancel()
        self.supertonic.cancel()
        self.capture.stop()
        self.audio.stop()
        self.region_confirmed = False
        self.region_required = False

    def cancel_voice_preview(self):
        self.voice_generation += 1
        if self.voice_job_generation is not None:
            self.voice_job = False
            self.voice_job_generation = None
            provider, self.voice_preview_provider = self.voice_preview_provider, None
            if provider is not None:
                provider.cancel()
            self.audio.stop()

    def preview_voice(self, request_id, params):
        if self.pipeline.active or self.quick.busy or self.asr_panel.busy or self.component_job or self.voice_job:
            raise RuntimeError('Zatrzymaj bieżące czytanie, żeby posłuchać głosu.')
        mode = params.get('mode', 'ocr')
        settings = self.profiles.settings()
        if mode == 'ocr':
            options = settings.to_dict()
        elif mode == 'quick':
            options = validate_options({**self.quick.options, **(params.get('options') or {})})
        elif mode == 'asr':
            options = validate_options({**self.profiles.asr_options(), **(params.get('options') or {})})
        else:
            raise ValueError('Nieznany tryb odsłuchu.')
        provider, voice = self.ocr_tts.speech_selection(options)
        self.ocr_tts.require_voice(provider, voice)
        arguments = {'language': 'pl', 'voice_id': voice, 'speech_rate': options['speech_rate']}
        if provider is self.tts:
            arguments.update((key, options[key]) for key in ('noise_scale', 'noise_w_scale')
                             if options.get(key) is not None)
        if getattr(provider, 'supports_request_epochs', False) is True:
            arguments['expected_epoch'] = provider.request_epoch()
        self.balance.configure(settings)
        self.voice_job = True
        self.voice_preview_provider = provider
        self.voice_job_generation = self.voice_generation
        text = params.get('text') or 'Wieczorem wróciliśmy do domu nad rzeką.'

        def render():
            pcm = provider.synthesize(text, **arguments)
            if mode == 'ocr' and provider is self.tts and settings.normalize_narrator:
                pcm = normalize_speech(pcm)
            return pcm, options['volume']

        self.async_job(request_id, render, 'voice', self.voice_generation)

    def handle(self, request):
        request_id = request.get('id')
        try:
            method, p = request['method'], request.get('params', {})
            if (getattr(self, 'asr_panel', None) is not None and self.asr_panel.busy
                    and method in ('start','change_source','read','stop','pause','preview','quick_read','quick_settings',
                                   'voice_preview','voice_import','voice_remove','component',
                                   'profile_create','profile_select','profile_delete','profile_rename',
                                   'settings','reset','ocr_options','region')):
                raise RuntimeError('Najpierw zatrzymaj eksperymentalny ASR')
            if self.quick.busy and method in ('start','change_source','read','pause','stop','preview','region','profile_create','profile_select','profile_delete'):
                raise RuntimeError('Najpierw zatrzymaj Szybki odczyt. Sesja przechwytywania pozostaje bez zmian.')
            result = True
            if method == 'status':
                result = {'profiles': self.view(), 'components': self.component_list, 'session': self.session(), 'quick_read': self.quick.snapshot(), 'asr': self.asr_panel.snapshot()}
            elif method == 'asr_sources':
                self.async_job(request_id, list_applications); return
            elif method == 'asr_start':
                if self.pipeline.active or self.quick.busy or self.component_job or self.voice_job:
                    raise RuntimeError('Przed rozpoczęciem zakończ inne czytanie lub instalację komponentu.')
                options = self.profiles.asr_options()
                result = self.asr_panel.start(p['selection'], p.get('preset', options['preset']),
                                              p.get('tts_enabled', options['tts_enabled']), options)
                self.balance.configure(self.profiles.settings())
            elif method == 'asr_stop': result = self.asr_panel.stop()
            elif method == 'asr_tts':
                result = self.asr_panel.set_tts(p['enabled'])
                self.profiles.update_asr({'tts_enabled': p['enabled']})
            elif method == 'asr_settings':
                if self.asr_panel.busy or self.voice_job:
                    raise RuntimeError('Zatrzymaj czytanie przed zmianą ustawień.')
                self.ocr_tts.provider_for(p['values'].get('tts_provider_id', ''))
                result = self.profiles.update_asr(p['values'])
            elif method == 'quick_settings': result = self.quick.save(p['values'])
            elif method == 'audio_targets':
                self.async_job(request_id, list_targets); return
            elif method == 'quick_read':
                if self.voice_job: raise RuntimeError('Poczekaj na zakończenie odsłuchu lub importu głosu.')
                result = self.quick.start(p.get('text'), p.get('options', {}), live_reading=self.capture.reading, component_job=bool(self.component_job))
                self.balance.configure(self.profiles.settings())
            elif method == 'quick_stop': result = self.quick.stop()
            elif method == 'profile_create':
                self.stop(); result = self.profiles.create(p['name']); self.apply_ocr()
            elif method == 'profile_select':
                self.stop(); self.profiles.select(p['id']); self.apply_ocr()
            elif method == 'profile_rename':
                self.profiles.rename(p['name'])
            elif method == 'profile_delete':
                if len(self.profiles.data['profiles']) <= 1: raise ValueError('Zachowaj przynajmniej jeden profil')
                self.stop()
                key = self.profiles.selected()['id']
                self.profiles.delete()
                CaptureGrantRepository().save_token(key, '')
                self.apply_ocr()
            elif method == 'settings':
                if getattr(self, 'voice_job', False): raise RuntimeError('Najpierw zakończ import lub zatrzymaj odsłuch')
                if self.pipeline.active: raise RuntimeError('Zatrzymaj sesję przed zmianą ustawień')
                mode = p.get('ocr_mode', self.profiles.selected()['ocr'].get('mode', 'automatic'))
                if mode not in ('automatic', 'classic'): raise ValueError('Unknown OCR mode')
                values = p['values']
                if values.get('tts_provider_id', '') not in ('', PiperPolishTtsProvider.provider_id, CHATTERBOX_ID, SUPERTONIC_ID):
                    raise ValueError('Nieznany silnik mowy')
                self.profiles.update(values, ocr_mode=mode)
                if getattr(self, 'ocr_tts', None) is not None:
                    self.ocr_tts.cancel()
                    if self.ocr_tts.active is not self.supertonic and self.profiles.settings().tts_provider_id != SUPERTONIC_ID:
                        self.supertonic.cancel()
                self.apply_ocr()
            elif method == 'ocr_options':
                if self.pipeline.active: raise RuntimeError('Zatrzymaj sesję przed zmianą OCR')
                mode = p['mode']
                if mode not in ('automatic','classic'): raise ValueError('Unknown OCR mode')
                self.profiles.update({}, ocr_mode=mode); self.apply_ocr()
            elif method == 'reset':
                if getattr(self, 'voice_job', False): raise RuntimeError('Najpierw zakończ import lub zatrzymaj odsłuch')
                if self.pipeline.active: raise RuntimeError('Zatrzymaj sesję przed resetem')
                self.profiles.reset(p['section']); self.apply_ocr()
            elif method in ('start', 'change_source'):
                if getattr(self, 'voice_job', False): raise RuntimeError('Najpierw zakończ import lub zatrzymaj odsłuch')
                if self.quick.busy: raise RuntimeError('Najpierw zatrzymaj Szybki odczyt')
                if self.component_job: raise RuntimeError('Zaczekaj na zakończenie operacji komponentu')
                self.stop()
                self.region_required = True
                if method == 'change_source': CaptureGrantRepository().save_token(self.profiles.selected()['id'], '')
                if self.experiments.translation == 'quickmt' and not self.translator.available:
                    raise RuntimeError(self.translator.status_message)
                settings = self.experiments.session_settings(self.profiles.settings(), self.translator.provider_id)
                self.pipeline.start(settings)
                self.preview_pending = True
            elif method == 'read':
                if self.quick.busy: raise RuntimeError('Najpierw zatrzymaj Szybki odczyt')
                if not self.pipeline.active or self.capture.frame is None: raise RuntimeError('Najpierw wybierz źródło i zaczekaj na klatkę')
                if not self.region_confirmed: raise RuntimeError('Najpierw potwierdź obszar napisów w podglądzie')
                audio_settings = self.profiles.settings()
                self.balance.configure(audio_settings)
                self.audio.normalize = audio_settings.normalize_narrator and audio_settings.tts_provider_id not in (CHATTERBOX_ID, SUPERTONIC_ID)
                self.capture.reading = True
            elif method == 'pause':
                self.voice_generation += 1
                self.capture.reading = False
                self.pipeline.pause_processing()
            elif method == 'stop': self.stop()
            elif method == 'preview':
                self.capture.reading = False
                self.pipeline.pause_processing()
                self.preview_generation += 1
                self.preview_future = None
                self.preview_pending = True
            elif method == 'region':
                rect = NormalizedRect(**p['rect'])
                self.capture.reading = False
                self.pipeline.pause_processing()
                self.preview_generation += 1
                self.preview_future = None
                self.profiles.update({'subtitle_region': rect.to_dict()})
                self.region_confirmed = True
                self.region_required = False
                if self.pipeline.active:
                    # Region is immutable in settings; replace atomically without restarting portal/models.
                    with self.pipeline._lock:
                        self.pipeline._settings = replace(self.pipeline._settings, subtitle_region=rect, subtitle_region_source='saved')
                        self.pipeline._stabilizer.reset()
                self.preview_pending = False
            elif method == 'components_refresh': self.refresh_components(check_chatterbox=True)
            elif method == 'component_cancel': self.manager.cancelled.set()
            elif method == 'component':
                if self.quick.busy or self.voice_job or self.components_refreshing: raise RuntimeError('Poczekaj na zakończenie odczytu, importu lub sprawdzania komponentów')
                if self.component_job: raise RuntimeError('Inna operacja komponentu jest w toku')
                if self.pipeline.active: raise RuntimeError('Zatrzymaj lektora przed zmianą komponentów')
                component, action = p['component'], p['action']
                if component in (CHATTERBOX_MODEL, CHATTERBOX_VULKAN):
                    raise ValueError('Pliki Chatterbox są częścią paczki Flatpak. Przycisk Odśwież sprawdza ich stan i dostęp do Vulkan.')
                if component == SUPERTONIC_COMPONENT and action in ('install', 'repair', 'remove'):
                    self.supertonic.cancel()
                self.component_job = component
                self.component_action = action
                self.manager.cancelled.clear()
                self.emit('session', self.session())
                def work():
                    if action in ('install','repair'):
                        bundled = config.BUNDLED_NARRATOR_COMPONENTS_DIR
                        definition = self.manager._definition(component)
                        if definition.pinned_files:
                            self.manager.install_local(component, {item.target_relative_path: bundled / component / item.target_relative_path for item in definition.pinned_files})
                            (self.manager.root / component / '.narratorverse-origin').write_text('bundled\n')
                            return True
                        self.manager.install(component)
                        (self.manager.root / component / '.narratorverse-origin').write_text('downloaded\n')
                        return True
                    if action == 'verify': return self.manager.verify_installed(component)
                    if action == 'remove': return self.manager.remove(component)
                    raise ValueError('Unknown component action')
                self.async_job(request_id, work, 'components'); return
            elif method == 'voice_import':
                if self.pipeline.active or self.component_job or self.quick.busy or self.voice_job: raise RuntimeError('Zatrzymaj lektora, Szybki odczyt i operacje komponentów')
                engine = p.get('engine', '')
                provider = self.ocr_tts.provider_for(engine)
                if engine == SUPERTONIC_ID:
                    importer = filechooser.import_supertonic_from_chooser
                elif engine == CHATTERBOX_ID:
                    importer = filechooser.import_chatterbox_from_chooser
                else:
                    importer = filechooser.import_from_chooser
                self.voice_job = True
                self.voice_cancelled.clear()
                self.async_job(request_id, lambda: importer(provider, self.voice_cancelled), 'voices')
                return
            elif method == 'voice_remove':
                if self.pipeline.active or self.component_job or self.quick.busy or self.voice_job: raise RuntimeError('Zatrzymaj lektora, Szybki odczyt i operacje komponentów')
                self.voice_job = True
                self.async_job(request_id, lambda: self.tts.remove_custom_voice(p['id']), 'voices'); return
            elif method == 'voice_preview':
                self.preview_voice(request_id, p); return
            elif method == 'voice_preview_stop': self.cancel_voice_preview()
            elif method == 'shutdown':
                self.shutdown(); return
            else: raise ValueError('Unknown method: ' + method)
            self.writer.send({'id': request_id, 'result': result})
            if method.startswith('profile_') or method in ('settings','asr_settings','asr_tts','reset','ocr_options','region'):
                self.emit('profiles', self.view())
        except Exception as error:
            logging.exception('Request failed')
            self.writer.send({'id': request_id, 'error': str(error)})

    def session(self):
        snap = asdict(self.pipeline.snapshot)
        snap['audio_balance'] = self.balance.snapshot()
        state = snap.get('capture_state')
        failed = state in ('error', 'source_lost', 'permission_denied', 'restore_failed', 'unavailable', 'cancelled') or snap.get('status') == 'error'
        if failed: ui_state = 'failed'
        elif not self.pipeline.active: ui_state = 'empty'
        elif not self.capture.frame: ui_state = 'selecting_source' if state in ('permission_required', 'selecting_source') else 'starting'
        elif self.capture.reading: ui_state = 'reading'
        elif not self.region_confirmed: ui_state = 'selecting_region'
        else: ui_state = 'paused'
        return {'snapshot': snap, 'ui_state': ui_state, 'region_confirmed': self.region_confirmed, 'reading': self.capture.reading and not failed, 'active': self.pipeline.active, 'has_frame': self.capture.frame is not None and not failed, 'source': self.profiles.settings().capture_source, 'frames': self.capture.frames_received, 'variant': self.capture.negotiated_variant, 'restarts': self.capture.restarts, 'component_job': self.component_job, 'component_action': getattr(self, 'component_action', ''), 'components_refreshing': self.components_refreshing, 'voice_job': self.voice_job, 'voice_preview_job': self.voice_job_generation is not None}

    def tick(self):
        if (self.pipeline.snapshot.capture_state in ('source_lost', 'error', 'permission_denied', 'cancelled', 'restore_failed', 'unavailable')
                or self.pipeline.snapshot.status == 'error'):
            if self.capture.reading:
                self.capture.reading = False
                self.pipeline.pause_processing()
            self.region_confirmed = False
        self.balance.tick(self.capture.reading or self.quick.busy or self.asr_panel.busy
                          or self.voice_job_generation is not None)
        for event in self.pipeline.drain_events(): self.emit('narrator', asdict(event))
        self.emit('session', self.session())
        self.emit('components', self.component_list)
        if self.preview_pending and self.capture.frame and not self.preview_future:
            self.preview_pending = False
            frame = self.capture.frame
            self.preview_future = True
            self.async_job(None, lambda: encode_region_preview(frame).to_dict(), 'preview', self.preview_generation)

    def shutdown(self):
        if self.closed: return
        self.closed = True
        self.timer.stop()
        self.manager.cancelled.set()
        self.voice_cancelled.set()
        self.stop()
        self.balance.close()
        self.asr_panel.shutdown()
        self.quick.shutdown()
        self.pipeline.shutdown()
        for provider in (self.ocr, self.rapid, self.translator, self.tts, self.chatterbox, self.supertonic): provider.close()
        if self.argos is not self.translator: self.argos.close()
        self.capture.close()
        self.pool.shutdown(wait=False, cancel_futures=True)
        if self.trace: self.trace.close()
        self.app.quit()

def main():
    # Native libraries printing to fd 1 cannot corrupt the protocol.
    protocol_stream = os.fdopen(os.dup(sys.stdout.fileno()), 'w', buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    for p in (config.CONFIG_DIR, config.STATE_DIR, config.CACHE_DIR, config.DATA_DIR):
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
    app = QGuiApplication(['NarratorVerse-backend'])
    app.setApplicationName(config.APP_NAME)
    app.setOrganizationDomain(config.APP_ID)
    app.setDesktopFileName(config.APP_ID)
    backend = Backend(app, Writer(protocol_stream))
    def read():
        try:
            while True:
                line = sys.stdin.buffer.readline(MAX_FRAME + 1)
                if not line: break
                try: backend.request.emit(decode(line))
                except Exception as error: backend.emit('error', {'message': str(error)})
        finally: backend.request.emit({'method': 'shutdown'})
    Thread(target=read, daemon=True, name='narratorverse-ipc').start()
    signal.signal(signal.SIGTERM, lambda *_: backend.request.emit({'method': 'shutdown'}))
    signal.signal(signal.SIGINT, lambda *_: backend.request.emit({'method': 'shutdown'}))
    app.aboutToQuit.connect(backend.shutdown)
    backend.emit('ready', {'version': config.APP_VERSION})
    app.exec()

if __name__ == '__main__': main()
