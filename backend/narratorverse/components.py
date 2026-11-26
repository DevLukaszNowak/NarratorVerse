"""Original verified installer with cancellable chunk transport and progress."""
import hashlib
import json
from pathlib import Path
from dataclasses import asdict
from threading import Event
from urllib.parse import urlparse
from urllib.request import urlopen
from game_optimization_linux.services.narrator_components import NarratorComponentManager, _APPROVED_DOWNLOAD_HOSTS
from game_optimization_linux.config import BUNDLED_NARRATOR_COMPONENTS_DIR

class ComponentManager(NarratorComponentManager):
    def __init__(self, *args, progress=lambda *_: None, **kwargs):
        super().__init__(*args, **kwargs)
        self.cancelled = Event()
        self.progress = progress
        self.failures = {}

    def verified_status(self, component_id, runtime, bundled_root=BUNDLED_NARRATOR_COMPONENTS_DIR):
        """Do not equate catalog presence or a Python import with an installed model."""
        definition = self._definition(component_id)
        row = asdict(self.status(component_id))
        directory = self.root / component_id
        manifest = self._read_manifest(component_id)
        model_component = bool(definition.resolved_artifacts() or definition.pinned_files)
        try:
            good, message = self.verify_installed(component_id) if model_component and manifest else (not model_component, '')
        except (OSError, ValueError, TypeError, KeyError):
            good, message = False, 'Nie można zweryfikować plików komponentu'
        # Pinned raw files must match our catalog, not merely a self-authored manifest.
        pins = definition.pinned_files or tuple(a for a in definition.resolved_artifacts() if not a.archive_format)
        if good and model_component:
            entries = {entry['path']: entry for entry in manifest['files']}
            for pin in pins:
                entry = entries.get(str(pin.target_relative_path))
                if not entry or entry['sha256'] != pin.sha256 or (directory / pin.target_relative_path).stat().st_size != pin.size_bytes:
                    good, message = False, 'Plik nie zgadza się z przypiętymi metadanymi komponentu'
            bundled_manifest = Path(bundled_root) / component_id / 'component.json'
            if bundled_manifest.is_file():
                trusted = json.loads(bundled_manifest.read_text())
                if str(manifest.get('version')) == str(trusted.get('version')):
                    expected = {e['path']: e['sha256'] for e in trusted['files']}
                    actual = {e['path']: e['sha256'] for e in manifest['files']}
                    if actual != expected:
                        good, message = False, 'Manifest plików różni się od zweryfikowanego manifestu tej wersji'
        installed = good and bool(runtime)
        if model_component and not manifest:
            state = 'error' if directory.exists() else 'not_installed'
            message = 'Brak poprawnego manifestu' if directory.exists() else 'Komponent nie jest zainstalowany'
        elif not good: state = 'error'
        elif not runtime: state, message = 'error', 'Pliki obecne, ale wymagane środowisko wykonawcze jest niedostępne'
        else: state, message = 'available', 'Pliki zweryfikowane; środowisko dostępne' if model_component else 'Dostępne w środowisku aplikacji'
        if component_id in self.failures:
            state, message, installed = 'error', self.failures[component_id], False
        bundled = False
        if manifest and good:
            bundled_manifest = Path(bundled_root) / component_id / 'component.json'
            if bundled_manifest.is_file():
                try:
                    bundled = {e['path']: e['sha256'] for e in manifest['files']} == {e['path']: e['sha256'] for e in json.loads(bundled_manifest.read_text())['files']}
                except (ValueError, KeyError, TypeError): pass
        origin_path = directory / '.narratorverse-origin'
        origin = origin_path.read_text().strip() if origin_path.is_file() else ''
        if origin not in ('bundled', 'downloaded', 'local'): origin = 'bundled' if bundled else 'downloaded' if manifest and manifest.get('artifacts') else 'local' if manifest else 'runtime' if not model_component else 'missing'
        row.update(state=state, installed=installed, verified=good and bool(manifest),
                   managed=directory.exists() if model_component else False,
                   version=str(manifest.get('version', '')) if manifest and good else '',
                   installed_size_bytes=sum(p.stat().st_size for p in directory.rglob('*') if p.is_file() and not p.is_symlink()) if directory.is_dir() else None,
                   origin=origin,
                   message=message, installable=bool(definition.install_ready or (definition.pinned_files and all((Path(bundled_root)/component_id/p.target_relative_path).is_file() for p in definition.pinned_files))))
        if not model_component: row['download_size_bytes'] = None
        return row

    def _check_install_cancelled(self):
        from .component_download import DownloadCancelled
        if self.cancelled.is_set():
            raise DownloadCancelled('Pobieranie anulowane. Pliki zachowano do wznowienia.')

    def install(self, component_id):
        definition = self._definition(component_id)
        self._check_install_cancelled()
        artifacts = definition.resolved_artifacts()
        self._download_base = 0
        self._download_total = sum(a.size_bytes for a in artifacts)
        try:
            super().install(component_id)
        except Exception as error:
            self.failures[component_id] = str(error)
            raise
        else:
            self.failures.pop(component_id, None)
            for artifact in artifacts:
                for suffix in ('.part', '.verified'):
                    (self.root / '.downloads' / (artifact.sha256 + suffix)).unlink(missing_ok=True)
        finally:
            self._download_base = self._download_total = 0

    def _download_artifact(self, artifact, destination):
        from .component_download import download
        base = getattr(self, '_download_base', 0)
        total = getattr(self, '_download_total', 0) or artifact.size_bytes
        download(artifact, destination, self.root / '.downloads', self.cancelled,
                 lambda size, _: self.progress(base + size, total), _APPROVED_DOWNLOAD_HOSTS)
        self._download_base = base + artifact.size_bytes
