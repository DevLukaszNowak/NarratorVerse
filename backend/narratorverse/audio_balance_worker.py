"""Independent restoration owner. EOF or expired heartbeat releases ducking."""
import fcntl
import json
import logging
import os
from pathlib import Path
import queue
import sys
import tempfile
from threading import Thread
import time

from .audio_balance import BalanceEngine, Pactl, validate_target


logger = logging.getLogger(__name__)


def write_journal(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.audio-balance-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(entries, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validated_message(data):
    if (not isinstance(data, dict) or type(data.get('enabled')) is not bool
            or type(data.get('active')) is not bool or type(data.get('reading')) is not bool
            or isinstance(data.get('db'), bool)
            or not isinstance(data.get('db'), (int, float))
            or type(data.get('epoch', 0)) is not int or data.get('epoch', 0) < 0
            or not -24 <= data['db'] <= -3):
        raise ValueError('Invalid audio balance command')
    return {**data, 'target': validate_target(data.get('target'))}


def receive(messages):
    try:
        for line in sys.stdin:
            if len(line) > 4096:
                break
            data = validated_message(json.loads(line))
            # Latest state, bounded memory, no queue of obsolete heartbeats.
            try:
                messages.get_nowait()
            except queue.Empty:
                pass
            messages.put_nowait((time.monotonic(), data))
    except (ValueError, OSError, queue.Full):
        pass
    finally:
        # A separate EOF flag cannot be overwritten by a pending heartbeat.
        ended[0] = True


ended = [False]


def main(directory):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / 'audio-balance.lock').open('a') as lock:
        # Only one worker can own a journal or manipulate baselines.
        fcntl.flock(lock, fcntl.LOCK_EX)
        journal = root / 'audio-balance-recovery.json'
        mixer = Pactl()
        engine = BalanceEngine(mixer, lambda entries: write_journal(journal, entries))
        if journal.exists():
            saved = json.loads(journal.read_text())
            if not isinstance(saved, dict):
                raise ValueError('Invalid audio recovery journal')
            engine.entries = saved
        messages = queue.Queue(maxsize=1)
        Thread(target=receive, args=(messages,), daemon=True).start()
        options = {'enabled': False, 'target': '', 'db': -12.0, 'active': False, 'reading': False}
        received = 0
        configured = None
        last_status = None
        failure = ''
        recovering = bool(engine.entries)
        delay = 0 if recovering else .25
        while True:
            try:
                received, options = messages.get(timeout=delay)
            except queue.Empty:
                pass
            now = time.monotonic()
            exiting = ended[0]
            expired = now - received > 1.5
            reading = options['enabled'] and options['reading'] and not expired
            target = options['target'] if reading and not failure else ''
            streams = []
            try:
                streams = mixer.streams() if target or engine.entries else []
                if recovering or exiting or failure:
                    engine.restore(streams)
                    recovering = False
                    if target:
                        streams = mixer.streams()
                if exiting:
                    break
                config = (target, options['db'], options.get('epoch', 0))
                if config != configured:
                    had_entries = bool(engine.entries)
                    engine.configure(target, options['db'], streams)
                    configured = config
                    # Configuration may restore levels. Never re-capture the
                    # pre-restoration snapshot as the next original baseline.
                    if target and had_entries:
                        streams = mixer.streams()
                engine.step(streams, active=bool(options['active'] and not expired), now=now)
                state = 'error' if failure else 'choose' if reading and not target else engine.state
                status = {'state': state, 'error': failure}
            except Exception as error:
                # No more ducking after errors. Keep journal and retry release,
                # including after parent death while the audio server recovers.
                failure = failure or (str(error) or type(error).__name__)[:300]
                recovering = True
                engine.target = ''
                status = {'state': 'error', 'error': failure}
            if status != last_status:
                if status['state'] not in ('off', 'ducking', 'restoring'):
                    app = json.loads(options['target'])['application.name'] if options['target'] else None
                    logger.info('Audio balance event=selection state=%s app=%r pactl=%r inputs=%r detail=%r',
                                status['state'], app, mixer.executable,
                                [(stream.index, bool(stream.identity), stream.corked) for stream in streams if stream.target == target],
                                failure)
                try:
                    print(json.dumps(status), flush=True)
                except (BrokenPipeError, OSError):
                    ended[0] = True
                last_status = status
            if failure and not engine.entries:
                break
            delay = .5 if failure else .05 if target else .25


if __name__ == '__main__':
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    main(sys.argv[1])
