"""Isolated ASR process configuration, without imports of inference libraries."""
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Lock, Thread

from game_optimization_linux import config
from .asr_assets import ASR_MODEL_ID, ASR_RUNTIME_ID, verified_asr_component

MAX_LINE = 256 * 1024
WORKER = Path(__file__).with_name('asr_worker.py').resolve()


def is_own_stream(stream, own_pids):
    metadata = ' '.join([stream.application_name, stream.application_binary,
                         stream.application_id]).casefold()
    return str(stream.pid) in own_pids or any(name in metadata for name in
        ('narratorverse', 'piper', 'lektorpl'))


def clean_environment(environment):
    return {k:v for k,v in environment.items() if k not in {
        'PYTHONPATH','LD_LIBRARY_PATH','LD_PRELOAD','PYTHONHOME','VIRTUAL_ENV',
        'HSA_OVERRIDE_GFX_VERSION','PULSE_SERVER','PULSE_COOKIE','QT_PLUGIN_PATH',
        'QT_QPA_PLATFORM_PLUGIN_PATH'}}


def worker_environment(*, legacy=False):
    environment = clean_environment(os.environ)
    if not legacy:
        environment.update({name: os.environ[name] for name in ('PULSE_SERVER', 'PULSE_COOKIE')
                            if name in os.environ})
    environment.update(HF_HUB_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
    return environment


def _local_command(*arguments):
    return [sys.executable, '-I', '-S', '-B', '-u', str(WORKER), *arguments]


def _legacy_command(python, model, *arguments):
    python, model = Path(python), Path(model)
    if not python.is_absolute() or not model.is_absolute():
        raise ValueError('Ścieżki środowiska i modelu ASR muszą być absolutne')
    if not python.is_file() or not (model / 'model.bin').is_file():
        raise RuntimeError('Brak lokalnego Whisper small lub osobnego środowiska ASR. Niczego nie pobrano automatycznie.')
    command = [str(python), '-I', '-B', '-u', str(WORKER),
               '--model', str(model), *arguments]
    if Path('/.flatpak-info').exists():
        # Host Python is intentionally isolated from the application's CT2/Qt ABI.
        unset = [f'--unset-env={name}' for name in os.environ if name not in clean_environment(os.environ)]
        return ['flatpak-spawn', '--host', '--watch-bus', *unset, *command]
    return command


def _legacy_paths():
    python = os.environ.get('NARRATORVERSE_ASR_PYTHON')
    model = os.environ.get('NARRATORVERSE_ASR_MODEL')
    if python and model:
        return python, model
    return None


def legacy_asr_requested():
    return _legacy_paths() is not None


def worker_command(*arguments):
    if legacy := _legacy_paths():
        return _legacy_command(*legacy, *arguments)
    if '--list' in arguments:
        return _local_command(*arguments)
    root = config.NARRATOR_COMPONENTS_DIR
    if not verified_asr_component(root, ASR_RUNTIME_ID):
        raise RuntimeError('Zainstaluj lub napraw środowisko ASR w Komponentach.')
    if not verified_asr_component(root, ASR_MODEL_ID):
        raise RuntimeError('Zainstaluj lub napraw model Whisper small w Komponentach.')
    return _local_command('--runtime', str(root / ASR_RUNTIME_ID),
                          '--model', str(root / ASR_MODEL_ID), *arguments)


def asr_runtime_available():
    root = config.NARRATOR_COMPONENTS_DIR
    if not verified_asr_component(root, ASR_RUNTIME_ID):
        return False
    try:
        result = subprocess.run(_local_command('--runtime', str(root / ASR_RUNTIME_ID), '--probe'),
                                env=worker_environment(), capture_output=True, text=True,
                                timeout=15, check=False)
        return result.returncode == 0 and json.loads(result.stdout) == {'ready': True}
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def list_applications():
    result = subprocess.run(worker_command('--list'), env=worker_environment(legacy=legacy_asr_requested()),
                            capture_output=True, text=True, timeout=12, check=False)
    if result.returncode:
        raise RuntimeError('Nie można odczytać strumieni ASR: ' + (result.stderr or result.stdout)[-1500:])
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise RuntimeError('Nieprawidłowa lista audio')
    return rows


class AsrProcess:
    """One inference child; EOF cancels capture, bounded output and explicit errors."""
    def __init__(self):
        self.process = None
        self.lock = Lock()

    def start(self, selection, preset, callback):
        with self.lock:
            if self.process is not None:
                raise RuntimeError('Poprzedni proces ASR nie został zamknięty')
            arguments=('--diagnostics',) if os.environ.get('NARRATORVERSE_ASR_DIAGNOSTICS')=='1' else ()
            process = subprocess.Popen(worker_command('--preset', preset, *arguments),
                env=worker_environment(legacy=legacy_asr_requested()), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding='utf-8', bufsize=1)
            self.process = process
        try:
            process.stdin.write(json.dumps(selection) + '\n')
            process.stdin.flush()
        except Exception:
            self.stop()
            raise

        def read():
            failed = False
            try:
                while True:
                    line = process.stdout.readline(MAX_LINE + 1)
                    if not line:
                        break
                    if len(line) > MAX_LINE:
                        raise RuntimeError('Zbyt duży wynik ASR')
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError('Nieprawidłowy wynik ASR')
                    failed |= row.get('kind') == 'error'
                    callback(row)
                code = process.wait(timeout=3)
                if not failed:
                    callback({'kind':'error', 'message':f'Proces ASR zakończył pracę ({code}). Wybierz strumień ponownie.'})
            except Exception as error:
                callback({'kind':'error','message':str(error)})
            finally:
                process.stdout.close()
        Thread(target=read, daemon=True, name='narratorverse-asr-output').start()

    def stop(self):
        with self.lock:
            process, self.process = self.process, None
        if process is None:
            return
        try:
            process.stdin.close()  # Cancellation signal; allows capture's finally to run.
        except OSError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
