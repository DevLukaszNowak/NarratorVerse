"""Experimental endpoint-only segmentation; never gates away PCM."""
import math

PRESETS = {'early': 1, 'quick': 2, 'context': 4}


class CoverageSegmenter:
    def __init__(self, preset):
        if preset not in PRESETS:
            raise ValueError('Wybierz wariant early, quick albo context')
        self.minimum = PRESETS[preset] * 16000
        self.start = self.previous = 0
        self.speech_end = None
        self.active = False

    def feed(self, probability, end_sample):
        if (type(end_sample) is not int or end_sample <= self.previous
                or not math.isfinite(probability) or not 0 <= probability <= 1):
            raise ValueError('Nieprawidłowa ramka VAD')
        self.previous = end_sample
        self.active = probability >= (.35 if self.active else .5)
        if self.active:
            self.speech_end = end_sample
        if (end_sample - self.start >= self.minimum and self.speech_end is not None
                and end_sample - self.speech_end >= 5120):
            return [self._emit(end_sample, 'silence')]
        if end_sample - self.start >= 128000:
            return [self._emit(end_sample, 'max_duration')]
        return []

    def _emit(self, end, reason):
        result = {'start_sample': self.start, 'end_sample': end,
                  'speech_end_sample': self.speech_end if reason=='silence' else None, 'reason': reason}
        self.start = end
        self.speech_end = None
        self.active = False
        return result

    def flush(self, end_sample):
        if end_sample < self.previous:
            raise ValueError('EOF przed ostatnią ramką')
        return [self._emit(end_sample, 'eof')] if end_sample > self.start else []