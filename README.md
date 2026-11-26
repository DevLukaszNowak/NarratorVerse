# NarratorVerse

Polski lektor do gier na Linuksie. Czyta napisy z okna lub ekranu, a w grach bez napisów może rozpoznawać mowę z dźwięku. Angielski tekst tłumaczy na polski. Wszystko odbywa się lokalnie, bez modyfikowania gry.

## Instalacja

Na stronie [wydań](https://github.com/DevLukaszNowak/NarratorVerse/releases) pobierz pełny plik `.flatpak` dla Linuksa x86_64. Do instalacji nie potrzeba poprzedniej wersji NarratorVerse. Flatpak korzysta ze środowiska GNOME Platform 49 dostępnego na Flathubie.

```sh
flatpak install --user ./NarratorVerse-0.3.0-beta-x86_64.flatpak
flatpak run io.github.DevVoidPL.NarratorVerse//beta
```

Do odczytu napisów wystarczą składniki w paczce. Modele Supertonic 3 i składniki rozpoznawania mowy pobierzesz w zakładce **Komponenty**. Internet jest potrzebny przy pobieraniu, nie przy czytaniu ani tłumaczeniu.

## Co potrafi

- **Lektor** czyta polskie napisy albo tłumaczy angielskie. Do wyboru są Tesseract i RapidOCR. Profile pamiętają m.in. obszar napisów, ustawienia głosu i wybraną aplikację audio.
- **Lektor z dźwięku gry** (wersja alpha) rozpoznaje angielską mowę za pomocą Faster Whisper na CPU. Pokazuje tekst i polskie tłumaczenie. Czytanie tłumaczenia można wyłączyć.
- **Szybki odczyt** czyta wklejony polski tekst, do 10 000 znaków. Można go używać bez gry oraz bez przechwytywania obrazu i dźwięku.
- **Głosy**: Piper z głosem Voidek i importem modeli `.onnx` z pasującym `.onnx.json`, Supertonic 3 CPU z głosem M5-02 i importem stylów, eksperymentalny Chatterbox Multilingual V3 z głosem wbudowanym i głosem 02. Każdy tryb ma osobne ustawienia głosu, głośności i tempa oraz pozwala na odsłuch.
- **Przyciszanie** obejmuje wybrany strumień gry i przywraca jego poprzednią głośność po odczycie. Możesz wybrać −6, −12 lub −18 dB. Głośność systemu pozostaje bez zmian.
- **Adaptacyjne tempo** działa dla napisów i dla lektora z dźwięku. Gdy kwestie czekają na odczyt, lektor czyta kolejne szybciej i wraca do tempa bazowego, gdy kolejka się rozładowuje. Ustawiasz maksymalne dodatkowe przyspieszenie, np. +30% lub +60%, w granicach tempa obsługiwanego przez silnik.

## Pierwsza sesja

**Napisy.** Wybierz profil i język napisów, zapisz zmiany i kliknij **Wybierz źródło**. Wskaż okno gry lub ekran w oknie systemowym. W podglądzie zaznacz tylko miejsce, w którym pojawiają się napisy, i rozpocznij czytanie. Pauza zostawia udostępnianie włączone. Zatrzymanie zamyka przechwytywanie. Po ponownym uruchomieniu system może jeszcze raz poprosić o zgodę.

**Dźwięk.** Zainstaluj Faster Whisper i Whisper small w **Komponentach**. Włącz dźwięk gry, odśwież listę w **Lektorze z dźwięku gry** i wybierz aplikację oraz jej strumień. NarratorVerse nie przełącza się na przechwytywanie całego dźwięku systemu.

**Przyciszanie.** W zakładce **Lektor** włącz przyciszanie, wskaż aplikację grającą dźwięk i zapisz profil. Wybranie okna do odczytu napisów nie wybiera automatycznie aplikacji audio. Przyciszanie wymaga działającego PipeWire z interfejsem PulseAudio i dostępu do strumienia gry. Jeśli gra zmieni strumień lub ręcznie zmienisz jej głośność, może być potrzebne wznowienie sesji.

## Ograniczenia bety

- Lektor może czasami dwa razy przeczytać ten sam dialog, zwłaszcza gdy napis na chwilę zniknie z rozpoznanego obrazu ([#4](https://github.com/DevLukaszNowak/NarratorVerse/issues/4)). Przy bardzo szybkich dialogach część kwestii może wypaść z kolejki.
- Lektor z dźwięku gry jest w wersji alpha. Od końca wypowiedzi do odczytu mija kilka sekund, a muzyka i efekty gry czasem zaburzają rozpoznawanie ([#3](https://github.com/DevLukaszNowak/NarratorVerse/issues/3)). Program nie rozdziela głosów poszczególnych postaci.
- Brzmienie zależy od silnika. Chatterbox wymaga zgodnego sterownika i sprzętowego Vulkan; nadal jest eksperymentalny. Przyciszanie i adaptacyjne tempo zostały sprawdzone w GTA V na Fedorze z Piperem i Supertonic 3 CPU.

Zmiany względem poprzedniej wersji publicznej znajdziesz w [changelogu](CHANGELOG.md).

## Kod i budowa

Interfejs korzysta z Svelte i TypeScript, aplikacja okienkowa z Tauri 2, a przechwytywanie i przetwarzanie obsługuje osobny proces Python/Qt. Repozytorium nie zawiera modeli, środowiska wykonawczego ani testów. Skrypty pakowania wymagają przygotowanych katalogów `.runtime` i `components`, a pełna paczka również bibliotek i modeli Chatterbox oraz runtime Supertonic. Samo sklonowanie kodu nie wystarczy do odtworzenia gotowego Flatpaka.

```sh
npm ci --no-audit --no-fund
npm run check
npm run build
```

`sh scripts/build.sh` buduje interfejs i binarkę Tauri w przygotowanym GNOME SDK 49. Pliki pakietu zestawia `scripts/stage-flatpak.py`. Zmienne do zapisu śladu zdarzeń opisuje [diagnostyka](docs/DIAGNOSTICS.md).

## Licencje

Kod NarratorVerse jest na licencji MIT. Zależności, modele i głosy mają osobne warunki. Voidek jest dołączony w niezmienionej postaci za zgodą właściciela. Informacje o pochodzeniu znajdują się w katalogu [licencji](licenses/), a otwarte kwestie redystrybucji w pliku [REDISTRIBUTION](docs/REDISTRIBUTION.md). Prywatne profile, logi i głosy importowane przez użytkownika nie są częścią pakietu.
