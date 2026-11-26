export const audioBalanceLabels:Record<string,string> = {
 title:'Balans lektora i gry',enabled:'Przyciszaj grę podczas mowy lektora',
 hint:'Wybór okna lub monitora w portalu udostępnia obraz. Wybierz aplikację audio gry osobno.',
 application:'Aplikacja z dźwiękiem gry',choose:'Wybierz aplikację',refresh:'Odśwież aplikacje audio',
 notLoaded:'Uruchom dźwięk w grze i odśwież listę.',
 loading:'Pobieranie aplikacji audio…',loadError:'Nie udało się pobrać listy aplikacji audio.',
 missing:'Brak bezpiecznie rozpoznanych aplikacji. Uruchom dźwięk w grze i odśwież listę.',
 saved:'Zapisana aplikacja (niewidoczna na liście)',ambiguous:'Kilka aktywnych strumieni',
 unidentified:'Brak pewnej tożsamości',idle_stream:'Dźwięk wstrzymany',
 selectionRequired:'Przyciszanie czeka na wybór aplikacji audio. Wybierz grającą grę i zapisz profil.',
 selectedMissing:'Zapisanej aplikacji nie ma wśród strumieni audio. Uruchom jej dźwięk i odśwież listę.',
 selectedAmbiguous:'Wybrana gra ma kilka aktywnych strumieni. Przyciszanie wymaga jednego pasującego strumienia.',
 selectedUnidentified:'System audio nie pozwala potwierdzić tożsamości strumienia wybranej gry.',
 selectedIdle:'Dźwięk wybranej aplikacji jest wstrzymany. Wznów go w grze.',
 depth:'Siła przyciszania',gentle:'Łagodna (−6 dB)',balanced:'Średnia (−12 dB)',strong:'Mocna (−18 dB)',
 normalize:'Wyrównuj poziom lektora',
 normalizeHint:'Ograniczone wyrównanie głośności mowy z zapasem na szczyty. Nie zmienia Szybkiego odczytu.',
 baseline:'Nie zmienia głośności głównej ani nie oddziela dialogów gry od muzyki. Ręczna regulacja w mikserze oddaje Ci sterowanie do ponownego włączenia czytania.',
 off:'Balans nieaktywny',ready:'Gra wybrana. Czekam na mowę lektora.',ducking:'Przyciszanie gry',restoring:'Przywracanie głośności',
 manual:'Głośność zmieniona ręcznie',error:'Balans niedostępny',
 changed:'Strumień gry się zmienił. Wznów czytanie.',
};

export type AudioTarget={target:string;label:string;ambiguous:boolean;state:string};

export function audioSelectionMessage(enabled:boolean,target:string,targets:AudioTarget[],loaded:boolean):string {
 if(!enabled)return '';
 if(!target)return audioBalanceLabels.selectionRequired;
 if(!loaded)return '';
 const selected=targets.find(item=>item.target===target);
 if(!selected)return audioBalanceLabels.selectedMissing;
 if(selected.state==='unidentified')return audioBalanceLabels.selectedUnidentified;
 if(selected.ambiguous)return audioBalanceLabels.selectedAmbiguous;
 if(selected.state==='idle_stream')return audioBalanceLabels.selectedIdle;
 return '';
}
