Wydanie testowe NarratorVerse 0.3.0. Paczka Flatpak ma wersję 0.3.0-beta i działa na Linuksie x86_64.

## Instalacja

Pobierz `NarratorVerse-0.3.0-beta-x86_64.flatpak` i plik `.sha256` z tego wydania, a potem:

```sh
sha256sum -c NarratorVerse-0.3.0-beta-x86_64.flatpak.sha256
flatpak install --user ./NarratorVerse-0.3.0-beta-x86_64.flatpak
flatpak run io.github.DevVoidPL.NarratorVerse//beta
```

SHA-256: `72125bd72547cf8286fd833f08faba49749352c293c4b14bd6ef2aee3a2d9f80`

Paczka nie wymaga wcześniejszej wersji NarratorVerse. Środowisko GNOME Platform 49 Flatpak pobiera z Flathuba. Do czytania napisów wystarczą składniki w paczce. Modele Supertonic 3 i składniki rozpoznawania mowy pobierzesz w zakładce Komponenty.

## Zmiany

- Lektor z dźwięku gry (ASR, wersja alpha) rozpoznaje angielską mowę z wybranego strumienia gry przez Faster Whisper. Pokazuje tekst i polskie tłumaczenie, które może przeczytać.
- Nowe silniki mowy: Supertonic 3 na CPU z głosem M5-02 i importem stylów oraz eksperymentalny Chatterbox Multilingual V3 na Vulkanie. Piper z głosem Voidek pozostaje domyślny. Napisy, dźwięk gry i szybki odczyt mają osobne ustawienia głosu.
- Przyciszanie obejmuje tylko wybrany strumień gry i przywraca jego głośność po odczycie albo zatrzymaniu lektora. Poprawiono dostęp do miksera w Flatpaku.
- Gdy kwestie czekają w kolejce, lektor czyta szybciej, bez zmiany wysokości głosu. Maksymalne przyspieszenie ustawiasz sam.
- Poprawiono obsługę dopisków i etykiet rozmówców. Odtwarzanie nie kończy się już przed odczytaniem całej kwestii.
- Argos ma ustawienia Zrównoważone i Szybkie. QuickMT pozostaje opcją eksperymentalną.
- Nowa ikona aplikacji.

## Znane problemy

- Lektor z dźwięku gry jest w wersji alpha. Od końca wypowiedzi do odczytu mija kilka sekund, a muzyka i efekty gry obniżają trafność rozpoznawania. Funkcja wymaga dalszej pracy ([#3](https://github.com/DevLukaszNowak/NarratorVerse/issues/3)).
- Lektor może czasami dwa razy przeczytać ten sam dialog. Problem zostanie naprawiony w jednej z kolejnych wersji ([#4](https://github.com/DevLukaszNowak/NarratorVerse/issues/4)).
- Szybka seria kwestii może przepełnić kolejkę.
- Chatterbox wymaga sprzętowego Vulkan i pozostaje eksperymentalny.

Przyciszanie i adaptacyjne tempo sprawdzono w GTA V na Fedorze z Piperem i Supertonic 3 CPU.

Obecnie rozwijam GameOpti, więc kolejne poprawki NarratorVerse pojawią się później.

Pełna lista zmian jest w [CHANGELOG.md](https://github.com/DevLukaszNowak/NarratorVerse/blob/v0.3.0/CHANGELOG.md).
