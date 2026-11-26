from __future__ import annotations

import argparse
import asyncio
from contextlib import aclosing
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time
import wave

from .audio_capture import PulseAudioDiscovery, capture_pcm, group_streams


def load_vosk(model_path: Path, language: str):
    if language not in {"en", "pl"}:
        raise ValueError("Wybierz jawnie język en lub pl i zgodny model Vosk")
    if not model_path.is_dir():
        raise FileNotFoundError(f"Brak lokalnego modelu Vosk: {model_path}")
    from vosk import KaldiRecognizer, Model
    recognizer = KaldiRecognizer(Model(str(model_path)), 16000)
    recognizer.SetWords(True)
    return recognizer


class StreamingTranscriber:
    def __init__(self, recognizer, *, minimum_confidence: float = 0.75):
        self.recognizer = recognizer
        self.minimum_confidence = minimum_confidence
        self.samples = 0
        self.partial = ""

    def feed(self, pcm: bytes):
        if not pcm or len(pcm) % 2:
            raise ValueError("PCM musi zawierać pełne próbki s16le 16 kHz mono")
        self.samples += len(pcm) // 2
        started = time.monotonic()
        final = self.recognizer.AcceptWaveform(pcm)
        result = json.loads(self.recognizer.Result() if final else self.recognizer.PartialResult())
        elapsed = (time.monotonic() - started) * 1000
        text = " ".join(str(result.get("text" if final else "partial", "")).split())
        if final:
            self.partial = ""
        elif not text or text == self.partial:
            return None
        else:
            self.partial = text
        if not text:
            return None
        event = {"kind": "final" if final else "partial", "text": text,
                 "audio_position_ms": self.samples / 16, "inference_ms": elapsed}
        if final:
            words = result.get("result", [])
            confidence = sum(float(w.get("conf", 0)) for w in words) / len(words) if words else 0
            event["confidence"] = confidence
            event["endpoint_delay_ms"] = max(0, self.samples / 16 - max(
                (float(w.get("end", 0)) * 1000 for w in words), default=0))
            if confidence < self.minimum_confidence:
                event["kind"] = "rejected"
        return event


async def transcribe_stream(source, transcriber, emit, *, queue_limit=20):
    if queue_limit < 1:
        raise ValueError("Limit kolejki musi być dodatni")
    queue = asyncio.Queue(maxsize=queue_limit)
    finished = object()

    async def produce():
        async with aclosing(source):
            async for pcm in source:
                try:
                    queue.put_nowait((pcm, time.monotonic()))
                except asyncio.QueueFull as error:
                    raise RuntimeError("Przepełniona kolejka ASR; zatrzymano zamiast zwiększać opóźnienie") from error
        await queue.put(finished)

    async def consume():
        while True:
            item = await queue.get()
            if item is finished:
                return
            pcm, captured = item
            wait_ms = (time.monotonic() - captured) * 1000
            if wait_ms > 2000:
                raise RuntimeError("Przeterminowana kolejka ASR; opóźnienie przekroczyło 2 sekundy")
            event = await asyncio.to_thread(transcriber.feed, pcm)
            if event:
                event["queue_wait_ms"] = wait_ms
                emit(event)

    tasks = [asyncio.create_task(produce()), asyncio.create_task(consume())]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def print_event(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


async def run(args):
    discovery = PulseAudioDiscovery()
    applications = None
    if args.list or args.stream_index is not None:
        applications = group_streams(await asyncio.to_thread(discovery.list_streams))
    if args.list:
        for application in applications:
            print_event({"kind": "application", **asdict(application)})
        return
    if args.model is None or args.language is None:
        raise ValueError("Podaj --model i --language; modele nie są pobierane automatycznie")
    print_event({"kind": "state", "engine": "loading", "language": args.language})
    started = time.monotonic()
    recognizer = await asyncio.to_thread(load_vosk, args.model, args.language)
    transcriber = StreamingTranscriber(recognizer)
    print_event({"kind": "state", "engine": "ready", "model_load_ms": (time.monotonic() - started) * 1000})
    if args.wav:
        with wave.open(str(args.wav), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError("WAV musi zawierać PCM s16le 16 kHz mono")
            while pcm := audio.readframes(1600):
                event = transcriber.feed(pcm)
                if event: print_event(event)
            for _ in range(20):
                event = transcriber.feed(bytes(3200))
                if event: print_event(event)
        return
    matches = [stream for app in applications for stream in app.streams
               if stream.stream_index == args.stream_index]
    if len(matches) != 1:
        raise ValueError("Wskazany indeks nie występuje w świeżej liście aplikacji audio")
    print_event({"kind": "state", "capture": "starting", "stream": asdict(matches[0])})
    await transcribe_stream(capture_pcm(discovery, matches[0].stream_id), transcriber, print_event)


def main():
    parser = argparse.ArgumentParser(description="Eksperymentalny ASR wybranej aplikacji, bez OCR i TTS")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--list", action="store_true")
    source.add_argument("--stream-index", type=int)
    source.add_argument("--wav", type=Path)
    parser.add_argument("--language", choices=("en", "pl"))
    parser.add_argument("--model", type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print_event({"kind": "state", "capture": "stopped"})
    except Exception as error:
        print_event({"kind": "error", "message": str(error)})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())