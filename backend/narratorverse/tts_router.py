"""Resolve a saved voice selection for any speech mode."""
from collections.abc import Mapping

from game_optimization_linux.services.narrator_tts import PIPER_DEFAULT_VOICE_ID
from .chatterbox_tts import CHATTERBOX_ID
from .supertonic_tts import SUPERTONIC_ID


VOICE_KEYS = {CHATTERBOX_ID: 'chatterbox_voice_id', SUPERTONIC_ID: 'supertonic_voice_id'}


class OcrTtsProvider:
    def __init__(self, piper, chatterbox, supertonic):
        self.piper = piper
        self.providers = {p.provider_id: p for p in (piper, chatterbox, supertonic)}
        self.active = piper

    def provider_for(self, provider_id):
        try:
            return self.providers[provider_id or self.piper.provider_id]
        except KeyError as error:
            raise ValueError("Nieznany silnik mowy") from error

    def select(self, provider_id):
        selected = self.provider_for(provider_id)
        if selected is not self.active:
            self.active.cancel()
        self.active = selected

    def speech_selection(self, options):
        getter = options.get if isinstance(options, Mapping) else lambda key, default=None: getattr(options, key, default)
        provider = self.provider_for(getter('tts_provider_id'))
        key = VOICE_KEYS.get(provider.provider_id, 'voice_id')
        return provider, getter(key) or provider.default_voice_id

    @staticmethod
    def require_voice(provider, voice_id):
        if provider.provider_id in VOICE_KEYS:
            provider.require_component(voice_id)
        elif voice_id not in provider.available_voice_ids:
            raise RuntimeError('Brak wybranego głosu lub środowiska Piper. Sprawdź Komponenty albo wybierz dostępny głos.')

    def require_component(self, voice_id):
        if self.active is not self.piper:
            self.active.require_component(voice_id)

    def __getattr__(self, key):
        return getattr(self.active, key)


def resolve_speech(tts, options):
    if isinstance(tts, OcrTtsProvider):
        return tts.speech_selection(options)
    return tts, options.get('voice_id') or PIPER_DEFAULT_VOICE_ID


def synthesis_options(provider, options, voice_id, epoch):
    arguments = {'language': 'pl', 'voice_id': voice_id, 'speech_rate': options['speech_rate']}
    if provider.provider_id not in VOICE_KEYS:
        for key in ('noise_scale', 'noise_w_scale'):
            if options.get(key) is not None:
                arguments[key] = options[key]
    if epoch is not None:
        arguments['expected_epoch'] = epoch
    return arguments


def provider_epoch(provider):
    if getattr(provider, 'supports_request_epochs', False) is True:
        return provider.request_epoch()
    return None
