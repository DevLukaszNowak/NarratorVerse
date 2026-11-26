"""QuickMT tokenization over the existing CPU/JSONL translation boundary.

Keep beam size, decoding defaults and thread policy identical to Argos.
Only the model and its required tokenization differ in the initial A/B.
"""
from pathlib import Path

from game_optimization_linux.services.narrator_translation_worker import (
    _TranslationEngine, main as serve,
)


class QuickMTTokenizer:
    def __init__(self, model_file):
        from sentencepiece import SentencePieceProcessor
        source = Path(model_file)
        self.source = SentencePieceProcessor(model_file=str(source))
        self.target = SentencePieceProcessor(model_file=str(source.with_name('tgt.spm.model')))

    def encode(self, text, *, out_type):
        if out_type is not str:
            raise TypeError('QuickMT requires string tokens')
        # Matches upstream QuickMT Translator.tokenize, including source EOS.
        return self.source.encode(text, out_type=str) + ['</s>']

    def decode(self, tokens):
        return self.target.decode(tokens)


class QuickMTEngine(_TranslationEngine):
    def __init__(self, model_dir, tokenizer_path, target_prefix='', *,
                 translator_factory=None, tokenizer_factory=None):
        super().__init__(model_dir, tokenizer_path, target_prefix,
                         translator_factory=translator_factory,
                         tokenizer_factory=tokenizer_factory or QuickMTTokenizer)

    def translate(self, text, *, beam_size):
        """Keep the earlier QuickMT experiment unchanged by Argos-specific SBD.

        QuickMT has its own source EOS contract. It was not evaluated with the
        Argos unit bounds and must not silently inherit new decoding semantics.
        """
        tokens = self._tokenizer.encode(text, out_type=str)
        if not tokens:
            raise RuntimeError('The translation tokenizer returned no tokens')
        options = dict(beam_size=beam_size, num_hypotheses=1,
                       length_penalty=0.2, replace_unknowns=True)
        if self._target_prefix:
            options['target_prefix'] = [[self._target_prefix]]
        results = self._translator.translate_batch([tokens], **options)
        if not results or not results[0].hypotheses:
            raise RuntimeError('The translation model returned no hypothesis')
        translated = str(self._tokenizer.decode(results[0].hypotheses[0])).strip()
        if self._target_prefix and translated.startswith(self._target_prefix):
            translated = translated[len(self._target_prefix):].lstrip()
        if not translated:
            raise RuntimeError('The translation model returned empty text')
        return translated


def main(argv=None, *, engine_factory=QuickMTEngine):
    return serve(argv, engine_factory=engine_factory)


if __name__ == '__main__':
    raise SystemExit(main())