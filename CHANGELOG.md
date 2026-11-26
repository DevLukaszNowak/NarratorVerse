# Historia wydań

## 0.3.0-beta

Zmiany od publicznego `0.2.0-beta.1`:

- **Lektor z dźwięku gry (ASR, wersja alpha).** Faster Whisper rozpoznaje angielską mowę z wybranego strumienia gry. Aplikacja pokazuje tekst i polskie tłumaczenie, które może przeczytać. Do wyboru są trzy sposoby dzielenia mowy: wcześniejszy podział, szybsza reakcja i dłuższy kontekst.
- **Dwa nowe silniki mowy.** Supertonic 3 działa na CPU i ma głos M5-02 oraz import stylów. Eksperymentalny Chatterbox Multilingual V3 działa na Vulkanie, oferuje głos wbudowany, głos 02 i import głosu z WAV. Piper z Voidkiem pozostaje domyślny. Napisy, dźwięk gry i szybki odczyt mają osobne ustawienia silnika i głosu oraz odsłuch.
- **Przyciszanie gry.** Poprawiono dostęp miksera do strumienia gry w Flatpaku, wybór aktywnego strumienia i przywracanie jego poziomu po zakończeniu lub zatrzymaniu lektora. Zmiana dotyczy tylko wybranego strumienia.
- **Tempo dialogów.** OCR i ASR dostosowują tempo kolejnych kwestii do obciążenia kolejki. Można ustawić maksymalne dodatkowe przyspieszenie i zobaczyć tempo ostatniej odczytanej kwestii. Przyspieszenie nie zmienia wysokości głosu.
- **Napisy i odtwarzanie.** Poprawiono obsługę dopisków i etykiet rozmówców. Naprawiono przedwczesne kończenie odtwarzania, gdy w buforze nadal były próbki. Supertonic generuje mowę w tempie 1×, a zmiana tempa następuje po syntezie.
- **Tłumaczenie.** W zwykłym trybie Argos można wybrać ustawienie Zrównoważone lub Szybkie. QuickMT pozostaje opcją eksperymentalną.
- **Interfejs i pakiet.** Nowa ikona aplikacji. Usunięto z widoku panele diagnostyczne OCR i ASR, zachowując logowanie i komunikaty o błędach. Pełny Flatpak nie wymaga wcześniejszej instalacji NarratorVerse. Modele Supertonic oraz składniki ASR pobiera się w Komponentach.

Znane ograniczenia:

- Lektor z dźwięku gry jest w wersji alpha. Ma duże opóźnienie, a muzyka i efekty gry obniżają trafność rozpoznawania ([#3](https://github.com/DevLukaszNowak/NarratorVerse/issues/3)).
- Lektor może czasami dwa razy przeczytać ten sam dialog ([#4](https://github.com/DevLukaszNowak/NarratorVerse/issues/4)).
- Szybka seria kwestii może przepełnić kolejkę.
- Brzmienie głosów wymaga dalszej pracy. Chatterbox potrzebuje sprzętowego Vulkan i pozostaje eksperymentalny.

Przyciszanie i adaptacyjne tempo sprawdzono w GTA V na Fedorze z Piperem i Supertonic 3 CPU.

## 0.2.0-beta.1

Eksperymentalne tłumaczenie QuickMT, poprawka filtrowania powtarzających się napisów i pakiet Flatpak.

## 0.1.0

Pierwsze publiczne wydanie lektora napisów z interfejsem po polsku.
