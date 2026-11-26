"""ASR orchestration sharing translation, speech engines and audio."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from dataclasses import asdict, is_dataclass
import logging
import time
from uuid import uuid4

from PySide6.QtCore import QObject, Signal, Qt, QTimer
from .asr_process import AsrProcess
from .asr_segments import PRESETS
from .asr_playback_probe import PlaybackProbe
from game_optimization_linux.services.narrator_adaptive_rate import AdaptiveSpeechRate
from game_optimization_linux.services.narrator_pipeline import limit_repeated_speech_words
from game_optimization_linux.services.narrator_tts import PIPER_PROVIDER_ID
from .quick_read import validate_options
from .tts_router import OcrTtsProvider, provider_epoch, resolve_speech, synthesis_options


class AsrPanel(QObject):
    incoming = Signal(object)
    finished = Signal(object)

    def __init__(self, translator, tts, audio, emit, *, worker=None, diagnostic=None):
        super().__init__()
        self.translator,self.tts,self.audio,self.emit = translator,tts,audio,emit
        self.worker = worker or AsrProcess()
        self.pool = ThreadPoolExecutor(max_workers=1,thread_name_prefix='narratorverse-asr-stages')
        self.cleanup = ThreadPoolExecutor(max_workers=1,thread_name_prefix='narratorverse-asr-stop')
        self.state,self.error = 'idle',''
        self.generation = self.tts_revision = 0
        self.last_segment_id = 0
        self.closed = False
        self.tts_enabled = False
        self.pending = deque()
        self.rows = deque(maxlen=30)
        self.current = None
        self.options = {}
        self.adaptive_rate = AdaptiveSpeechRate()
        self.effective_speech_rate = None
        self.provider, self.voice_id = None, ''
        self.speech_epoch = None
        self.prepare_error_revision = None
        self.diagnostic = diagnostic
        self.session_id = ''
        self.diagnostic_stopped = True
        self.diagnostic_timer = QTimer(self)
        self.diagnostic_timer.setInterval(1000)
        self.diagnostic_timer.timeout.connect(self.diagnostic_tick)
        self.cancelled = Event()
        self.model_load_ms = None
        self.piper_prepare_ms = None
        self.tts_prepare_ms = None
        self.playback_probe = PlaybackProbe(audio,self._audio_progress)
        self.incoming.connect(self._incoming,Qt.ConnectionType.QueuedConnection)
        self.finished.connect(self._finished,Qt.ConnectionType.QueuedConnection)

    @property
    def busy(self):
        return self.state not in ('idle','stopped','error')

    def snapshot(self):
        return {'state':self.state,'error':self.error,'tts_enabled':self.tts_enabled,
                'preset':self.options.get('preset','quick'),'pending':len(self.pending),
                'rows':list(self.rows),'model_load_ms':self.model_load_ms,
                'tts_prepare_ms':self.tts_prepare_ms,
                'effective_speech_rate':self.effective_speech_rate,
                'piper_prepare_ms':self.piper_prepare_ms,
                'timing_basis':'monotonic; audio/speech endpoints estimated; playback callback is sink submission, not acoustic output'}

    def notify(self):
        self.emit('asr',self.snapshot())

    def _trace(self, event, *, session_id=None, **fields):
        if self.diagnostic is not None:
            try:
                self.diagnostic(event, origin='asr', session_id=session_id or self.session_id, **fields)
            except Exception:
                logging.getLogger(__name__).exception('ASR diagnostic observer failed')

    def diagnostic_tick(self):
        if self.diagnostic is None:return
        now=time.monotonic()
        def describe(record):
            source=record['source']
            anchor=source.get('segment_start_mono_estimate',source.get('collect_started_mono',source['ready_mono']))
            return dict(id=record['id'],state=record['state'],audio_duration_s=record.get('audio_duration_s'),
                        segment_age_s=max(0.,now-anchor),
                        downstream_age_s=max(0.,now-source.get('downstream_queued_mono',now)))
        pending=[describe(record) for _,record in self.pending]
        active=describe(self.current) if self.current is not None else None
        records=pending+([active] if active else [])
        remaining=0.
        if active and active['audio_duration_s'] is not None:
            elapsed=max(0.,now-self.current['source'].get('sink_started_mono',now))
            remaining=max(0.,active['audio_duration_s']-elapsed)
        self._trace('asr_queue_snapshot',pending_count=len(pending),pending=pending,active=active,
                    oldest_segment_age_s=max((r['segment_age_s'] for r in records),default=0.),
                    known_audio_remaining_s=remaining,
                    unknown_audio_count=sum(r['audio_duration_s'] is None for r in records),
                    remaining_basis='WAV duration minus callback elapsed estimate; unknown future WAV lengths excluded')

    def start(self, selection, preset, tts_enabled, options):
        if preset not in PRESETS: raise ValueError('Wybierz wariant early, quick albo context')
        if type(tts_enabled) is not bool: raise ValueError('Nieprawidłowy przełącznik lektora')
        if self.closed or self.busy: raise RuntimeError('Zatrzymaj poprzednią sesję ASR')
        if not self.translator.available: raise RuntimeError('Brak istniejącego tłumacza EN→PL. Sprawdź Komponenty.')
        options = {**options, **validate_options(options)}
        provider, voice_id = resolve_speech(self.tts, options)
        if tts_enabled:
            OcrTtsProvider.require_voice(provider, voice_id)
        self.generation += 1
        generation = self.generation
        self.session_id = uuid4().hex
        self.diagnostic_stopped = False
        self.options = {**options,'preset':preset}
        self.provider, self.voice_id = provider, voice_id
        self.speech_epoch = provider_epoch(provider)
        self.prepare_error_revision = None
        self.cancelled = Event()
        self.tts_enabled = tts_enabled
        self.pending.clear();self.rows.clear();self.current=None
        self.last_segment_id = 0
        self.adaptive_rate.reset();self.effective_speech_rate=options['speech_rate']
        self.error='';self.model_load_ms=None;self.piper_prepare_ms=None;self.tts_prepare_ms=None;self.state='loading'
        self._trace('asr_session_started',options=dict(self.options),selection=selection,tts_enabled=tts_enabled,
                    timing_basis='monotonic; capture receipt after Pulse/PipeWire delivery; endpoints estimated; callbacks not acoustic')
        if self.diagnostic is not None:self.diagnostic_timer.start()
        try:
            self.worker.start(selection,preset,lambda row:self.incoming.emit((generation,row)))
        except Exception as error:
            self.state,self.error='error',str(error)
            self._trace('asr_session_stopped',error=str(error))
            self.diagnostic_stopped = True
            self.diagnostic_timer.stop()
            self.notify()
            raise
        if tts_enabled:
            self._prepare_voice()
        self.notify()
        return self.snapshot()

    def _prepare_voice(self):
        generation, cancelled, revision = self.generation, self.cancelled, self.tts_revision
        provider, voice_id, epoch = self.provider, self.voice_id, self.speech_epoch
        request = {'expected_epoch': epoch} if epoch is not None else {}

        def work():
            if cancelled.is_set() or revision != self.tts_revision:return
            started=time.monotonic()
            try:
                provider.prepare(voice_id, **request)
            except Exception as error:
                if not cancelled.is_set() and revision == self.tts_revision:
                    self.prepare_error_revision = revision
                    self.finished.emit({'generation':generation,'revision':revision,
                                        'prepared':True,'error':str(error)})
                return
            if not cancelled.is_set():
                self.finished.emit({'generation':generation,'revision':revision,'prepared':True,
                    'tts_prepare_ms':(time.monotonic()-started)*1000})
        self.pool.submit(work)

    def _incoming(self, payload):
        generation,row=payload
        if generation==self.generation: self.receive(row)

    def receive(self, row):
        if not self.busy or self.state=='stopping': return
        kind=row.get('kind')
        if kind=='diagnostic':
            fields={k:v for k,v in row.items() if k not in ('kind','event')}
            self._trace('asr_worker_'+row['event'],**fields)
            return
        if kind=='error':
            self._trace('asr_worker_error',message=row.get('message'))
            self.stop(str(row.get('message','Błąd ASR')));return
        if kind=='state':
            self._trace('asr_worker_state',source=dict(row))
            self.state=row['state']
            if 'model_load_ms' in row:self.model_load_ms=row['model_load_ms']
            self.notify();return
        if kind!='transcript' or row['id'] <= self.last_segment_id:return
        self.last_segment_id = row['id']
        self._trace('asr_transcript',request_id=row['id'],source=dict(row))
        record={'id':row['id'],'text':row['text'],'translation':'','state':'queued',
                'metrics':{key:row[key] for key in ('collect_ms','collect_wall_ms','asr_queue_ms','asr_compute_ms','vad_compute_ms') if key in row},
                'source':dict(row)}
        self.rows.appendleft(record)
        if not row['text'].strip():
            record['state']='empty';self.notify();return
        if len(self.pending)>=3:
            self._trace('asr_downstream_rejected',request_id=row['id'],reason='existing_queue_limit')
            self.stop('Kolejka tłumaczenia/lektora pełna. Zatrzymano bez cichego pomijania dialogów.');return
        queued=time.monotonic()
        if self.diagnostic is not None:
            record['source']['downstream_queued_mono']=queued
            self._trace('asr_downstream_queued',request_id=record['id'],observed_mono=queued,
                        blocked_by_id=self.current['id'] if self.current else None,
                        blocked_by_state=self.current['state'] if self.current else None)
        self.pending.append((queued,record))
        self._pump()
        self.notify()

    def _pump(self):
        if self.current is not None or not self.pending or not self.busy or self.state=='stopping':return
        queued,record=self.pending.popleft()
        if time.monotonic()-queued>20:
            self.stop('Oczekiwanie na tłumaczenie/lektora przekroczyło 20 s.');return
        self.current=record
        generation,cancelled=self.generation,self.cancelled
        session_id=self.session_id
        trace=lambda event,**fields:self._trace(event,session_id=session_id,request_id=record['id'],**fields)
        revision=self.tts_revision
        enabled=self.tts_enabled
        options=dict(self.options)
        provider, voice_id = self.provider, self.voice_id
        arguments = synthesis_options(provider, options, voice_id, self.speech_epoch)
        if enabled:
            arguments['speech_rate'] = self.effective_speech_rate = self.adaptive_rate.select(
                base_rate=options['speech_rate'], enabled=options.get('adaptive_speech_rate') is True,
                accepted_at=queued, text=record['text'], backlog=bool(self.pending),
                max_boost=options.get('adaptive_max_boost', .5), phrase_id=record['id'])
        piper = options['tts_provider_id'] == PIPER_PROVIDER_ID
        record['metrics']['downstream_queue_ms']=(time.monotonic()-queued)*1000
        trace('asr_downstream_dequeued',queue_ms=record['metrics']['downstream_queue_ms'])
        record['state']='translating'

        def work():
            if cancelled.is_set():return {}
            start=time.monotonic()
            trace('asr_translation_started',observed_mono=start)
            result=self.translator.translate(record['text'],source_language='en',target_language='pl',
                                              profile_id=options.get('translation_profile_id','balanced'))
            finished=time.monotonic()
            trace('asr_translation_done',observed_mono=finished,call_ms=(finished-start)*1000,
                  provider_id=getattr(result,'provider_id',''),text=result.translated_text)
            payload={'translation':result.translated_text,'translation_call_ms':(finished-start)*1000,
                     'translation_started_mono':start,'translation_done_mono':finished}
            if cancelled.is_set():return payload
            if enabled and revision==self.tts_revision and revision!=self.prepare_error_revision and self.tts_enabled:
                start=time.monotonic()
                speech = {'speech_rate': arguments['speech_rate'], 'voice_id': voice_id,
                          'provider_id': options['tts_provider_id']}
                trace('asr_piper_started' if piper else 'asr_tts_started',observed_mono=start,**speech)
                try:
                    pcm=provider.synthesize(limit_repeated_speech_words(result.translated_text), **arguments)
                except Exception as error:
                    if revision==self.tts_revision and self.tts_enabled:
                        payload['tts_error'] = str(error)
                    return payload
                elapsed = (time.monotonic()-start)*1000
                if self.diagnostic is not None:
                    samples=getattr(pcm,'samples',None)
                    duration=len(samples)/(2*pcm.sample_rate*pcm.channels) if isinstance(samples,bytes) else None
                    trace('asr_piper_done' if piper else 'asr_tts_done',observed_mono=time.monotonic(),call_ms=elapsed,
                          compute_ms=pcm.inference_ms,startup_ms=pcm.worker_startup_ms,
                          audio_duration_s=duration,**speech)
                metrics = {'call_ms': elapsed, 'compute_ms': pcm.inference_ms,
                           'startup_ms': pcm.worker_startup_ms, 'started_mono': start,
                           'done_mono': time.monotonic()}
                payload.update(pcm=pcm, **{'tts_'+key:value for key,value in metrics.items()})
                if piper:
                    payload.update({'piper_'+key:value for key,value in metrics.items()})
            return payload

        def done(future):
            try:payload=future.result()
            except Exception as error:
                payload={'error':str(error)}
                trace('asr_stage_error',message=str(error))
            self.finished.emit({'generation':generation,'record':record,'revision':revision,**payload})
        self.pool.submit(work).add_done_callback(done)

    def _finished(self, payload):
        if self.closed or payload['generation']!=self.generation:return
        if payload.get('stopped'):
            if payload.get('cleanup_error'):self.error=payload['cleanup_error']
            self.state='error' if self.error else 'stopped'
            self._trace('asr_session_stopped',error=self.error)
            self.diagnostic_stopped = True
            self.notify();return
        if payload.get('prepared'):
            if payload['revision']!=self.tts_revision:return
            if payload.get('error'):
                self.stop('Nie udało się przygotować głosu. '+payload['error']);return
            self.tts_prepare_ms=payload['tts_prepare_ms']
            if self.options['tts_provider_id']==PIPER_PROVIDER_ID:
                self.piper_prepare_ms=self.tts_prepare_ms
                self._trace('asr_piper_prepared',prepare_ms=self.piper_prepare_ms)
            else:
                self._trace('asr_tts_prepared',prepare_ms=self.tts_prepare_ms,
                            provider_id=self.options['tts_provider_id'])
            self.notify();return
        record=payload['record']
        if payload.get('error'):
            record['state']='error';self.stop(payload['error']);return
        record['translation']=payload.get('translation','')
        if payload.get('tts_error') and self.tts_enabled and payload['revision']==self.tts_revision:
            record['state']='error';self.stop(payload['tts_error']);return
        for key in ('translation_call_ms','tts_call_ms','tts_compute_ms','tts_startup_ms',
                    'piper_call_ms','piper_compute_ms','piper_startup_ms'):
            if key in payload:record['metrics'][key]=payload[key]
        for key in ('translation_started_mono','translation_done_mono','tts_started_mono','tts_done_mono',
                    'piper_started_mono','piper_done_mono'):
            if key in payload:record['source'][key]=payload[key]
        if payload.get('pcm') is not None and self.tts_enabled and payload['revision']==self.tts_revision:
            record['state']='waiting_audio'
            pcm=payload['pcm']
            width={'u8':1,'s16le':2,'s32le':4,'f32le':4}.get(str(getattr(pcm,'sample_format','s16le')).casefold(),2)
            if isinstance(getattr(pcm,'samples',None),bytes):
                self.adaptive_rate.synthesized(record['id'],len(pcm.samples)/max(1,pcm.sample_rate*pcm.channels*width))
            if self.diagnostic is not None:
                pcm=payload['pcm'];samples=getattr(pcm,'samples',None)
                record['audio_duration_s']=len(samples)/(2*pcm.sample_rate*pcm.channels) if isinstance(samples,bytes) else None
                self._trace('asr_audio_submitted',request_id=record['id'],audio_duration_s=record['audio_duration_s'])
            self.audio.play(payload['pcm'],volume=self.options['volume'],request_id=record['id'],text=record['translation'],
                started_callback=lambda wait:self._playing(payload['generation'],record,wait),
                completed_callback=lambda:self._played(payload['generation'],record),
                terminal_callback=lambda:self._terminal(payload['generation'],record),
                error_callback=lambda message:self._play_error(payload['generation'],message))
        else:
            record['state']='translated';self.current=None;self._pump()
        self.notify()

    def _playing(self, generation, record, wait):
        if generation!=self.generation or not self.tts_enabled:return
        now=time.monotonic();source=record['source'];metrics=record['metrics']
        source['sink_started_mono']=now
        metrics.update(audio_queue_ms=wait,ready_to_playback_ms=(now-source['ready_mono'])*1000,
            since_audio_end_estimate_ms=(now-source['audio_end_mono_estimate'])*1000,
            since_speech_end_estimate_ms=(now-source['speech_end_mono_estimate'])*1000
                if source.get('speech_end_mono_estimate') is not None else None)
        self.playback_probe.watch(generation,record['id'])
        self._trace('asr_playback_started',request_id=record['id'],observed_mono=now,
                    audio_queue_ms=wait,metrics=dict(metrics))
        record['state']='playing';self.notify()

    def _audio_progress(self,generation,request_id,previous,observed,processed):
        if generation!=self.generation:return
        record=next((r for r in self.rows if r['id']==request_id),None)
        if record is None:return
        source=record['source'];metrics=record['metrics']
        source['sink_progress_observed_mono']=observed
        metrics.update(playback_observation_interval_ms=(observed-previous)*1000,
            sink_processed_us_at_observation=processed,
            ready_to_audio_progress_ms=(observed-source['ready_mono'])*1000,
            since_speech_end_to_progress_estimate_ms=(observed-source['speech_end_mono_estimate'])*1000
                if source.get('speech_end_mono_estimate') is not None else None)
        self._trace('asr_playback_progress',request_id=request_id,observed_mono=observed,metrics=dict(metrics))
        self.notify()

    def _played(self,generation,record):
        if generation!=self.generation or self.current is not record:return
        self.playback_probe.stop()
        self._trace('asr_playback_completed',request_id=record['id'])
        record['state']='played';self.current=None;self._pump();self.notify()

    def _play_error(self,generation,message):
        if generation==self.generation:self.stop(message)

    def _terminal(self,generation,record):
        if generation!=self.generation or self.current is not record:return
        last=self.audio.last_playback
        if self.diagnostic is not None:
            self._trace('asr_playback_terminal',request_id=record['id'],
                        playback=asdict(last) if is_dataclass(last) else None)
        if last is not None and last.request_id==record['id'] and last.result!='completed':
            self.stop('Odtwarzanie mowy przerwane: '+last.reason)

    def set_tts(self, enabled):
        if type(enabled) is not bool:raise ValueError('Nieprawidłowy przełącznik lektora')
        if self.tts_enabled == enabled:return self.snapshot()
        if enabled and self.busy and self.provider is not None:
            OcrTtsProvider.require_voice(self.provider,self.voice_id)
        self._trace('asr_tts_changed',enabled=enabled)
        self.tts_enabled=enabled;self.tts_revision+=1
        if enabled and self.busy and self.provider is not None:
            self._prepare_voice()
        if not enabled and self.busy:
            self.playback_probe.stop()
            self.audio.stop()
            # Do not cancel the shared provider under an active call. Revision
            # invalidates its result; explicit session Stop cancels inference.
            if self.current is not None and self.current['state'] in ('waiting_audio','playing'):
                self.current['state']='tts_disabled';self.current=None;self._pump()
        self.notify()
        return self.snapshot()

    def stop(self, error=''):
        if not self.busy or self.state=='stopping':return self.snapshot()
        self.diagnostic_tick()
        self._trace('asr_session_stopping',error=error,
                    unfinished_ids=([self.current['id']] if self.current else [])+[r['id'] for _,r in self.pending])
        self.diagnostic_timer.stop()
        self.error=error;self.state='stopping';self.generation+=1
        generation=self.generation
        self.cancelled.set();self.pending.clear();self.current=None
        self.playback_probe.stop()
        self.audio.stop();self.notify()
        provider=self.provider
        def cleanup():
            self.worker.stop()
            self.translator.cancel();provider.cancel()
            # A worker can be constructed concurrently with cancel. Serial
            # barrier waits for stage work, then closes that late worker too.
            self.pool.submit(lambda:(self.translator.close(),provider.close())).result()
            self.finished.emit({'generation':generation,'stopped':True})
        def completed(future):
            try:future.result()
            except Exception as failure:
                self.finished.emit({'generation':generation,'stopped':True,'cleanup_error':str(failure)})
        self.cleanup.submit(cleanup).add_done_callback(completed)
        return self.snapshot()

    def shutdown(self):
        self.stop();self.cleanup.shutdown(wait=True,cancel_futures=False)
        self.closed=True
        self.pool.shutdown(wait=True,cancel_futures=True)
        if not self.diagnostic_stopped:
            self._trace('asr_session_stopped',error=self.error,shutdown=True)
            self.diagnostic_stopped=True
