"""Opt-in observer of existing Qt output. Does not alter playback or OCR."""
import time
from PySide6.QtCore import QObject, QTimer


class PlaybackProbe(QObject):
    def __init__(self,audio,callback,*,clock=time.monotonic):
        super().__init__()
        self.audio,self.callback,self.clock=audio,callback,clock
        self.generation=self.request_id=None
        self.previous=None
        self.timer=QTimer(self)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self.poll)

    def watch(self,generation,request_id):
        self.generation,self.request_id=generation,request_id
        self.previous=self.clock()
        self.timer.start()

    def stop(self):
        self.timer.stop()
        self.generation=self.request_id=None

    def poll(self):
        if self.request_id is None:return
        now=self.clock()
        # Adapter is intentionally specific to the existing Qt provider; no
        # second audio engine, no change to the OCR playback callbacks.
        current=getattr(self.audio,'_current',None)
        sink=getattr(self.audio,'_sink',None)
        if current is None or current.request_id!=self.request_id or sink is None:
            return
        try:processed=int(sink.processedUSecs())
        except (AttributeError,RuntimeError,TypeError,ValueError):return
        if processed>0:
            generation,request_id=self.generation,self.request_id
            previous=self.previous
            self.stop()
            self.callback(generation,request_id,previous,now,processed)
        else:
            self.previous=now