# Diagnostyka

## Kontrole źródeł

`npm run check` i `npm run build` sprawdzają frontend. Testy backendu nie są częścią publicznego repozytorium. Żadna z tych kontroli nie zastępuje próby przechwytywania i odtwarzania w aplikacji.

## Ślad zdarzeń

`NARRATORVERSE_EXPERIMENT_TRACE` przyjmuje absolutną ścieżkę do nowego pliku JSONL. Aplikacja nie nadpisuje istniejącego pliku. Ślad zawiera pełne teksty napisów i tłumaczeń, więc przejrzyj go przed udostępnieniem.

W Flatpaku plik musi leżeć w katalogu dostępnym dla aplikacji:

```sh
flatpak run --env=NARRATORVERSE_EXPERIMENT_TRACE="$HOME/.var/app/io.github.DevVoidPL.NarratorVerse/data/trace.jsonl" io.github.DevVoidPL.NarratorVerse//beta
```

Ślad obejmuje akceptację napisów, kolejki, tłumaczenie, syntezę i odtwarzanie. Zapis odbywa się w osobnym wątku i nie spowalnia lektora. Gdy kolejka zapisu się zapełni, nadmiarowe zdarzenia są pomijane, a ich liczbę podaje ostatni wpis `trace_closed`.

Dodatkowe przełączniki działają tylko razem ze śladem:

- `NARRATORVERSE_OCR_WAV=1` zapisuje obok pliku JSONL nagrania WAV mowy odtworzonej w trybie napisów.
- `NARRATORVERSE_ASR_DIAGNOSTICS=1` dodaje zdarzenia z rozpoznawania mowy w lektorze z dźwięku gry.

## Runtime tłumaczenia QuickMT

`python3 -m narratorverse.quickmt_runtime` sprawdza natywne zależności eksperymentalnego QuickMT, a `narratorverse.quickmt_smoke` uruchamia tłumaczenie na wskazanym modelu. Oba narzędzia wymagają przygotowanego środowiska Python. Pomiar samego tłumaczenia nie obejmuje OCR, przechwytywania ani opóźnienia wyjścia audio.
