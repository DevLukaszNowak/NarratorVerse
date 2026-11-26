"""Resident Vulkan Chatterbox Multilingual V3 provider."""
import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile

from game_optimization_linux.models.narrator import PcmAudio
from .audio_tempo import stretch_pcm
from .chatterbox_components import MODEL_COMPONENT, chatterbox_component_statuses


CHATTERBOX_ID = "chatterbox-mtl-v3-vulkan"
CUSTOM_ID = re.compile(r"chatterbox-[0-9a-f]{24}")
REFERENCE_FILES = ("embedding.npy", "prompt_token.npy", "prompt_feat.npy",
                   "speaker_emb.npy", "cond_prompt_speech_tokens.npy")
RATE = 24000
logger = logging.getLogger(__name__)


class ChatterboxComponentMissing(RuntimeError):
    pass


def _validate_reference(source):
    try:
        with wave.open(str(source), 'rb') as wav:
            if (wav.getnchannels() not in (1, 2) or wav.getsampwidth() not in (1, 2, 3, 4)
                    or not 8000 <= wav.getframerate() <= 192000):
                raise ValueError('Wybierz próbkę WAV PCM w mono lub stereo.')
            duration = wav.getnframes() / wav.getframerate()
            if not 5 < duration <= 60:
                raise ValueError('Próbka WAV musi mieć ponad 5 s i najwyżej 60 s.')
            wav.setpos(wav.getnframes() - 1)
            if len(wav.readframes(1)) != wav.getnchannels() * wav.getsampwidth():
                raise ValueError('Plik WAV jest niekompletny.')
    except (wave.Error, EOFError) as error:
        raise ValueError('Wybierz lokalną próbkę w formacie WAV PCM.') from error


def _validate_conditioning(path):
    import numpy as np

    try:
        for name in REFERENCE_FILES:
            tensor = path / name
            if not 128 < tensor.stat().st_size <= 1024 * 1024:
                raise ValueError(name)
            values = np.load(tensor, allow_pickle=False)
            if name == 'embedding.npy':
                valid = values.shape == (192,) and values.dtype == np.float32
            elif name == 'speaker_emb.npy':
                valid = values.shape == (256,) and values.dtype == np.float32
            elif name == 'prompt_feat.npy':
                valid = values.ndim == 2 and values.shape[1] == 80 and values.dtype == np.float32
            else:
                valid = values.ndim == 1 and values.dtype == np.int32 and (values >= 0).all()
            if not valid or not values.size or not np.isfinite(values).all():
                raise ValueError(name)
    except (OSError, ValueError, EOFError) as error:
        raise RuntimeError('Chatterbox nie przygotował kompletu parametrów głosu z próbki.') from error


class ChatterboxTtsProvider:
    provider_id = CHATTERBOX_ID
    supports_request_epochs = True
    default_voice_id = "builtin"
    narration_queue_limit = 1
    narration_max_age_s = 8.0
    audio_queue_limit = 1

    def __init__(self, root, popen=subprocess.Popen, *, custom_dir=None):
        self.root = Path(root)
        self.binary = self.root / "bin/tts-cli"
        self.model = self.root / "models/t3-v3-q8_0.gguf"
        self.s3gen = self.root / "models/s3gen-mtl-q8_0.gguf"
        self.reference = self.root / "reference/02-conditioning"
        self.custom_dir = Path(custom_dir) if custom_dir is not None else None
        self._popen = popen
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._synthesis_lock = threading.Lock()
        self._process = None
        self._process_voice = None
        self.model_prepare_reused = None
        self._epoch = 0
        self._ready_event = threading.Event()
        self._request_event = threading.Event()
        self._pcm = bytearray()
        self._tail = []
        self._error = None
        self._ready = False
        self._pcm_last_update = None
        self._components = ()
        self._verified_files = {}

    def component_statuses(self):
        self._components = chatterbox_component_statuses(self.root, self._environment(), self._verified_files)
        return self._components

    @property
    def component_issue(self):
        if not self._components:
            return MODEL_COMPONENT, 'Trwa sprawdzanie modelu i środowiska Chatterbox.'
        for component in self._components:
            if not component['installed']:
                return component['component_id'], component['message']
        return MODEL_COMPONENT, ''

    @property
    def available_voice_ids(self):
        return tuple(row['voice_id'] for row in self.voice_choices if row['installed'])

    @property
    def voice_choices(self):
        ready = self.available
        choices = [{'voice_id': 'builtin', 'name': 'Głos wbudowany', 'custom': False, 'installed': ready},
                   {'voice_id': '02', 'name': '02', 'custom': False, 'installed': ready and self._reference_available}]
        if self.custom_dir is not None:
            for entry in sorted(self.custom_dir.glob('chatterbox-*')):
                if not CUSTOM_ID.fullmatch(entry.name) or not self._has_tensors(entry):
                    continue
                try:
                    name = (entry / 'name').read_text(encoding='utf-8')
                except (OSError, UnicodeError):
                    continue
                choices.append({'voice_id': entry.name, 'name': name, 'custom': True, 'installed': ready})
        return choices

    @property
    def available(self):
        return not self.component_issue[1]

    @property
    def _reference_available(self):
        return self._has_tensors(self.reference)

    @staticmethod
    def _has_tensors(path):
        return all((path / name).is_file() for name in REFERENCE_FILES)

    def require_component(self, voice_id):
        if voice_id not in ("02", "builtin") and not CUSTOM_ID.fullmatch(voice_id):
            raise ValueError("Nieznany głos Chatterbox")
        _, issue = self.component_issue
        if issue:
            raise ChatterboxComponentMissing(issue)
        if voice_id == "builtin":
            return None
        if voice_id == "02":
            if not self._reference_available:
                raise RuntimeError("Brakuje próbki głosu 02.")
            return self.reference
        if self.custom_dir is not None and self._has_tensors(self.custom_dir / voice_id):
            return self.custom_dir / voice_id
        raise RuntimeError('Wybrany głos Chatterbox został usunięty. Wybierz dostępny głos.')

    def _environment(self):
        env = dict(os.environ)
        env.pop('GGML_VK_VISIBLE_DEVICES', None)
        env['LD_LIBRARY_PATH'] = os.pathsep.join(filter(None, (str(self.root / 'lib'), env.get('LD_LIBRARY_PATH'))))
        return env

    def import_reference(self, source, cancelled=None):
        self.require_component('builtin')
        if self.custom_dir is None:
            raise RuntimeError('Brak katalogu na własne głosy Chatterbox.')
        cancelled = cancelled if cancelled is not None else threading.Event()
        source = Path(source)
        self.custom_dir.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=self.custom_dir) as temporary:
            staging = Path(temporary)
            reference = staging / 'reference.wav'
            with source.open('rb') as original:
                data = original.read(100 * 1024 * 1024 + 1)
            if len(data) > 100 * 1024 * 1024:
                raise ValueError('Próbka WAV może mieć najwyżej 100 MB.')
            if cancelled.is_set():
                return None
            reference.write_bytes(data)
            _validate_reference(reference)
            voice_id = 'chatterbox-' + hashlib.sha256(data).hexdigest()[:24]
            destination = self.custom_dir / voice_id
            if self._has_tensors(destination):
                return voice_id
            conditioning = staging / voice_id
            with self._synthesis_lock, TemporaryFile() as log:
                if cancelled.is_set():
                    return None
                process = self._popen([str(self.binary), '--model', str(self.model), '--s3gen-gguf', str(self.s3gen),
                                       '--reference-audio', str(reference), '--save-voice', str(conditioning),
                                       '--language', 'pl', '--threads', '2', '--n-gpu-layers', '99'],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                                      env=self._environment())
                deadline = time.monotonic() + 600
                try:
                    while process.poll() is None:
                        if cancelled.wait(.1):
                            return None
                        if time.monotonic() >= deadline:
                            raise RuntimeError('Przygotowanie głosu Chatterbox przekroczyło 10 minut.')
                    if process.returncode:
                        log.seek(max(0, log.tell() - 2048))
                        logger.error('Chatterbox voice import failed: %s', log.read().decode('utf-8', 'replace'))
                        raise RuntimeError('Chatterbox nie mógł przygotować głosu z tego pliku WAV.')
                    _validate_conditioning(conditioning)
                finally:
                    self._terminate(process)
                if cancelled.is_set():
                    return None
                (conditioning / 'name').write_text(source.stem, encoding='utf-8')
                if not destination.exists():
                    conditioning.rename(destination)
            return voice_id

    def remove_custom_voice(self, voice_id):
        if self.custom_dir is None or not CUSTOM_ID.fullmatch(voice_id):
            raise ValueError('Można usunąć tylko zaimportowany głos Chatterbox.')
        self.cancel()
        shutil.rmtree(self.custom_dir / voice_id)

    def request_epoch(self):
        with self._lock:
            return self._epoch

    def cancel(self):
        with self._lock:
            self._epoch += 1
            process = self._process
            self._process = None
            self._ready_event.set()
            self._request_event.set()
        self._terminate(process)

    close = cancel

    @staticmethod
    def _terminate(process):
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def _stderr_reader(self, process):
        try:
            for line in iter(process.stderr.readline, b""):
                text = line.decode("utf-8", "replace").strip()
                with self._lock:
                    if self._process is not process:
                        return
                    self._tail.append(text)
                    if len(self._tail) > 40:
                        self._tail.pop(0)
                    if text.startswith("NV_EVENT ready "):
                        self._ready = re.search(r"\bbackend=Vulkan\d+\b", text) is not None
                        if not self._ready:
                            self._error = 'Chatterbox nie uruchomił syntezy przez backend Vulkan.'
                        self._ready_event.set()
                    elif text.startswith("NV_EVENT request_done "):
                        self._request_event.set()
                    elif "truncated=1" in text:
                        self._error = "Chatterbox osiągnął limit tokenów bez zakończenia wypowiedzi"
                if text.startswith("NV_EVENT "):
                    logger.info("Chatterbox pid=%s %s", process.pid, text)
        finally:
            with self._lock:
                if self._process is process:
                    if not self._ready:
                        self._ready_event.set()
                    self._request_event.set()

    def _stdout_reader(self, process):
        while True:
            block = os.read(process.stdout.fileno(), 128 * 1024)
            with self._lock:
                if not block or self._process is not process:
                    return
                self._pcm.extend(block)
                self._pcm_last_update = time.monotonic()

    def _start(self, expected_epoch, voice_id):
        with self._start_lock:
            with self._lock:
                if expected_epoch != self._epoch:
                    raise RuntimeError("Chatterbox: uruchamianie anulowane")
                if (self._process is not None and self._process.poll() is None
                        and self._ready and self._process_voice == voice_id):
                    return self._process, True
                reference = self.require_component(voice_id)
                previous, self._process = self._process, None
            self._terminate(previous)
            with self._lock:
                if expected_epoch != self._epoch:
                    raise RuntimeError("Chatterbox: uruchamianie anulowane")
                process = self._popen(
                    [
                        str(self.binary),
                        "--model", str(self.model),
                        "--s3gen-gguf", str(self.s3gen),
                        *(("--ref-dir", str(reference)) if reference is not None else ()),
                        "--language", "pl",
                        "--input-file", "-",
                        "--input-by-line",
                        "--out", "-",
                        "--threads", "2",
                        "--context", "1024",
                        "--n-predict", "512",
                        "--n-gpu-layers", "99",
                        "--seed", "101",
                        "--stream-first-chunk-tokens", "10",
                        "--stream-chunk-tokens", "25",
                        "--stream-cfm-steps", "10",
                        "--cfm-steps", "10",
                        "--cfg-weight", "0.5",
                        "--exaggeration", "0.5",
                        "--temp", "0.8",
                        "--repeat-penalty", "1.2",
                        "--min-p", "0.05",
                        "--top-p", "1",
                        "--top-k", "0",
                        "--crossfade-ms", "0",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=self._environment(),
                    bufsize=0,
                )
                self._process = process
                self._process_voice = voice_id
                self._ready = False
                self._error = None
                self._tail.clear()
                self._ready_event.clear()
                threading.Thread(target=self._stdout_reader, args=(process,), daemon=True).start()
                threading.Thread(target=self._stderr_reader, args=(process,), daemon=True).start()
            if not self._ready_event.wait(timeout=120):
                tail = " | ".join(self._tail[-8:])
                self.cancel()
                detail = f": {tail}" if tail else ""
                raise RuntimeError(f"Chatterbox nie zgłosił gotowości Vulkan w 120 s{detail}")
            with self._lock:
                cancelled = expected_epoch != self._epoch
                if not self._ready:
                    exit_code = process.poll()
                    tail = " | ".join(self._tail[-8:])
                    startup_error = self._error
                else:
                    exit_code = None
                    tail = ""
                    startup_error = None
            if cancelled:
                raise RuntimeError("Chatterbox: uruchamianie anulowane")
            if not self._ready:
                self._terminate(process)
                if startup_error:
                    raise RuntimeError(startup_error)
                detail = f": {tail}" if tail else ""
                if exit_code is not None:
                    raise RuntimeError(f"Chatterbox zakończył start (kod {exit_code}){detail}")
                raise RuntimeError(f"Chatterbox nie przygotował modelu i głosu{detail}")
            return process, False

    def prepare(self, voice_id, *, expected_epoch=None):
        epoch = self.request_epoch() if expected_epoch is None else expected_epoch
        with self._synthesis_lock:
            _, self.model_prepare_reused = self._start(epoch, voice_id)

    def synthesize(self, text, *, language, voice_id, speech_rate,
                   noise_scale=None, noise_w_scale=None, expected_epoch=None):
        del noise_scale, noise_w_scale
        if language.lower() not in ("pl", "pl-pl"):
            raise ValueError("Chatterbox obsługuje tu wyłącznie polski")
        if not 0.5 <= speech_rate <= 2.0:
            raise ValueError('Tempo musi mieścić się między 0,5 a 2.')
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Chatterbox: pusty tekst")

        with self._synthesis_lock:
            with self._lock:
                epoch = self._epoch if expected_epoch is None else expected_epoch
                if epoch != self._epoch:
                    raise RuntimeError("Chatterbox: odrzucono spóźnioną operację")
            process, reused = self._start(epoch, voice_id)
            with self._lock:
                if epoch != self._epoch:
                    raise RuntimeError("Chatterbox: odrzucono spóźnioną operację")
                self._pcm.clear()
                self._error = None
                self._pcm_last_update = None
                self._request_event.clear()
            line = " ".join(text.split()) + "\n"
            process.stdin.write(line.encode("utf-8"))
            process.stdin.flush()

            started = time.monotonic()
            if not self._request_event.wait(timeout=3600):
                raise RuntimeError("Chatterbox przekroczył czas generowania")
            if process.poll() is not None:
                raise RuntimeError("Chatterbox zakończył działanie: " + " | ".join(self._tail[-8:]))
            if epoch != self._epoch:
                raise RuntimeError("Chatterbox: odrzucono spóźniony wynik")
            if self._error:
                raise RuntimeError(self._error)
            end = time.monotonic() + 0.2
            while time.monotonic() < end:
                with self._lock:
                    last_update = self._pcm_last_update
                remaining = end - time.monotonic()
                if last_update is not None and time.monotonic() - last_update >= 0.05:
                    break
                time.sleep(min(0.01, remaining))
            with self._lock:
                if epoch != self._epoch:
                    raise RuntimeError("Chatterbox: odrzucono spóźniony wynik")
                pcm = bytes(self._pcm)
                self._pcm.clear()
        if not pcm or len(pcm) % 2:
            raise RuntimeError("Chatterbox nie zwrócił poprawnego PCM16")
        pcm = stretch_pcm(pcm, RATE, speech_rate)
        with self._lock:
            if epoch != self._epoch:
                raise RuntimeError("Chatterbox: odrzucono spóźniony wynik")
        elapsed = (time.monotonic() - started) * 1000
        return PcmAudio(
            samples=pcm,
            sample_rate=RATE,
            channels=1,
            sample_format="s16le",
            provider_id=CHATTERBOX_ID,
            elapsed_ms=elapsed,
            worker_reused=reused,
            audio_duration_ms=len(pcm) / 2 / RATE * 1000,
            pending_limit=1,
        )
