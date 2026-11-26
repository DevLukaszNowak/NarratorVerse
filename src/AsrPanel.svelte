<script lang="ts">
 import {rpc,type Component,type Settings,type SpeechEngine} from './ipc';
 import {asrStates} from './asrPanel';
 import VoiceControls from './VoiceControls.svelte';
 import Icon from './Icon.svelte';
 import {defaultVoice,translationHint,voiceSelection} from './voiceSettings';

 export let state:any={state:'idle',rows:[]};
 export let options:Settings={};
 export let engines:SpeechEngine[]=[];
 export let components:Component[]=[];
 export let requiredComponents:string[]=[];
 export let blocked=false;
 export let previewing=false;
 export let balanceMessage='';
 export let openComponent:(id:string)=>void;
 export let importVoice:(id:string)=>void;
 export let preview:(values:Settings)=>void;
 export let stopPreview:()=>void;

 let applications:any[]=[],appId='',streamId='';
 let loading=false,pending=false,error='';
 let form:Settings={...defaultVoice};
 $: form={...defaultVoice,...options};
 $: application=applications.find(app=>app.group_id===appId);
 $: stream=application?.streams.find((item:any)=>item.stream_id===streamId);
 $: busy=!['idle','stopped','error'].includes(state.state);
 $: tts=busy?state.tts_enabled:form.tts_enabled??true;
 $: missing=requiredComponents.filter(id=>!components.some(item=>item.component_id===id&&item.installed));
 $: voice=voiceSelection(engines,form);

 async function refresh(){
  loading=true;error='';
  try{applications=await rpc('asr_sources');appId='';streamId='';}
  catch(e){error=String(e);}
  finally{loading=false;}
 }
 async function command(method:string,params:Record<string,unknown>={}){
  pending=true;error='';
  try{state=await rpc(method,params);}
  catch(e){error=String(e);}
  finally{pending=false;}
 }
 async function save(){
  pending=true;error='';
  try{form=await rpc('asr_settings',{values:form});}
  catch(e){form={...defaultVoice,...options};error=String(e);}
  finally{pending=false;}
 }
 function change(key:string,value:unknown,commit:boolean){form={...form,[key]:value};if(commit)void save();}
 function toggle(enabled:boolean){
  if(busy)void command('asr_tts',{enabled});
  else change('tts_enabled',enabled,true);
 }
</script>

<section class="panel">
 <div class="section-heading"><h2>Źródło dźwięku</h2><span class="badge" role="status">{asrStates[state.state]||state.state}</span></div>
 {#if blocked&&!previewing}<p class="hint">Przed rozpoczęciem zakończ inne czytanie lub instalację komponentu.</p>{/if}
 {#if error||state.error}<p class="alert error" role="alert">{error||state.error}</p>{/if}
 {#each missing as id}
  <div class="button-row"><p class="hint">Brakuje komponentu {components.find(item=>item.component_id===id)?.name||'rozpoznawania mowy'}.</p><button on:click={()=>openComponent(id)}><Icon name="components"/>Otwórz komponent</button></div>
 {/each}
 <div class="form-grid">
  <label>Aplikacja<select bind:value={appId} disabled={busy||pending} on:change={()=>streamId=''}><option value="">Wybierz grę</option>{#each applications as app}<option value={app.group_id}>{app.label}</option>{/each}</select></label>
  <label>Strumień audio<select bind:value={streamId} disabled={busy||pending||!application}><option value="">Wybierz dźwięk gry</option>{#each application?.streams||[] as item}<option value={item.stream_id}>{item.label} ({item.stream_index})</option>{/each}</select></label>
  <button disabled={loading||busy} on:click={refresh}><Icon name="refresh"/>{loading?'Odczytywanie…':'Odśwież listę gier'}</button>
  <label>Podział mowy<select value={form.preset||'quick'} disabled={busy||pending} on:change={e=>change('preset',e.currentTarget.value,true)}>
   <option value="early">Wcześniejszy podział</option><option value="quick">Szybsza reakcja</option><option value="context">Dłuższy kontekst</option>
  </select></label>
  <label>Tłumaczenie<select value={form.translation_profile_id||'balanced'} disabled={busy||pending} on:change={e=>change('translation_profile_id',e.currentTarget.value,true)}><option value="balanced">Zrównoważony</option><option value="fast">Szybki</option></select></label>
 </div>
 <p class="hint">{translationHint}</p>
 <div class="transport"><button class="primary" disabled={busy||blocked||pending||!stream||missing.length>0||(tts&&!voice.ready)} on:click={()=>void command('asr_start',{selection:stream,preset:form.preset||'quick',tts_enabled:tts})}><Icon name="play"/>Rozpocznij</button><button disabled={!busy||state.state==='stopping'} on:click={()=>void command('asr_stop')}><Icon name="stop"/>Zatrzymaj</button></div>
</section>
<section class="panel">
 <h2>Głos i czytanie</h2>
 <label class="check"><input type="checkbox" checked={tts} disabled={pending||state.state==='stopping'} on:change={e=>toggle(e.currentTarget.checked)}/>Czytaj tłumaczenie</label>
 <VoiceControls {engines} values={form} showAdaptive effectiveRate={busy&&tts?state.effective_speech_rate:undefined} locked={busy||blocked||pending} {previewing} change={change}
   preview={()=>preview(form)} {stopPreview} {importVoice} {openComponent}/>
 {#if balanceMessage}<p class="hint" role="status">{balanceMessage}</p>{/if}
</section>
{#each state.rows||[] as row (row.id)}
 <section class="panel">
  <div class="section-heading"><h2>Fragment {row.id}</h2><span class="badge">{asrStates[row.state]||row.state}</span></div>
  <h3>Rozpoznany tekst</h3><p class="transcript">{row.text||'Brak tekstu'}</p>
  <h3>Tłumaczenie polskie</h3><p class="transcript">{row.translation||'Oczekiwanie na tłumaczenie'}</p>
 </section>
{/each}
<style>
 .transcript{white-space:pre-wrap;overflow-wrap:anywhere}
 h3{font-size:.95rem;margin:18px 0 8px}
</style>
