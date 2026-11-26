"""Explicit local QuickMT experiment; no downloader or cloud fallback.

    quickmt/quickmt-en-pl, CC-BY-4.0, revision below. Git blob hashes for
    small files; SHA-256 for LFS payloads. Hashing occurs once per file stat
    signature in each provider, not per caption. Model loaded only on demand.
"""
from __future__ import annotations

from functools import partial
import hashlib
import importlib.util
from pathlib import Path

from game_optimization_linux.services.narrator_translation import (
    ArgosCTranslate2TranslationProvider, _SubprocessTranslationWorker,
)

QUICKMT_REVISION = '6209580ce1fe525214f29fc39725007b73410109'
QUICKMT_FILES = {
    'model.bin': (401699775, 'sha256', '7f02e09d52f04f77cb6b2da251235149053387812e3cfb73afb99882483cee72'),
    'config.json': (223, 'git', '3f51dcd53a8af7e9ba8972f2264e8514af1efc43'),
    'source_vocabulary.json': (332684, 'git', 'c7e883a165ca81db044b46a9f2d5f101f0a30cb3'),
    'target_vocabulary.json': (381704, 'git', 'f2f29d4b87131affe42162f984dda69acb1e9481'),
    'src.spm.model': (585058, 'sha256', '83fa4a21ff8359b8827f033977c0e563fd0786ba84bbc93d4a1e22a0dd81ee7f'),
    'tgt.spm.model': (605240, 'sha256', 'b6ad874c44613df56fb48be31fb64b6b5fc96a14a7888e802b0f91dbf98a8836'),
}


def quickmt_layout(root: Path) -> tuple[Path, Path, str] | None:
    root = Path(root)
    try:
        for name, (size, kind, expected) in QUICKMT_FILES.items():
            path = root / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
                return None
            digest = hashlib.sha256() if kind == 'sha256' else hashlib.sha1()
            if kind == 'git':
                digest.update(f'blob {size}\0'.encode('ascii'))
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                return None
    except OSError:
        return None
    return root, root / 'src.spm.model', ''


def quickmt_runtime_available() -> bool:
    try:
        return all(importlib.util.find_spec(name) is not None
                   for name in ('ctranslate2', 'sentencepiece'))
    except (ImportError, ValueError):
        return False


class QuickMTTranslationProvider(ArgosCTranslate2TranslationProvider):
    provider_id = 'quickmt-ctranslate2-en-pl-' + QUICKMT_REVISION + '-shared-decoding-v1'
    component_id = 'experimental.quickmt-en-pl'
    model_version = QUICKMT_REVISION

    def __init__(self, model_dir: Path, *, runtime_available=quickmt_runtime_available,
                 worker_factory=None, **kwargs):
        self._checked_signature = None
        self._checked_layout = None
        super().__init__(component_path=model_dir, runtime_available=runtime_available,
                         worker_factory=worker_factory or partial(
                             _SubprocessTranslationWorker,
                             worker_module='narratorverse.quickmt_worker'), **kwargs)

    def _layout(self):
        try:
            signature = tuple((name, path.stat().st_size, path.stat().st_mtime_ns,
                               path.stat().st_ctime_ns, path.stat().st_ino, path.is_symlink())
                              for name in QUICKMT_FILES
                              for path in (self._component_path / name,))
        except OSError:
            return None
        with self._worker_lock:
            if signature != self._checked_signature:
                self._checked_layout = quickmt_layout(self._component_path)
                self._checked_signature = signature
            return self._checked_layout

    @property
    def status_message(self):
        if not self._runtime_available():
            return 'QuickMT requires local CTranslate2 and SentencePiece in the backend runtime'
        if self._layout() is None:
            return 'QuickMT files are missing or do not match pinned revision ' + QUICKMT_REVISION
        return 'Experimental QuickMT EN→PL is ready (local CPU)'

    def translate(self, text, **kwargs):
        if not self.available:
            raise RuntimeError(self.status_message)
        return super().translate(text, **kwargs)