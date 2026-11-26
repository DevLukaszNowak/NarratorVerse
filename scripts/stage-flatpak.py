#!/usr/bin/env python3
"""Assemble a local snapshot with owner-authorized Voidek, never user data."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
APP_ID = 'io.github.DevVoidPL.NarratorVerse'
COMPONENTS = ('ocr.english-local', 'ocr.polish-local', 'ocr.rapidocr-local', 'translation.opus-en-pl', 'tts.voidek')
APPROVED_STYLE = Path('backend/narratorverse/assets/supertonic/Supertonic3-M5-02.json')
APPROVED_STYLE_SHA256 = '35fcc0a9f65eb7e693a2aac17b30d6b9ce8cf5bb21953190cf08bc12d2ab15a0'


def copy_tree(source, destination, *, exclude_f5=False):
    if not source.is_dir(): raise RuntimeError(f'Missing input directory: {source}')
    for path in source.rglob('*'):
        if path.is_symlink(): raise RuntimeError(f'Symlink not allowed in snapshot: {path}')
    ignored = ['__pycache__', '*.pyc', '.pytest_cache']
    if exclude_f5:
        ignored.extend(('f5_*', 'f5-*', 'F5-*'))
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(*ignored))


def stage_quickmt_runtime(destination):
    source = ROOT / '.tools/quickmt-python'
    metadata = source / 'sentencepiece-0.2.1.dist-info'
    license_file = ROOT / 'licenses/sentencepiece-LICENSE.txt'
    if not (metadata / 'METADATA').is_file() or not (source / 'sentencepiece').is_dir():
        raise RuntimeError('Missing SentencePiece; run scripts/prepare-quickmt-runtime.sh first')
    if not license_file.is_file() or license_file.is_symlink():
        raise RuntimeError('Missing SentencePiece license: licenses/sentencepiece-LICENSE.txt')
    details = importlib.metadata.PathDistribution(metadata)
    if details.metadata['Name'] != 'sentencepiece' or details.version != '0.2.1':
        raise RuntimeError('Expected pinned SentencePiece 0.2.1')
    if not (source / 'sentencepiece/_sentencepiece.cpython-313-x86_64-linux-gnu.so').is_file():
        raise RuntimeError('Missing CPython 3.13 / x86_64 SentencePiece native extension')
    for name in ('sentencepiece', 'sentencepiece-0.2.1.dist-info'):
        if (destination / name).exists():
            raise RuntimeError(f'Refusing to overwrite existing runtime package: {name}')
    for name in ('sentencepiece', 'sentencepiece-0.2.1.dist-info'):
        copy_tree(source / name, destination / name)


def stage_supertonic_runtime(destination, source):
    packages = {'supertonic': '1.3.1', 'onnxruntime': '1.22.1', 'numpy': '2.2.6'}
    for package, version in packages.items():
        metadata = source / f'{package}-{version}.dist-info'
        if importlib.metadata.PathDistribution(metadata).version != version:
            raise RuntimeError(f'Supertonic requires {package} {version}')
    extension = source / 'onnxruntime/capi/onnxruntime_pybind11_state.cpython-313-x86_64-linux-gnu.so'
    if not extension.is_file():
        raise RuntimeError('Supertonic requires the CPython 3.13 x86_64 ONNX Runtime wheel')
    destination.mkdir(parents=True)
    for package, version in packages.items():
        copy_tree(source / package, destination / package)
        metadata = f'{package}-{version}.dist-info'
        copy_tree(source / metadata, destination / metadata)
    copy_tree(source / 'numpy.libs', destination / 'numpy.libs')


def stage_chatterbox(app, library_dir, cli_path):
    source = ROOT / 'experiments/chatterbox-v3-vulkan'
    target = app / 'experiments/chatterbox-v3-vulkan'
    target.mkdir(parents=True)
    if cli_path.is_symlink() or not cli_path.is_file():
        raise RuntimeError(f'Missing Chatterbox CLI: {cli_path}')
    cli_target = target / 'bin/tts-cli'
    cli_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cli_path, cli_target)
    for name in ('models/t3-v3-q8_0.gguf', 'models/s3gen-mtl-q8_0.gguf',
                 'reference/02-conditioning/embedding.npy',
                 'reference/02-conditioning/prompt_token.npy',
                 'reference/02-conditioning/prompt_feat.npy',
                 'reference/02-conditioning/speaker_emb.npy',
                 'reference/02-conditioning/cond_prompt_speech_tokens.npy'):
        src = source / name
        if src.is_symlink() or not src.is_file():
            raise RuntimeError(f'Missing Chatterbox package file: {name}')
        dst = target / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    libraries = {
        'libggml.so.0': library_dir / 'libggml.so.0',
        'libggml-base.so.0': library_dir / 'libggml-base.so.0',
        'libggml-cpu.so.0': library_dir / 'libggml-cpu.so.0',
        'libggml-vulkan.so.0': library_dir / 'ggml-vulkan/libggml-vulkan.so.0',
    }
    for name, src in libraries.items():
        if not src.is_file():
            raise RuntimeError(f'Missing Chatterbox runtime library: {src}')
        dst = target / 'lib' / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for name in ('SOURCE.json', 'UPSTREAM_PATCH.diff', 'assets.json',
                 'licenses/model-card.md', 'README.md'):
        src = source / name
        if not src.is_file():
            raise RuntimeError(f'Missing Chatterbox provenance file: {name}')
        dst = target / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def audit_snapshot(destination):
    app = Path(destination) / 'share/narratorverse'
    forbidden = ('tts.custom-voices', 'tts.supertonic3', 'Supertonic3-M5-02.json',
                 '.private', 'reference', 'accepted-references',
                 'profiles.json', 'capture-grants.json', 'translation-cache.sqlite3',
                 'quickmt-en-pl', 'src.spm.model', 'tgt.spm.model')
    for path in Path(destination).rglob('*'):
        if path.is_symlink(): raise RuntimeError(f'Unexpected staged symlink: {path}')
        if path == app / APPROVED_STYLE:
            if hashlib.sha256(path.read_bytes()).hexdigest() != APPROVED_STYLE_SHA256:
                raise RuntimeError('Wrong SHA-256 for the approved Supertonic M5-02 style')
            continue
        for part in path.relative_to(destination).parts:
            if part not in forbidden: continue
            public_chatterbox_voice = (part == 'reference'
                                       and path.is_relative_to(app / 'experiments/chatterbox-v3-vulkan/reference'))
            if not public_chatterbox_voice:
                raise RuntimeError(f'Forbidden package input: {path}')
        forbidden_f5 = {
            'f5_assets.py', 'f5_data', 'f5_diagnostics.py', 'f5_tts.py',
            'f5_worker.py', 'f5-runtime', 'f5-native', 'f5-accepted-references',
            'tts.f5-polish', 'f5-polish-quality',
        }
        if any(part.lower() in forbidden_f5 for part in path.relative_to(destination).parts):
            raise RuntimeError(f'F5 content is not included in this package: {path}')


def stage_desktop_assets(destination):
    for path in (f'applications/{APP_ID}.desktop', f'metainfo/{APP_ID}.metainfo.xml'):
        target = destination / 'share' / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / 'data' / Path(path).name, target)
    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        folder = destination / f'share/icons/hicolor/{size}x{size}/apps'
        folder.mkdir(parents=True, exist_ok=True)
        for name in (APP_ID, f'{APP_ID}.round'):
            shutil.copy2(ROOT / f'data/icons/{size}x{size}.png', folder / f'{name}.png')


def stage(destination, *, quickmt=False, chatterbox=False,
          chatterbox_lib_dir=None, chatterbox_cli=None, supertonic_python_dir=None):
    binary = ROOT / 'src-tauri/target/release/narratorverse'
    if not binary.is_file(): raise RuntimeError('Build the release binary first')
    app = destination / 'share/narratorverse'
    if app.exists(): raise RuntimeError(f'Refusing to overwrite staged application: {app}')
    app.mkdir(parents=True)
    copy_tree(ROOT / 'backend', app / 'backend', exclude_f5=True)
    copy_tree(ROOT / 'bin', app / 'bin')
    # .runtime/python is an obsolete duplicate; Python imports from lib only.
    for name in ('lib', 'lib64', 'bin', 'share', 'libexec'):
        source = ROOT / '.runtime' / name
        if source.exists(): copy_tree(source, app / '.runtime' / name)
    if quickmt:
        stage_quickmt_runtime(app / '.runtime/lib/python3.13/site-packages')
    if supertonic_python_dir is not None:
        stage_supertonic_runtime(app / '.runtime/supertonic3', supertonic_python_dir)
    for component in COMPONENTS:
        copy_tree(ROOT / 'components' / component, app / 'components' / component)
    copy_tree(ROOT / 'licenses', app / 'licenses', exclude_f5=True)
    if (app / APPROVED_STYLE).is_file():
        shutil.copy2(ROOT / 'experiments/supertonic3-cpu/LICENSE-SUPERTONIC',
                     app / 'licenses/Supertonic3-OpenRAIL-M.txt')
    shutil.copy2(ROOT / 'LICENSE', app / 'LICENSE')
    if chatterbox:
        if chatterbox_lib_dir is None:
            raise RuntimeError('Chatterbox packaging requires --chatterbox-lib-dir')
        if chatterbox_cli is None:
            raise RuntimeError('Chatterbox packaging requires --chatterbox-cli')
        stage_chatterbox(app, chatterbox_lib_dir, chatterbox_cli)
    (destination / 'bin').mkdir(exist_ok=True)
    shutil.copy2(binary, destination / 'bin/narratorverse')
    stage_desktop_assets(destination)
    packages = []
    for dist in importlib.metadata.distributions(path=[str(app / '.runtime/lib/python3.13/site-packages')]):
        packages.append({'name': dist.metadata.get('Name'), 'version': dist.version,
                         'license': dist.metadata.get('License-Expression') or dist.metadata.get('License'),
                         'classifiers': dist.metadata.get_all('Classifier', [])})
    (app / 'licenses/python-packages.json').write_text(json.dumps(packages, indent=2))
    inventory = {}
    for path in sorted(destination.rglob('*')):
        if path.is_symlink(): raise RuntimeError(f'Unexpected staged symlink: {path}')
        if path.is_file():
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            inventory[str(path.relative_to(destination))] = {'bytes': path.stat().st_size, 'sha256': digest}
    (app / 'licenses/snapshot-files.json').write_text(json.dumps(inventory, indent=2))
    audit_snapshot(destination)
    print(f'Staged {len(inventory)} files; owner-authorized Voidek included; user data and custom voices excluded.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dest', required=True, type=Path)
    parser.add_argument('--chatterbox', action='store_true', help='Include bundled Chatterbox V3 Vulkan experiment')
    parser.add_argument('--chatterbox-lib-dir', type=Path, help='Build directory containing Chatterbox ggml runtime libraries')
    parser.add_argument('--chatterbox-cli', type=Path, help='Built Chatterbox CLI to include in the package')
    parser.add_argument('--quickmt', action='store_true', help='Include pinned SentencePiece, never QuickMT weights')
    parser.add_argument('--supertonic-python-dir', type=Path, help='Include the CPU benchmark Python wheels for Supertonic')
    args = parser.parse_args()
    stage(args.dest.resolve(), quickmt=args.quickmt, chatterbox=args.chatterbox,
          chatterbox_lib_dir=args.chatterbox_lib_dir.resolve() if args.chatterbox_lib_dir else None,
          chatterbox_cli=args.chatterbox_cli.resolve() if args.chatterbox_cli else None,
          supertonic_python_dir=args.supertonic_python_dir)
