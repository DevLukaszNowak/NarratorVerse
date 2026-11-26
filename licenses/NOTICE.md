# NarratorVerse — provenance and packaging limits

The Python Narrator models and services originate from DevVoidPL's GameOpti,
publication commit `df300c23e8a0a45a43fb136d75093f5b5e80e1a1`. The MIT license
and copyright notice are preserved in `LICENSE`. RapidOCR attribution in the
adapter is preserved in the original source files.

The application also uses Tauri, Svelte, Qt for Python/PySide6, Tesseract,
Leptonica, GStreamer, RapidOCR, ONNX Runtime, CTranslate2, SentencePiece,
sacremoses, Piper, Supertonic and chatterbox.cpp with ggml. Their licenses are separate
from the application license. Python distribution metadata and supplied license
files are retained in the local runtime. Model manifests preserve component
attribution and artifact/runtime license IDs.

Publishing this source tree is not redistribution approval for the copied runtime.
The tree contains application sources, manifests and license texts, not the
third-party wheels, native libraries or model weights. The SentencePiece
Apache-2.0 license and upstream NOTICE (including its copyright/contact) are
preserved.

## Voidek

Voidek is **not covered by MIT**. On October 31, 2026, its owner explicitly
authorized including the voice in NarratorVerse in the project conversation.
`VOIDEK-LICENSE.md` preserves the original notice and records this additional,
project-specific permission to redistribute the unmodified model with NarratorVerse.
The package builder includes `tts.voidek`; custom voices and all user
configuration/cache remain excluded. This permission does not relicense the
model or cover other third-party dependencies.

## Supertonic 3

The Flatpak bundles the `supertonic` 1.3.1 Python runtime with ONNX Runtime and
NumPy. The Supertonic 3 model files are downloaded from
`Supertone/supertonic-3`, revision `3cadd1ee6394adea1bd021217a0e650ede09a323`,
through the Components panel and are not part of the bundle.

`backend/narratorverse/assets/supertonic/Supertonic3-M5-02.json` is an
owner-authorized voice style adapted from preset M5 of that model. Its origin
is described in `PROVENANCE.txt` beside it. The style and the model are subject
to the OpenRAIL-M license in `Supertonic3-OpenRAIL-M.txt`, including its use
restrictions. The license is not changed by the MIT license of this repository.

## Chatterbox Multilingual V3

The experimental Vulkan engine bundles a chatterbox.cpp command-line build,
ggml libraries and the quantized Chatterbox Multilingual weights
`t3-v3-q8_0.gguf` and `s3gen-mtl-q8_0.gguf`. The MIT notices
are kept in `Chatterbox-cpp-MIT.txt`, `Chatterbox-ggml-MIT.txt` and
`Chatterbox-ResembleAI-MIT.txt`. The build sources and conversion scripts are
not part of this repository.

## Translation and speech recognition

The normal EN-to-PL translator runs the Argos Translate model package
`translate-en_pl-1_9` with CTranslate2. The model is OPUS-MT English to Polish
by Jörg Tiedemann and Santhosh Thottingal, CC BY 4.0. It is bundled as the
`translation.opus-en-pl` component with its original README and attribution.

The experimental QuickMT provider uses quickmt/quickmt-en-pl, revision
`6209580ce1fe525214f29fc39725007b73410109` (QuickMT, CC BY 4.0). The model is
downloaded separately and is not bundled.

Speech recognition uses Faster Whisper with `Systran/faster-whisper-small`,
revision `536b0662742c02347bc0e980a01041f333bce120`. The Python runtime and the
model are downloaded through the Components panel and are not part of the bundle. The
Whisper MIT license is kept in `Whisper-LICENSE.txt`.

## Runtime snapshot

The local package is assembled from an independently copied runtime snapshot,
not a reproducible source build of every native dependency. An inventory with
SHA-256 values and Python package license metadata is included under
`share/narratorverse/licenses`. This inventory does **not** replace the required
third-party notices, corresponding-source obligations or a redistribution
review (notably for Qt/Piper/GStreamer and bundled native dependencies).

No claim is made that this snapshot's license/source audit is complete.

## Open runtime obligations

The inspected snapshot also contains LGPL-labelled archive dependencies
(inflate64, multivolumefile, py7zr, pybcj and pyppmd), GEOS through Shapely,
and MPL-labelled certifi/tqdm metadata. These require file-level review, not
an assumption that every Python wheel is permissive. Bundled espeak-ng,
GStreamer plugins, codecs and the Qt WebEngine third-party tree remain part
of the native-library audit. Preserved ONNX Runtime/OpenCV third-party notices
and Piper COPYING do not close the missing Qt/CTranslate2 notice/source checks.

Before binary distribution, decide and document the supported source-delivery
method for each applicable GPL/LGPL/MPL component and assess whether the actual
Piper integration forms a combined work. No automatic relicensing of the
application, nor a blanket MIT-only binary distribution claim, is made here.
See `docs/REDISTRIBUTION.md` for the evidence and unresolved actions.
