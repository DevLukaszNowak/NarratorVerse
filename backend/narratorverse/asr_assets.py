"""Pinned, optional Faster Whisper CPU runtime and multilingual small weights."""
import hashlib
import json
from pathlib import Path

from game_optimization_linux.services.narrator_components import (
    NarratorComponentArtifact, NarratorComponentDefinition, NarratorComponentManager,
)

ASR_RUNTIME_ID = 'asr.faster-whisper-runtime'
ASR_MODEL_ID = 'asr.whisper-small'
ASSETS = json.loads(Path(__file__).with_name('asr_assets.json').read_text(encoding='utf-8'))


def asr_component_definitions():
    runtime = ASSETS['runtime']
    model = ASSETS['model']
    model_base = f"https://huggingface.co/{model['repository']}/resolve/{model['revision']}"
    runtime_artifacts = tuple(
        NarratorComponentArtifact(wheel['url'], wheel['sha256'], wheel['size'], archive_format='zip')
        for wheel in runtime['wheels'].values()
    )
    model_artifacts = tuple(
        NarratorComponentArtifact(f'{model_base}/{name}', file['sha256'], file['size'], name)
        for name, file in model['files'].items()
    )
    return (
        NarratorComponentDefinition(
            component_id=ASR_RUNTIME_ID, kind='asr', name='Środowisko Faster Whisper (CPU)',
            license_id='Wiele licencji', runtime_license_id='Wiele licencji',
            attribution='SYSTRAN · faster-whisper i zależności z PyPI', version=runtime['version'],
            download_size_bytes=sum(artifact.size_bytes for artifact in runtime_artifacts),
            artifacts=runtime_artifacts, install_ready=True,
        ),
        NarratorComponentDefinition(
            component_id=ASR_MODEL_ID, kind='asr', name='Whisper small',
            license_id='MIT', artifact_license_id='MIT',
            attribution='OpenAI · Whisper small, konwersja SYSTRAN', version=model['revision'],
            download_size_bytes=sum(artifact.size_bytes for artifact in model_artifacts),
            artifacts=model_artifacts, install_ready=True,
        ),
    )


def verified_asr_component(components_root, component_id):
    manager = NarratorComponentManager(components_root, definitions=asr_component_definitions())
    manifest = manager._read_manifest(component_id)
    definition = manager._definition(component_id)
    directory = components_root / component_id
    if not manifest or not directory.is_dir() or directory.is_symlink():
        return False
    if manifest.get('version') != definition.version:
        return False
    try:
        allowed = {entry['path'] for entry in manifest['files']}
        if component_id == ASR_MODEL_ID:
            if not _model_files_match(directory, definition.artifacts, allowed):
                return False
        else:
            expected = [{
                'source_url': file.source_url, 'size': file.size_bytes, 'sha256': file.sha256,
                'archive_format': file.archive_format,
            } for file in definition.artifacts]
            if manifest.get('artifacts') != expected or not manager.verify_installed(component_id)[0]:
                return False
        allowed.update(('component.json', '.narratorverse-origin'))
        return all(not path.is_symlink() and (path.is_dir() or path.relative_to(directory).as_posix() in allowed)
                   for path in directory.rglob('*'))
    except (OSError, KeyError, TypeError, ValueError):
        return False


def _model_files_match(directory, artifacts, allowed):
    if allowed != {artifact.target_relative_path for artifact in artifacts}:
        return False
    for artifact in artifacts:
        path = directory / artifact.target_relative_path
        if not path.is_file() or path.is_symlink() or path.stat().st_size != artifact.size_bytes:
            return False
        with path.open('rb') as data:
            if hashlib.file_digest(data, 'sha256').hexdigest() != artifact.sha256:
                return False
    return True
