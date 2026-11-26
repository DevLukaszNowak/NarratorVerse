"""One model and style per process. Stdout carries only worker responses."""
import base64
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys
from time import monotonic



def run(model, style_path, send):
    import numpy as np
    from supertonic import TTS

    engine = TTS(model='supertonic-3', model_dir=Path(model), auto_download=False,
                 intra_op_num_threads=2, inter_op_num_threads=1)
    if engine.sample_rate != 44100 or any(
        getattr(engine.model, key).get_providers() != ['CPUExecutionProvider']
        for key in ('dp_ort', 'text_enc_ort', 'vector_est_ort', 'vocoder_ort')
    ):
        raise RuntimeError('Nieprawidłowy format modelu lub tryb ONNX Supertonic 3.')
    style = engine.get_voice_style_from_path(Path(style_path))

    def synthesize(text):
        np.random.seed(101)
        wav, _ = engine.synthesize(text, style, total_steps=8, speed=1.0, lang='pl')
        return wav[0]

    def valid_pcm(pcm):
        return len(pcm) and np.isfinite(pcm).all() and np.max(np.abs(pcm)) >= 0.001

    synthesize('Sprawdźmy, czy wszystko jest gotowe.')
    send({'ready': True})
    for line in sys.stdin:
        start = monotonic()
        request = json.loads(line)
        pcm = synthesize(request['text'])
        generated = monotonic()
        if not valid_pcm(pcm):
            send({'error': 'Supertonic 3 nie zwrócił poprawnego sygnału audio.', 'invalid_audio': True})
            continue
        raw = np.floor(np.clip(pcm, -1.0, 32767 / 32768) * 32768).astype('<i2')
        encoded = base64.b64encode(raw.tobytes()).decode('ascii')
        send({'pcm': encoded, 'inference_ms': (generated - start) * 1000,
              'serialization_ms': (monotonic() - generated) * 1000,
              'native_speech_rate': 1.0})


if __name__ == '__main__':
    protocol = sys.stdout

    def send(reply):
        print(json.dumps(reply, allow_nan=False), file=protocol, flush=True)

    try:
        with redirect_stdout(sys.stderr):
            run(*sys.argv[1:], send)
    except Exception as error:
        send({'error': f'Błąd Supertonic 3: {error}'})
        sys.exit(1)
