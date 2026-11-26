"""Resident CPU Supertonic provider."""
import base64
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import re
import shutil
import subprocess
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Lock, Thread
from time import monotonic

from game_optimization_linux.models.narrator import PcmAudio
from game_optimization_linux.services.narrator_components import (
    NarratorComponentArtifact, NarratorComponentDefinition,
)
from .audio_tempo import stretch_pcm
from .protocol import MAX_FRAME


SUPERTONIC_ID = 'supertonic3-cpu'
COMPONENT_ID = 'tts.supertonic3'
VOICE_ID = 'M5-02'
STYLE_FILENAME = 'Supertonic3-M5-02.json'
STYLE_SHA256 = '35fcc0a9f65eb7e693a2aac17b30d6b9ce8cf5bb21953190cf08bc12d2ab15a0'
BUNDLED_STYLE = Path(__file__).with_name('assets') / 'supertonic' / STYLE_FILENAME
CUSTOM_ID = re.compile(r'supertonic-[0-9a-f]{24}')
MAX_STYLE_BYTES = 1024 * 1024
ASSETS = json.loads(Path(__file__).with_name('supertonic_assets.json').read_text())
RATE = 44100


class _InvalidWaveform(RuntimeError):
    pass


def supertonic_component():
    revision = ASSETS['revision']
    base = f"https://huggingface.co/{ASSETS['repository']}/resolve/{revision}"
    files = ASSETS['files']
    return NarratorComponentDefinition(
        component_id=COMPONENT_ID, kind='tts', name='Supertonic 3 (CPU)',
        license_id='OpenRAIL-M', artifact_license_id='OpenRAIL-M',
        attribution='Supertone · Supertone/supertonic-3', version=revision,
        download_size_bytes=sum(entry['size'] for entry in files.values()),
        install_ready=True,
        artifacts=tuple(NarratorComponentArtifact(
            f'{base}/{name}', entry['sha256'], entry['size'], name,
        ) for name, entry in files.items()),
    )


def file_matches(path, entry, *, verify_hash=False):
    try:
        if path.stat().st_size != entry['size'] or not path.is_file():
            return False
        if verify_hash:
            with path.open('rb') as file:
                return hashlib.file_digest(file, 'sha256').hexdigest() == entry['sha256']
        return True
    except OSError:
        return False


def _style_is_approved(path):
    try:
        with path.open('rb') as file:
            return hashlib.sha256(file.read(MAX_STYLE_BYTES + 1)).hexdigest() == STYLE_SHA256
    except OSError:
        return False


def _validate_style(data):
    def flatten(value):
        if isinstance(value, list):
            for item in value:
                yield from flatten(item)
        else:
            yield value

    try:
        style = json.loads(data)
        for key, dimensions in (('style_ttl', [1, 50, 256]), ('style_dp', [1, 8, 16])):
            item = style[key]
            values = list(flatten(item['data']))
            if (item['dims'] != dimensions or len(values) != math.prod(dimensions)
                    or any(type(v) not in (int, float) or not -3.4028234e38 <= v <= 3.4028234e38 for v in values)):
                raise ValueError('Nieprawidłowe wymiary lub wartości stylu')
        # SDK constructs a NumPy array before reshaping. Verify nested data is rectangular.
        import numpy as np
        for key in ('style_ttl', 'style_dp'):
            np.asarray(style[key]['data'], dtype=np.float32).reshape(style[key]['dims'])
    except (UnicodeError, LookupError, TypeError, ValueError, RecursionError) as error:
        raise ValueError('Wybierz poprawny JSON ze stylem Supertonic 3, np. M5-02.') from error


class SupertonicTtsProvider:
    provider_id = SUPERTONIC_ID
    default_voice_id = VOICE_ID
    narration_queue_limit = 1
    narration_max_age_s = 8.0
    supports_request_epochs = True

    def __init__(self, model, style, *, runtime=None, bundled_style=BUNDLED_STYLE, popen=subprocess.Popen):
        self.model, self.style = Path(model), Path(style)
        self.bundled_style = Path(bundled_style)
        self.custom_dir = self.style.parent / 'voices'
        self.runtime = Path(runtime) if runtime is not None else None
        self._popen = popen
        self._state = Lock()
        self._serial = Lock()
        self._epoch = 0
        self._process = None
        self._process_voice = None
        self._responses = None
        self.model_prepare_reused = None

    def runtime_available(self):
        if self.runtime is not None and self.runtime.is_dir():
            return all((self.runtime / name / '__init__.py').is_file()
                       for name in ('supertonic', 'onnxruntime', 'numpy'))
        return importlib.util.find_spec('supertonic') is not None

    def model_present(self):
        return all(file_matches(self.model / name, item) for name, item in ASSETS['files'].items())

    @property
    def style_available(self):
        return self._style_path(VOICE_ID) is not None

    def _style_path(self, voice_id):
        if voice_id == VOICE_ID:
            return next((p for p in (self.bundled_style, self.style) if _style_is_approved(p)), None)
        if CUSTOM_ID.fullmatch(voice_id):
            path = self.custom_dir / voice_id / 'style.json'
            if path.is_file():
                return path
        return None

    @property
    def available(self):
        return self.runtime_available() and self.model_present() and self.style_available

    @property
    def available_voice_ids(self):
        return tuple(row['voice_id'] for row in self.voice_choices if row['installed'])

    @property
    def voice_choices(self):
        ready = self.runtime_available() and self.model_present()
        choices = [{'voice_id': VOICE_ID, 'name': VOICE_ID, 'custom': False,
                    'installed': ready and self.style_available}]
        for entry in sorted(self.custom_dir.glob('supertonic-*')):
            if not CUSTOM_ID.fullmatch(entry.name) or not (entry / 'style.json').is_file():
                continue
            try:
                name = (entry / 'name').read_text(encoding='utf-8')
            except (OSError, UnicodeError):
                continue
            choices.append({'voice_id': entry.name, 'name': name, 'custom': True, 'installed': ready})
        return choices

    def import_style(self, source, cancelled=None):
        source = Path(source)
        with source.open('rb') as file:
            data = file.read(MAX_STYLE_BYTES + 1)
        if len(data) > MAX_STYLE_BYTES:
            raise ValueError('Plik stylu Supertonic 3 może mieć najwyżej 1 MB.')
        digest = hashlib.sha256(data).hexdigest()
        if digest != STYLE_SHA256:
            _validate_style(data)
            if cancelled is not None and cancelled.is_set():
                return None
            voice_id = 'supertonic-' + digest[:24]
            self.custom_dir.mkdir(parents=True, exist_ok=True)
            destination = self.custom_dir / voice_id
            with TemporaryDirectory(dir=self.custom_dir) as staging:
                staged = Path(staging) / voice_id
                staged.mkdir()
                (staged / 'style.json').write_bytes(data)
                (staged / 'name').write_text(source.stem, encoding='utf-8')
                with self._state:
                    if cancelled is not None and cancelled.is_set():
                        return None
                    if not destination.exists():
                        staged.rename(destination)
            return voice_id
        if cancelled is not None and cancelled.is_set():
            return None
        self.style.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=self.style.parent, delete=False) as staging:
            temporary = Path(staging.name)
        try:
            temporary.write_bytes(data)
            with self._state:
                if cancelled is not None and cancelled.is_set():
                    return None
                temporary.replace(self.style)
        finally:
            temporary.unlink(missing_ok=True)
        return VOICE_ID

    def remove_custom_voice(self, voice_id):
        if not CUSTOM_ID.fullmatch(voice_id):
            raise ValueError('Można usunąć tylko zaimportowany głos Supertonic 3.')
        self.cancel()
        shutil.rmtree(self.custom_dir / voice_id)

    def require_component(self, voice_id):
        if voice_id != VOICE_ID and not CUSTOM_ID.fullmatch(voice_id):
            raise ValueError('Nieznany głos Supertonic 3')
        if not self.runtime_available():
            raise RuntimeError('Środowisko Supertonic 3 jest niedostępne. Zaktualizuj aplikację.')
        if not self.model_present():
            raise RuntimeError('Zainstaluj model Supertonic 3 w Komponentach.')
        path = self._style_path(voice_id)
        if path is None:
            if voice_id == VOICE_ID:
                raise RuntimeError(f'Brakuje {STYLE_FILENAME}. Zaktualizuj aplikację.')
            raise RuntimeError('Wybrany głos Supertonic 3 został usunięty. Wybierz dostępny głos.')
        return path

    def _verify_model(self):
        for name, entry in ASSETS['files'].items():
            if not file_matches(self.model / name, entry, verify_hash=True):
                raise RuntimeError(f'Plik {name} jest uszkodzony. Napraw Supertonic 3 w Komponentach.')

    def request_epoch(self):
        with self._state:
            return self._epoch

    def _check_epoch(self, expected):
        if self._epoch != expected:
            raise RuntimeError('Synteza Supertonic 3 została anulowana.')

    @staticmethod
    def _read(process, responses):
        try:
            with process.stdout as stream:
                while line := stream.readline(MAX_FRAME + 1):
                    if len(line) > MAX_FRAME or not line.endswith(b'\n'):
                        break
                    responses.put(line)
        finally:
            responses.put(None)

    def _response(self, responses):
        try:
            line = responses.get(timeout=60)
        except Empty as error:
            raise RuntimeError('Supertonic 3 przekroczył czas oczekiwania.') from error
        if line is None:
            raise RuntimeError('Proces Supertonic 3 zakończył pracę bez poprawnej odpowiedzi.')
        reply = json.loads(line)
        if reply.get('error'):
            if reply.get('invalid_audio') is True:
                raise _InvalidWaveform(reply['error'])
            raise RuntimeError(reply['error'])
        return reply

    @staticmethod
    def _terminate(process):
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass

    def cancel(self):
        with self._state:
            self._epoch += 1
            process = self._process
            self._process = self._responses = self._process_voice = None
        self._terminate(process)

    close = cancel

    def _discard(self, process):
        with self._state:
            if process is not self._process:
                return
            self._process = self._responses = self._process_voice = None
        self._terminate(process)

    def _start(self, voice_id, epoch):
        with self._state:
            self._check_epoch(epoch)
            process, responses, running_voice = self._process, self._responses, self._process_voice
        if running_voice == voice_id and process is not None and process.poll() is None:
            return process, responses, None
        if process is not None:
            self._discard(process)
        style = self.require_component(voice_id)
        started = monotonic()
        self._verify_model()
        env = dict(os.environ, OMP_NUM_THREADS='2', OPENBLAS_NUM_THREADS='1', HF_HUB_OFFLINE='1')
        if self.runtime is not None and self.runtime.is_dir():
            env['PYTHONPATH'] = os.pathsep.join(filter(None, (str(self.runtime), env.get('PYTHONPATH'))))
        with self._state:
            self._check_epoch(epoch)
            process = self._popen([sys.executable, '-u', str(Path(__file__).with_name('supertonic_worker.py')),
                                   str(self.model), str(style)], env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            self._process = process
            self._process_voice = voice_id
            self._responses = responses = SimpleQueue()
            Thread(target=self._read, args=(process, responses), daemon=True).start()
        try:
            if not self._response(responses).get('ready'):
                raise RuntimeError('Supertonic 3 nie zgłosił gotowości.')
        except Exception:
            self._discard(process)
            with self._state:
                self._check_epoch(epoch)
            raise
        with self._state:
            self._check_epoch(epoch)
        return process, responses, (monotonic() - started) * 1000

    def prepare(self, voice_id, *, expected_epoch=None):
        epoch = self.request_epoch() if expected_epoch is None else expected_epoch
        with self._serial:
            _, _, startup_ms = self._start(voice_id, epoch)
            self.model_prepare_reused = startup_ms is None

    def synthesize(self, text, *, language, voice_id, speech_rate, expected_epoch=None,
                   noise_scale=None, noise_w_scale=None):
        if language.lower() not in ('pl', 'pl-pl'):
            raise ValueError('Supertonic 3 obsługuje tu wyłącznie polski.')
        if not 0.5 <= speech_rate <= 2.0:
            raise ValueError('Tempo musi mieścić się między 0,5 a 2.')
        epoch = self.request_epoch() if expected_epoch is None else expected_epoch
        started = monotonic()
        with self._serial:
            process, responses, startup_ms = self._start(voice_id, epoch)
            sent = monotonic()
            try:
                process.stdin.write((json.dumps({'text': text}) + '\n').encode())
                process.stdin.flush()
                reply = self._response(responses)
                pcm = base64.b64decode(reply['pcm'], validate=True)
                if not pcm or len(pcm) % 2:
                    raise RuntimeError('Supertonic 3 zwrócił nieprawidłowe audio.')
                if reply['native_speech_rate'] != 1.0:
                    raise RuntimeError('Nieprawidłowe tempo syntezy Supertonic 3.')
                pcm = stretch_pcm(pcm, RATE, speech_rate)
            except _InvalidWaveform:
                with self._state:
                    self._check_epoch(epoch)
                raise
            except Exception:
                self._discard(process)
                with self._state:
                    self._check_epoch(epoch)
                raise
            with self._state:
                self._check_epoch(epoch)
            finished = monotonic()
        return PcmAudio(pcm, RATE, 1, provider_id=self.provider_id,
                        elapsed_ms=(finished - started) * 1000,
                        worker_roundtrip_ms=(finished - sent) * 1000,
                        inference_ms=reply['inference_ms'], serialization_ms=reply['serialization_ms'],
                        worker_startup_ms=startup_ms, worker_reused=startup_ms is None,
                        audio_duration_ms=len(pcm) * 1000 / (RATE * 2), pending_limit=1,
                        native_speech_rate=1.0)
