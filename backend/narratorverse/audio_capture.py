"""PulseAudio/PipeWire-Pulse stream discovery and safe selection."""
from __future__ import annotations

from dataclasses import dataclass, field
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from uuid import uuid4
from typing import Mapping


class AudioStreamUnavailable(RuntimeError):
    """The selected stream is no longer present or cannot be identified safely."""


@dataclass(frozen=True, slots=True)
class AudioStream:
    stream_id: str
    stream_index: int
    client_id: str
    server_identity: str
    serial: str
    pid: str
    application_name: str
    application_binary: str
    application_id: str
    label: str
    sink_index: int | None = None
    # Playback state changes are not a change of the selected stream identity.
    corked: bool | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class AudioApplication:
    group_id: str
    pid: str
    label: str
    streams: tuple[AudioStream, ...]


def _text(value: object, limit: int = 500) -> str:
    if not isinstance(value, (str, int)):
        return ""
    result = str(value).strip()
    if len(result) > limit or any(character in result for character in "\r\n\0"):
        return ""
    return result


def _stable_id(server_identity: str, client_id: str, serial: str) -> str:
    value = json.dumps([server_identity, client_id, serial], separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_sink_inputs(rows: object, *, server_identity: str) -> list[AudioStream]:
    """Parse fresh pactl JSON; a stream without a stable serial is not selectable."""
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        index = row.get("index")
        if type(index) is not int or index < 0:
            continue
        properties = row.get("properties")
        if not isinstance(properties, Mapping):
            properties = {}
        client_id = _text(row.get("client"), 100)
        serial = _text(properties.get("object.serial"), 100)
        pid = _text(properties.get("application.process.id"), 20)
        if not pid.isdigit():
            pid = ""
        name = _text(properties.get("application.name"))
        binary = _text(properties.get("application.process.binary"))
        application_id = _text(properties.get("application.id"))
        label = name or binary or (f"Aplikacja PID {pid}" if pid else "Strumień bez metadanych")
        result.append(
            AudioStream(
                stream_id=(
                    _stable_id(server_identity, client_id, serial)
                    if serial
                    else uuid4().hex
                ),
                stream_index=index,
                client_id=client_id,
                server_identity=server_identity,
                serial=serial,
                pid=pid,
                application_name=name,
                application_binary=binary,
                application_id=application_id,
                label=label,
                sink_index=row.get("sink") if type(row.get("sink")) is int else None,
                corked=row.get("corked") if type(row.get("corked")) is bool else None,
            )
        )
    return result


def group_streams(streams: list[AudioStream]) -> list[AudioApplication]:
    """Group related streams while preserving each stream as a choice."""
    groups: dict[tuple[str, ...], list[AudioStream]] = {}
    for stream in streams:
        metadata = (
            stream.application_id,
            stream.application_name,
            stream.application_binary,
        )
        if stream.pid:
            key = ("pid", stream.pid)
        elif any(metadata):
            key = ("application", *metadata)
        else:
            key = ("stream", stream.stream_id)
        groups.setdefault(key, []).append(stream)
    result = []
    for key, items in groups.items():
        first = items[0]
        result.append(
            AudioApplication(
                group_id=hashlib.sha256(json.dumps(key).encode("utf-8")).hexdigest(),
                pid=first.pid,
                label=first.label,
                streams=tuple(items),
            )
        )
    return result


def resolve_current_stream(stream_id: str, current: list[AudioStream]) -> AudioStream:
    """Resolve an opaque selection against fresh discovery, never a saved index."""
    matches = [stream for stream in current if stream.stream_id == stream_id]
    if len(matches) != 1:
        raise AudioStreamUnavailable("Wybrany strumień audio zniknął lub zmienił tożsamość")
    return matches[0]


def build_record_command(stream: AudioStream, *, executable: str = "parec") -> list[str]:
    """Request server-side conversion to the recognizer's PCM contract."""
    if not stream.serial:
        raise AudioStreamUnavailable(
            "Serwer audio nie udostępnia stabilnej tożsamości wybranego strumienia"
        )
    return [
        executable,
        "--raw",
        "--format=s16le",
        "--rate=16000",
        "--channels=1",
        f"--monitor-stream={stream.stream_index}",
        "--client-name=NarratorVerse ASR",
        "--stream-name=Selected application stream",
    ]


class PulseAudioDiscovery:
    """Read-only pactl discovery compatible with PulseAudio and PipeWire-Pulse."""

    def __init__(self, *, executable: str | None = None, runner=subprocess.run):
        self.executable = executable or shutil.which("pactl") or "pactl"
        self.runner = runner
        self._selection_ids: dict[str, str] = {}
        self._last_discovered_ids: set[str] = set()

    def _json(self, *args: str) -> object:
        result = self.runner(
            [self.executable, "--format=json", *args],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        if len(result.stdout) > 4 * 1024 * 1024:
            raise RuntimeError("Odpowiedź serwera audio jest zbyt duża")
        return json.loads(result.stdout)

    def list_streams(self) -> list[AudioStream]:
        info = self._json("info")
        if not isinstance(info, Mapping) or info.get("is_local") not in ("yes", True):
            raise RuntimeError("Wymagany jest lokalny serwer PulseAudio")
        identity = json.dumps(
            [info.get("cookie", ""), info.get("host_name", ""), info.get("user_name", "")],
            separators=(",", ":"),
        )
        rows = self._json("list", "sink-inputs")
        streams = parse_sink_inputs(rows, server_identity=identity)
        current_ids = {stream.stream_id for stream in streams}
        for vanished in self._last_discovered_ids - current_ids:
            self._selection_ids.pop(vanished, None)
        self._last_discovered_ids = current_ids
        return [
            AudioStream(
                stream_id=self._selection_ids.setdefault(stream.stream_id, uuid4().hex),
                stream_index=stream.stream_index,
                client_id=stream.client_id,
                server_identity=stream.server_identity,
                serial=stream.serial,
                pid=stream.pid,
                application_name=stream.application_name,
                application_binary=stream.application_binary,
                application_id=stream.application_id,
                label=stream.label,
                sink_index=stream.sink_index,
                corked=stream.corked,
            )
            for stream in streams
        ]

    def resolve(self, stream_id: str) -> AudioStream:
        return resolve_current_stream(stream_id, self.list_streams())

    def record_command(self, stream_id: str) -> list[str]:
        stream = self.resolve(stream_id)
        sinks = self._json("list", "sinks")
        matches = [row for row in sinks if isinstance(row, Mapping)
                   and row.get("index") == stream.sink_index] if isinstance(sinks, list) else []
        monitor = _text(matches[0].get("monitor_source")) if len(matches) == 1 else ""
        if not monitor:
            raise AudioStreamUnavailable("Nie można ustalić monitora wybranego wyjścia audio")
        if self.resolve(stream_id) != stream:
            raise AudioStreamUnavailable("Wybrany strumień zmienił wyjście podczas uruchamiania")
        return build_record_command(stream) + [f"--device={monitor}", "--latency-msec=100"]


async def capture_pcm(discovery: PulseAudioDiscovery, stream_id: str, *,
                      poll_interval: float = .25, no_pcm_timeout: float = 5):
    if poll_interval <= 0 or no_pcm_timeout <= 0:
        raise ValueError("Interwał kontroli capture i limit bez PCM muszą być dodatnie")
    selected = await asyncio.to_thread(discovery.resolve, stream_id)
    command = await asyncio.to_thread(discovery.record_command, stream_id)
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=32000,
    )
    pending = None
    stderr_tail = bytearray()

    async def collect_stderr():
        stderr = getattr(process, "stderr", None)
        if stderr is not None:
            while block := await stderr.read(1024):
                stderr_tail.extend(block)
                del stderr_tail[:-2048]

    stderr_task = asyncio.create_task(collect_stderr())

    def process_error():
        detail = stderr_tail.decode("utf-8", errors="replace").strip()
        code = process.returncode
        return AudioStreamUnavailable(
            f"Proces capture zakończył dostarczanie PCM (kod {code})."
            + (f" {detail}" if detail else "")
        )

    loop = asyncio.get_running_loop()
    last_progress = loop.time()
    try:
        if await asyncio.to_thread(discovery.resolve, stream_id) != selected:
            raise AudioStreamUnavailable("Wybrany strumień zmienił tożsamość podczas startu capture")
        while True:
            if pending is None:
                pending = asyncio.create_task(process.stdout.readexactly(3200))
            # Do not cancel readexactly on an idle poll: it may hold partial PCM.
            done, _ = await asyncio.wait({pending}, timeout=poll_interval)
            current = await asyncio.to_thread(discovery.resolve, stream_id)
            if current != selected:
                raise AudioStreamUnavailable("Wybrany strumień zmienił tożsamość lub wyjście")
            if process.returncode is not None:
                raise process_error()
            if done:
                try:
                    pcm = pending.result()
                except asyncio.IncompleteReadError as error:
                    raise process_error() from error
                pending = None
                last_progress = loop.time()
                yield pcm
            elif current.corked is True:
                # A paused source is still selected, but need not produce bytes.
                # No fabricated silence and no restart/fallback to another source.
                last_progress = loop.time()
            elif loop.time() - last_progress >= no_pcm_timeout:
                raise AudioStreamUnavailable(
                    f"Brak PCM z wybranego strumienia przez {no_pcm_timeout:g} sekund; "
                    "źródło nie zgłasza pauzy (zastój capture lub nieaktywne źródło)."
                )
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            process.kill()
            await process.wait()
        finally:
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)