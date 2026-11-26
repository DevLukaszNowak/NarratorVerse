"""Isolated CPU Whisper child with capture of one application stream."""
import argparse
import asyncio
from contextlib import aclosing
from dataclasses import asdict
import json
import math
import os
import shutil
import signal
from pathlib import Path
import sys
from threading import Thread, Lock
import time

# Direct file entrypoint uses the independent venv; never app runtime PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from narratorverse.asr_segments import CoverageSegmenter, PRESETS
from narratorverse.audio_capture import PulseAudioDiscovery, capture_pcm, group_streams
from narratorverse.asr_process import is_own_stream


OUTPUT_LOCK = Lock()


def emit(row):
    with OUTPUT_LOCK:
        print(json.dumps(row, ensure_ascii=False), flush=True)


def own_pids():
    result = {str(os.getpid()), str(os.getppid())}
    # Reject the calling backend and known ancestors even without application metadata.
    pid = os.getppid()
    for _ in range(8):
        try:
            status = Path(f'/proc/{pid}/status').read_text()
            pid = int(next(line.split()[1] for line in status.splitlines() if line.startswith('PPid:')))
            if pid <= 1: break
            result.add(str(pid))
        except (OSError, StopIteration, ValueError):
            break
    return result


def selectable(discovery):
    pids = own_pids()
    return [stream for stream in discovery.list_streams() if stream.serial and not is_own_stream(stream, pids)]


def resolve_selection(discovery, selection):
    if not isinstance(selection, dict) or selection.get('capture_kind', 'application') != 'application':
        raise RuntimeError('Wybierz strumień konkretnej aplikacji do rozpoznawania mowy.')
    # Opaque IDs are scoped to discovery. Compare complete original identity on
    # this fresh process; never trust a recycled numeric stream index alone.
    fields = ('stream_index','client_id','server_identity','serial','pid','application_name',
              'application_binary','application_id','sink_index')
    matches = [s for s in selectable(discovery) if all(getattr(s,k) == selection.get(k) for k in fields)]
    if len(matches) != 1:
        raise RuntimeError('Wybrany strumień zniknął lub zmienił tożsamość. Odśwież listę.')
    return matches[0]


class CausalVad:
    def __init__(self):
        import numpy as np
        from faster_whisper.vad import get_vad_model
        self.np = np
        self.session = get_vad_model().session
        self.h = np.zeros((1,1,128), dtype=np.float32)
        self.c = np.zeros((1,1,128), dtype=np.float32)
        self.context = np.zeros((1,64), dtype=np.float32)

    def feed(self, pcm):
        np = self.np
        block = np.frombuffer(pcm, dtype='<i2').astype(np.float32).reshape(1,512) / 32768
        value, self.h, self.c = self.session.run(None, {
            'input': np.concatenate([self.context, block], axis=1), 'h':self.h, 'c':self.c})
        self.context = block[:,-64:].copy()
        return float(value.reshape(-1)[0])


def decode(model, pcm):
    import numpy as np
    start = time.monotonic()
    segments, _ = model.transcribe(np.frombuffer(pcm,dtype='<i2').astype(np.float32)/32768,
        language='en', task='transcribe', beam_size=5, temperature=0,
        condition_on_previous_text=False, word_timestamps=False, vad_filter=False)
    text = ' '.join(s.text.strip() for s in segments)
    return text, (time.monotonic()-start)*1000


async def recognize(source, model, preset, output=emit, *, diagnostics=False):
    vad = CausalVad()
    segmenter = CoverageSegmenter(preset)
    pending = asyncio.Queue(maxsize=2)
    origin = None
    received = 0
    frame_buffer = bytearray()
    segment_buffer = bytearray()
    position = 0
    sequence = 0
    vad_ms = 0.
    collect_started = None

    def diagnostic(event, **fields):
        if diagnostics:
            try:
                output(dict(kind='diagnostic',event=event,observed_mono=time.monotonic(),**fields))
            except Exception:
                pass

    def timed_decode(row, pcm):
        diagnostic('decode_started',id=row['id'])
        try:
            return decode(model,pcm)
        finally:
            diagnostic('decode_done',id=row['id'])

    def enqueue(row):
        nonlocal sequence, segment_buffer, vad_ms,collect_started
        sequence += 1
        ready = time.monotonic()
        length = (row['end_sample']-row['start_sample'])*2
        pcm = bytes(segment_buffer[:length])
        del segment_buffer[:length]
        if len(pcm) != length:
            raise RuntimeError('Niekompletny fragment PCM')
        payload = {**row,'id':sequence,'ready_mono':ready,
            'collect_started_mono':collect_started,'collect_wall_ms':(ready-collect_started)*1000,
            'audio_end_mono_estimate':origin+row['end_sample']/16000,
            'speech_end_mono_estimate':origin+row['speech_end_sample']/16000
                if row['speech_end_sample'] is not None else None,
            'collect_ms':length/32,'vad_compute_ms':vad_ms,
            'timing_warning':'Audio timeline anchored to first received block; Pulse buffering and speech end are estimates.'}
        if diagnostics:
            payload['segment_start_mono_estimate']=origin+row['start_sample']/16000
            diagnostic('segment_closed',**payload)
        vad_ms = 0.
        collect_started = ready
        try:
            pending.put_nowait((payload,pcm))
        except asyncio.QueueFull as error:
            raise RuntimeError('Kolejka Whisper pełna (2 fragmenty). Zatrzymano zamiast gubić audio.') from error

    async def produce():
        nonlocal origin,received,position,vad_ms,collect_started
        async with aclosing(source):
            collect_started=time.monotonic()
            async for block in source:
                if not block or len(block)%2:
                    raise ValueError('Nieprawidłowe PCM capture')
                diagnostic('capture_received',start_sample=received,end_sample=received+len(block)//2,
                           pcm_bytes=len(block),basis='ASR receipt after capture iterator delivery, not device timestamp')
                if origin is None:
                    origin = time.monotonic()-len(block)/32000
                    output({'kind':'state','state':'listening','capture_origin_mono_estimate':origin})
                    diagnostic('segment_opened',id=sequence+1,start_sample=0)
                received += len(block)//2
                frame_buffer.extend(block)
                segment_buffer.extend(block)
                while len(frame_buffer)>=1024:
                    frame = bytes(frame_buffer[:1024]); del frame_buffer[:1024]
                    start = time.monotonic()
                    probability = vad.feed(frame)
                    vad_ms += (time.monotonic()-start)*1000
                    position += 512
                    for row in segmenter.feed(probability,position):
                        enqueue(row)
                        diagnostic('segment_opened',id=sequence+1,start_sample=row['end_sample'])
        for row in segmenter.flush(received): enqueue(row)
        await pending.put(None)

    async def consume():
        while True:
            item = await pending.get()
            if item is None: return
            row,pcm = item
            wait = (time.monotonic()-row['ready_mono'])*1000
            if wait>10000:
                raise RuntimeError('Kolejka Whisper przekroczyła 10 sekund; zatrzymano sesję.')
            asr_started=time.monotonic()
            if diagnostics:
                text,compute = await asyncio.to_thread(timed_decode,row,pcm)
            else:
                text,compute = await asyncio.to_thread(decode,model,pcm)
            output({**row,'kind':'transcript','text':text,'asr_compute_ms':compute,
                    'asr_queue_ms':wait,'asr_started_mono':asr_started,'asr_done_mono':time.monotonic()})

    tasks = [asyncio.create_task(produce()),asyncio.create_task(consume())]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


async def run(args, selection):
    discovery = PulseAudioDiscovery()
    selected = await asyncio.to_thread(resolve_selection,discovery,selection)
    loop = asyncio.get_running_loop()
    owner = asyncio.current_task()
    for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,owner.cancel)
    def control():
        os.read(sys.stdin.fileno(),1)  # No buffered lock held at interpreter exit.
        if not loop.is_closed():loop.call_soon_threadsafe(owner.cancel)
    Thread(target=control,daemon=True,name='asr-parent-watch').start()
    emit({'kind':'state','state':'loading'})
    start = time.monotonic()
    from faster_whisper import WhisperModel
    model = await asyncio.to_thread(WhisperModel,str(args.model),device='cpu',compute_type='int8',
                                    cpu_threads=4,num_workers=1,local_files_only=True)
    emit({'kind':'state','state':'ready','model_load_ms':(time.monotonic()-start)*1000,
          'device':'cpu','compute_type':'int8','model':'small','preset':args.preset})
    await recognize(capture_pcm(discovery,selected.stream_id),model,args.preset,diagnostics=args.diagnostics)


def probe_runtime():
    import ctranslate2
    from faster_whisper import WhisperModel

    return (WhisperModel is not None and 'int8' in ctranslate2.get_supported_compute_types('cpu')
            and math.isfinite(CausalVad().feed(bytes(1024)))
            and all(shutil.which(executable) for executable in ('pactl', 'parec')))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model',type=Path)
    parser.add_argument('--runtime',type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--list',action='store_true')
    mode.add_argument('--probe',action='store_true')
    parser.add_argument('--preset',choices=tuple(PRESETS),default='quick')
    parser.add_argument('--diagnostics',action='store_true')
    args = parser.parse_args()
    if args.runtime is not None:
        sys.path.insert(1, str(args.runtime))
    if args.probe:
        if not probe_runtime():
            raise SystemExit(1)
        emit({'ready': True})
        return
    if args.list:
        emit([asdict(app) for app in group_streams(selectable(PulseAudioDiscovery()))])
        return
    try:
        if args.model is None:
            raise RuntimeError('Brak modelu Whisper small.')
        line = sys.stdin.buffer.readline(16385)
        if len(line)>16384: raise ValueError('Zbyt duży wybór audio')
        selection = json.loads(line)
        asyncio.run(run(args,selection))
    except asyncio.CancelledError:
        pass
    except Exception as error:
        emit({'kind':'error','message':str(error)})
        raise SystemExit(1)


if __name__ == '__main__':
    main()
