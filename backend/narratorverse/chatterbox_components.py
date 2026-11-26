"""Integrity and Vulkan checks for the Chatterbox resources shipped in Flatpak."""
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys

MODEL_COMPONENT = 'tts.chatterbox-v3-model'
VULKAN_COMPONENT = 'tts.chatterbox-vulkan'
MODEL_FILES = {
    'models/t3-v3-q8_0.gguf': {
        'bytes': 604_323_840, 'sha256': '9f241bf00b9c59036dc08bb0ee206d5675210720d1a9ef7a05f8e4cd12f99049',
    },
    'models/s3gen-mtl-q8_0.gguf': {
        'bytes': 827_369_184, 'sha256': '14dc06ebad03d0edcef98f99d24a91c571599593c5e379ea0190ce15414de672',
    },
}
RUNTIME_FILES = ('bin/tts-cli', 'lib/libggml.so.0', 'lib/libggml-base.so.0',
                 'lib/libggml-cpu.so.0', 'lib/libggml-vulkan.so.0')
INVENTORY_PREFIX = 'share/narratorverse/experiments/chatterbox-v3-vulkan/'
RECOVERY = 'Aby odzyskać pliki, zainstaluj pełną paczkę Flatpak NarratorVerse-FedoraFixes-full.flatpak.'
logger = logging.getLogger(__name__)


def _file_identity(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _check_files(root, names, metadata, verified_files):
    total = 0
    for name in names:
        path = root / name
        if not path.is_file():
            return 'missing', f'Brakuje pliku {path.name}. {RECOVERY}', None
        expected = metadata.get(name)
        if not isinstance(expected, dict):
            return 'corrupt', f'Brakuje danych do sprawdzenia pliku {path.name}. {RECOVERY}', None
        try:
            identity = _file_identity(path)
            size = identity[2]
            if not path.is_symlink() and size == expected['bytes']:
                digest = expected['sha256']
                if verified_files.get(path) != (identity, digest):
                    with path.open('rb') as data:
                        if hashlib.file_digest(data, 'sha256').hexdigest() != digest or _file_identity(path) != identity:
                            return 'corrupt', f'Plik {path.name} nie przeszedł kontroli integralności. {RECOVERY}', None
                    verified_files[path] = identity, digest
                total += size
                continue
        except (OSError, KeyError):
            pass
        return 'corrupt', f'Plik {path.name} nie przeszedł kontroli integralności. {RECOVERY}', None
    return 'available', '', total


def _runtime_inventory(root):
    path = root.parents[1] / 'licenses/snapshot-files.json'
    try:
        inventory = json.loads(path.read_text(encoding='utf-8'))
        return {name: inventory.get(INVENTORY_PREFIX + name) for name in RUNTIME_FILES}
    except (OSError, ValueError, AttributeError):
        return {}


def _probe_driver(root, environment):
    try:
        cli = subprocess.run([str(root / 'bin/tts-cli'), '--help'], env=environment,
                             capture_output=True, text=True, timeout=8, check=False)
        if cli.returncode != 0:
            logger.warning('Chatterbox executable probe returned %s: %s', cli.returncode, cli.stderr[-1200:])
            return 'unavailable', 'Program Chatterbox nie uruchamia się w Flatpaku.'
        probe = subprocess.run([sys.executable, '-I', '-S', '-B', str(Path(__file__).with_name('chatterbox_vulkan_probe.py')),
                                str(root / 'lib/libggml-vulkan.so.0')], env=environment,
                               capture_output=True, text=True, timeout=15, check=False)
        if probe.returncode == 0:
            device = json.loads(probe.stdout)['device']
            if isinstance(device, str) and device.strip():
                return 'available', f'Uruchomiono Vulkan na urządzeniu {device}.'
            if device is None:
                return 'unsupported', 'Flatpak nie widzi sprzętowego urządzenia Vulkan. Sprawdź sterownik karty graficznej i środowisko graficzne Flatpaka.'
        logger.warning('Chatterbox Vulkan probe returned %s: %s', probe.returncode, probe.stderr[-1200:])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
        logger.warning('Chatterbox Vulkan probe failed: %s', error)
    return 'unavailable', 'Nie udało się uruchomić Vulkan w Flatpaku. Sprawdź sterownik karty graficznej i środowisko graficzne Flatpaka.'


def _component(component_id, name, attribution, state, message, size, *, version):
    return dict(component_id=component_id, kind='tts', name=name, attribution=attribution,
                state=state, message=message, installed=state == 'available', verified=size is not None,
                installed_size_bytes=size, download_size_bytes=None, installable=False, managed=False,
                origin='bundled', license_id='MIT', version=version)


def chatterbox_component_statuses(root, environment, verified_files):
    state, message, size = _check_files(root, MODEL_FILES, MODEL_FILES, verified_files)
    model = _component(MODEL_COMPONENT, 'Model Chatterbox Multilingual V3', 'Resemble AI', state,
                       message or 'Zweryfikowano oba pliki GGUF modelu.', size, version='Multilingual V3 Q8_0')
    installed_package = root.is_relative_to('/app') or (root.parents[1] / 'licenses/snapshot-files.json').exists()
    if not installed_package and (root / 'bin/tts-cli').is_file() and os.access(root / 'bin/tts-cli', os.X_OK):
        state, message, size = 'available', 'Program z katalogu źródłowego. Dostęp do Vulkan jest sprawdzany przy uruchomieniu silnika.', None
    else:
        state, message, size = _check_files(root, RUNTIME_FILES, _runtime_inventory(root), verified_files)
    if state == 'available' and installed_package:
        if os.access(root / 'bin/tts-cli', os.X_OK):
            state, message = _probe_driver(root, environment)
        else:
            state, message = 'corrupt', f'Brakuje uprawnień do uruchomienia tts-cli. {RECOVERY}'
    runtime = _component(VULKAN_COMPONENT, 'Chatterbox Vulkan', 'chatterbox.cpp · ggml', state,
                         message, size, version='')
    return model, runtime
