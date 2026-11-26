import type {Settings,SpeechEngine} from './ipc';

export const defaultVoice:Settings={tts_provider_id:'',voice_id:'voidek-pl-e991',chatterbox_voice_id:'builtin',supertonic_voice_id:'M5-02',speech_rate:1,volume:.85};
export const translationHint='Zrównoważony rozważa do czterech wersji w każdym kroku. Szybki wybiera jedną.';

export function voiceSelection(engines:SpeechEngine[], values:Settings){
 const engine=engines.find(item=>item.id===values.tts_provider_id)||engines[0];
 const voice=engine&&(values[engine.voice_key]||engine.default_voice);
 return {engine,voice,ready:!!engine?.ready&&!!engine.voices.find(item=>item.voice_id===voice)?.installed};
}
