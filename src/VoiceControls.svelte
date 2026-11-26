<script lang="ts">
 import {adaptiveLabels} from './adaptive';
 import Icon from './Icon.svelte';
 import type {Settings,SpeechEngine} from './ipc';
 import {voiceSelection} from './voiceSettings';

 export let engines:SpeechEngine[]=[];
 export let values:Settings={};
 export let locked=false;
 export let previewing=false;
 export let previewBlocked=false;
 export let canImport=true;
 export let showAdaptive=false;
 export let effectiveRate:number|undefined=undefined;
 export let rateLimit=2;
 const formatRate=(value:number)=>value.toLocaleString('pl-PL',{minimumFractionDigits:2,maximumFractionDigits:2})+'×';
 $: baseRate=Number(values.speech_rate??1);
 $: maxBoost=Number(values.adaptive_max_boost??.5);
 $: maxRate=Math.min(rateLimit,baseRate*(1+maxBoost));
 export let change:(key:string,value:unknown,commit:boolean)=>void;
 export let preview:()=>void;
 export let stopPreview:()=>void;
 export let importVoice:(engine:string)=>void;
 export let openComponent:(component:string)=>void;

 $: ({engine,voice,ready}=voiceSelection(engines,values));
 $: importLabel=engine?.id==='chatterbox-mtl-v3-vulkan'?(voice!=='builtin'?'Importuj referencję':''):'Importuj głos';
</script>

{#if engine}
 <div class="form-grid voice-controls">
  <label>Silnik mowy<select value={engine.id} disabled={locked} on:change={e=>change('tts_provider_id',e.currentTarget.value,true)}>
   {#each engines as item (item.id)}<option value={item.id}>{item.name}</option>{/each}
  </select><span class="hint">{engine.description}</span></label>
  <label>Głos<select value={voice} disabled={locked} on:change={e=>change(engine.voice_key,e.currentTarget.value,true)}>
   {#if !engine.voices.some(item=>item.voice_id===voice)}<option value={voice}>Zapisany głos niedostępny</option>{/if}
   {#each engine.voices as item (item.voice_id)}<option value={item.voice_id}>{item.name}{item.installed?'':' (niedostępny)'}</option>{/each}
  </select></label>
  <label>{showAdaptive?'Tempo bazowe':'Tempo'} <output>{formatRate(baseRate)}</output>
   <input type="range" min=".5" max="2" step=".05" value={values.speech_rate??1} disabled={locked}
    on:input={e=>change('speech_rate',e.currentTarget.valueAsNumber,false)} on:change={e=>change('speech_rate',e.currentTarget.valueAsNumber,true)}/>
  </label>
  <label>Głośność <output>{Math.round((values.volume??.85)*100)}%</output>
   <input type="range" min="0" max="1" step=".05" value={values.volume??.85} disabled={locked}
    on:input={e=>change('volume',e.currentTarget.valueAsNumber,false)} on:change={e=>change('volume',e.currentTarget.valueAsNumber,true)}/>
  </label>
 </div>
 {#if showAdaptive}
  <label class="check"><input type="checkbox" checked={values.adaptive_speech_rate}
   disabled={locked} on:change={e=>change('adaptive_speech_rate',e.currentTarget.checked,true)}/>{adaptiveLabels.title}</label>
  {#if values.adaptive_speech_rate}
   <label>Dodatkowe przyspieszenie <output>+{Math.round(maxBoost*100)}%</output>
    <input type="range" min="0" max="100" step="5" value={Math.round(maxBoost*100)} disabled={locked}
     on:input={e=>change('adaptive_max_boost',e.currentTarget.valueAsNumber/100,false)}
     on:change={e=>change('adaptive_max_boost',e.currentTarget.valueAsNumber/100,true)}/>
   </label>
   <p class="hint">{adaptiveLabels.hint} Maksymalne tempo {formatRate(maxRate)}.{#if baseRate*(1+maxBoost)>rateLimit} Limit silnika wynosi {formatRate(rateLimit)}.{/if}</p>
  {/if}
  {#if effectiveRate!=null}
   <p>Tempo ostatniej kwestii <output>{formatRate(effectiveRate)}</output></p>
  {/if}
 {/if}
 <p class="hint" role="status">{ready?'Głos gotowy':engine.ready?'Wybrany głos jest niedostępny':engine.message||'Brakuje składników silnika'}</p>
 <div class="button-row">
  {#if !ready&&engine.component}<button on:click={()=>openComponent(engine.component||'')}><Icon name="components"/>Otwórz komponent</button>{/if}
  {#if canImport&&importLabel}<button disabled={locked} on:click={()=>importVoice(engine.id)}><Icon name="upload"/>{importLabel}</button>{/if}
  <button disabled={!ready||locked||previewBlocked} on:click={preview}><Icon name="play"/>Posłuchaj głosu</button>
  {#if previewing}<button on:click={stopPreview}><Icon name="stop"/>Zatrzymaj odsłuch</button>{/if}
 </div>
{/if}

<style>
 .voice-controls{grid-template-columns:repeat(auto-fit,minmax(min(100%,170px),1fr))}
 .voice-controls output{font-variant-numeric:tabular-nums}
</style>
