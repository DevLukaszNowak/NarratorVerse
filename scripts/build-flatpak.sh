#!/bin/sh
# Local unsigned verification bundle, not a distribution-ready release.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BUILD="$ROOT/flatpak/build"
REPO="$ROOT/flatpak/repo"
OUT="$ROOT/flatpak/artifacts"
QUICKMT=no
case "$#:$*" in
  0:) ;;
  1:--quickmt) QUICKMT=yes ;;
  *) printf 'Użycie: sh %s [--quickmt]\n' "$0" >&2; exit 2 ;;
esac
if [ -e "$BUILD" ]; then
  printf '%s\n' "Refusing to replace existing build directory: $BUILD" >&2
  exit 1
fi
mkdir -p "$OUT"
flatpak build-init "$BUILD" io.github.DevVoidPL.NarratorVerse org.gnome.Sdk org.gnome.Platform 49
if [ "$QUICKMT" = yes ]; then
  python3 "$ROOT/scripts/stage-flatpak.py" --dest "$BUILD/files" --quickmt
  # Check the staged dependencies, not the developer's .tools/PYTHONPATH.
  flatpak build --unshare=network "$BUILD" sh -c '
    export PYTHONPATH=/app/share/narratorverse/backend:/app/share/narratorverse/.runtime/lib/python3.13/site-packages
    export LD_LIBRARY_PATH=/app/share/narratorverse/.runtime/lib:/app/share/narratorverse/.runtime/lib64
    exec python3 -m narratorverse.quickmt_runtime
  '
else
  python3 "$ROOT/scripts/stage-flatpak.py" --dest "$BUILD/files"
fi
flatpak build-finish --command=narratorverse --share=ipc --share=network --socket=wayland --socket=fallback-x11 --socket=pulseaudio --device=dri "$BUILD"
flatpak build-export "$REPO" "$BUILD" testing
BUNDLE="$OUT/NarratorVerse-0.1.0-x86_64.flatpak"
SUMS=SHA256SUMS
if [ "$QUICKMT" = yes ]; then
  BUNDLE="$OUT/NarratorVerse-0.1.0-quickmt-x86_64.flatpak"
  SUMS=SHA256SUMS-quickmt
fi
TEMP="$BUNDLE.pending"
trap 'rm -f "$TEMP"' EXIT HUP INT TERM
flatpak build-bundle "$REPO" "$TEMP" io.github.DevVoidPL.NarratorVerse testing
mv "$TEMP" "$BUNDLE"
(cd "$OUT" && sha256sum "$(basename "$BUNDLE")" > "$SUMS")
printf 'Utworzono: %s\nSuma SHA-256: %s/%s\n' "$BUNDLE" "$OUT" "$SUMS"