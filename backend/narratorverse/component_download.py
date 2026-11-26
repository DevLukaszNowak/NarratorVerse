"""Resumable pinned HTTPS downloads. Unverified files never become components."""
from contextlib import contextmanager
from threading import Event, Thread
import hashlib
from http.client import IncompleteRead, HTTPException
import os
from pathlib import Path
import re
import shutil
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class DownloadCancelled(RuntimeError):
    pass


class DownloadIntegrityError(RuntimeError):
    pass


@contextmanager
def cancellable_response(response, cancelled):
    """Interrupt a stalled urllib socket read without waiting for its timeout."""
    finished = Event()
    def watch():
        while not finished.wait(.1):
            if cancelled.is_set():
                try:
                    response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
                except (AttributeError, OSError, ValueError):
                    pass
                return
    watcher = Thread(target=watch, daemon=True, name='component-download-cancel')
    watcher.start()
    try:
        yield response
    finally:
        finished.set()


def file_matches(path, artifact):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != artifact.size_bytes:
        return False
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() == artifact.sha256


def download(artifact, destination, cache, cancelled, progress, approved_hosts,
             *, opener=None, timeout=60, attempts=4, backoff=(1, 2, 4)):
    """Cache survives retries/cancellation and the parent's transactional staging."""
    opener = opener or urlopen
    cache = Path(cache)
    part, ready = cache/(artifact.sha256+'.part'), cache/(artifact.sha256+'.verified')
    def check_cancelled():
        if cancelled.is_set():
            raise DownloadCancelled('Pobieranie anulowane. Zapisano pobraną część; ponowna instalacja wznowi pobieranie.')
    def approved(url):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in approved_hosts:
            raise RuntimeError('Unapproved HTTPS component source')
    def publish():
        check_cancelled()
        # Same component filesystem: keep a verified cache through later-artifact errors.
        try:
            os.link(ready, destination)
        except OSError:
            shutil.copyfile(ready, destination)
        progress(artifact.size_bytes, artifact.size_bytes)
    approved(artifact.source_url)
    check_cancelled()
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    if file_matches(ready, artifact):
        publish();return
    ready.unlink(missing_ok=True)
    if part.exists() and (part.is_symlink() or part.stat().st_size > artifact.size_bytes):
        part.unlink()
    last_error = None
    for attempt in range(attempts):
        check_cancelled()
        if file_matches(part, artifact):
            part.replace(ready);publish();return
        if part.exists() and part.stat().st_size == artifact.size_bytes:
            # A complete but corrupt file must never be resumed at EOF.
            part.unlink()
        offset = part.stat().st_size if part.exists() else 0
        headers = {'Accept-Encoding': 'identity'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        request = Request(artifact.source_url, headers=headers)
        try:
            with opener(request, timeout=timeout) as response, cancellable_response(response, cancelled):
                approved(response.geturl())
                status = response.status
                if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                    raise RuntimeError('Nieobsługiwane kodowanie pobieranego modelu')
                if status == 206:
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                    if not match:
                        raise RuntimeError('Niepoprawna odpowiedź HTTP Range (Content-Range)')
                    start, end, total = map(int, match.groups())
                    if start != offset or total != artifact.size_bytes or not start <= end < total:
                        raise RuntimeError('Niepoprawny zakres odpowiedzi HTTP; plik częściowy zachowano')
                    body_size = end-start+1
                    mode = 'ab' if offset else 'wb'
                elif status == 200:
                    # Server ignored Range. Replace, never append a full response.
                    offset, body_size, mode = 0, artifact.size_bytes, 'wb'
                else:
                    raise RuntimeError(f'Nieoczekiwana odpowiedź HTTP {status}')
                declared = response.headers.get('Content-Length')
                if declared is not None and (not declared.isdigit() or int(declared) != body_size):
                    raise RuntimeError('Niepoprawna długość odpowiedzi HTTP')
                received = 0
                progress(offset, artifact.size_bytes)
                with part.open(mode) as output:
                    while True:
                        check_cancelled()
                        interrupted = None
                        try:
                            block = response.read(128*1024)
                        except IncompleteRead as error:
                            block, interrupted = error.partial, error
                        if not block:
                            if interrupted:raise interrupted
                            break
                        received += len(block)
                        if received > body_size or output.tell()+len(block) > artifact.size_bytes:
                            raise DownloadIntegrityError('Pobrany plik przekracza przypięty rozmiar')
                        output.write(block)
                        # Preserve progress even if the following read times out.
                        output.flush()
                        progress(output.tell(), artifact.size_bytes)
                        if interrupted:raise interrupted
                    output.flush()
                if received != body_size or part.stat().st_size != artifact.size_bytes:
                    raise IncompleteRead(b'', artifact.size_bytes-part.stat().st_size)
            check_cancelled()
            if not file_matches(part, artifact):
                raise DownloadIntegrityError('Model nie przeszedł weryfikacji rozmiaru i SHA-256')
            part.replace(ready)
            publish();return
        except DownloadIntegrityError:
            part.unlink(missing_ok=True)
            raise
        except HTTPError as error:
            # A stale/unsupported range must restart at zero; retry only transient HTTPs.
            if error.code == 416 and offset:
                part.unlink(missing_ok=True)
            elif error.code not in (408, 425, 429, 500, 502, 503, 504):
                raise RuntimeError(f'Nie można pobrać komponentu (HTTP {error.code})') from error
            last_error = error
        except (TimeoutError, socket.timeout, URLError, HTTPException, ConnectionError, OSError) as error:
            last_error = error
        if attempt+1 < attempts:
            delay = backoff[min(attempt, len(backoff)-1)] if backoff else 0
            if cancelled.wait(delay):check_cancelled()
    check_cancelled()
    raise RuntimeError(f'Pobieranie przerwane po {attempts} próbach: {last_error}. '
                       'Zapisano plik częściowy. Kliknij Zainstaluj/Napraw, aby wznowić.') from last_error
