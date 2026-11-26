export const asrStates:Record<string,string>={idle:'Nie uruchomiono',loading:'Ładowanie modelu',
 ready:'Model gotowy, uruchamianie audio',listening:'Rozpoznawanie',stopping:'Zatrzymywanie',
 stopped:'Zatrzymano',error:'Błąd',queued:'W kolejce',translating:'Tłumaczenie',
 waiting_audio:'Oczekiwanie na odtwarzanie',playing:'Odczyt',played:'Odczytano',
 translated:'Przetłumaczono',empty:'Brak tekstu w tym fragmencie',tts_disabled:'Lektor wyłączony'};
export function duration(value:number|null|undefined):string{
 if(value==null||!Number.isFinite(value))return '—';
 return value>=1000?(value/1000).toFixed(2)+' s':Math.round(value)+' ms';
}