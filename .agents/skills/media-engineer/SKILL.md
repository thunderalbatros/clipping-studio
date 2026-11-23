---
name: media-engineer
description: >-
  Use this skill when building or debugging deterministic media ingestion (yt-dlp, Deno EJS,
  curl_cffi, TwitchDownloaderCLI), ASR (parakeet-tdt-0.6b-v3 onnx-asr), ONNX models
  (UltraFace, LR-ASD, YAMNet, rembg BiRefNet-lite), auto-editor, FFmpeg/libass, Chrome Headless
  rendering, SQLite sqlite-vec/FTS5 RAG, or compute_profile routing.
---

# Media Engineer — Clipping Studio

## Cuándo activar
- Al implementar, optimizar o depurar ingesta parcial, ASR, visión ONNX, `auto-editor`, `FFmpeg`/`libass`, `src/design_engine.py`, `src/rag_engine.py` o `Live-Stream Sniper`.
- Al consultar APIs actualizadas vía `context7` o diagnosticar entorno Windows 10 (`cli doctor`).

## Protocolo Operativo
1. **Principio Integrate-over-Build y Documentación Viva:**
   - Usar `context7` para verificar flags de `yt-dlp`, `FFmpeg`, `onnxruntime`, `sqlite-vec`, `fastembed`, `rembg` y `auto-editor`.
   - Consultar [docs/engineering_patterns.md](../../../docs/engineering_patterns.md) y [docs/shortform_and_editing_bible.md](../../../docs/shortform_and_editing_bible.md).
2. **Ingesta Determinista (VOD + Live Sniper):**
   - Solo slices parciales (`--download-sections`), Smart HLS `#EXT-X-BYTERANGE` (`curl_cffi --impersonate chrome`) o *HLS `.ts` Ring-Buffer* (`90 s` en RAM).
   - Windows: `--cookies <cookies.txt>` (nunca `--cookies-from-browser`), `--js-runtimes deno --remote-components ejs:github` para YouTube 1080p, `streamlink --twitch-disable-ads` para Twitch live, `TwitchDownloaderCLI --threads 6 --bttv=false --ffz=false --stv=false` y `load_dotenv(override=True)`.
3. **ONNX Stack, Integridad `.venv` y `resource_guard`:**
   - Stack comercial (KS-4): `parakeet-tdt-0.6b-v3` (`onnx-asr` `int8`), `UltraFace`, `LR-ASD`, `YAMNet`, **`rembg` + `birefnet-general-lite` (MIT — prohibido `bria-rmbg`)** y `fastembed` (`BAAI/bge-small-en-v1.5`).
   - Respetar siempre `apply_thread_budget()` (`min(4, cores - 2)`) y `resource_guard.onnx_threads()`.
   - **Guardrail de integridad `onnxruntime`:** Tras instalar o actualizar librerías ONNX, verificar versión de `onnxruntime` y ejecutar smoke test real confirmando que `parakeet-tdt-0.6b-v3`, `LR-ASD` y `YAMNet` corren sin regresiones.
   - En `colab_t4` (`sm_75`): forzar `float16`/`int8` + `sdpa` (prohibido `bfloat16`, `fp8`, `flash_attention_2`).
4. **Render `FFmpeg`, `auto-editor` y `DesignEngine` en Windows 10:**
   - **`auto-editor`:** Usar análisis estructurado con `--margin 0.25s` (*Synchresis*) y preservación de silencios dramáticos gaming, con fallback automático al analizador PCM. Soportar `--ab-variants 2` con hash visual distinto.
   - **`FFmpeg` + `libass`:** `Montserrat Black` vía `ass=subs.ass:fontsdir=fonts` desde `cwd` relativo (nunca `C:\...`), `format=nv12` con `h264_qsv` (fallback `libx264 -preset fast -crf 18`), bleeper `0–7.5s`, `loudnorm` 2-pass (`-14.0 LUFS`, `<= -1.0 dBTP`).
   - **Chrome/Edge Headless (`src/design_engine.py`):** Embeber `Montserrat-Black.ttf` en `@font-face` (`data:font/ttf;base64,...`), usar `--headless=new --force-device-scale-factor=1 --hide-scrollbars`, validar dimensiones exactas con `PIL.Image.size` (`(1080, 1920)` y `(1280, 720)`), y aplicar ROI fallback en `birefnet-general-lite` si cobertura de máscara excede `0.72` o baja de `0.04`.
5. **RAG Local + Puente Federado (`src/rag_engine.py`):**
   - `runs/studio.db`: `rag_chunks`, `rag_chunks_fts` (BM25) y `rag_chunks_vec` (`sqlite-vec` `float[384]`) fusionados con **RRF ($k=60$)**.
   - Puente hacia `web-software-factory/.cache/rag_hot.sqlite` y `rag_library.sqlite`: abrir **siempre** con SQLite URI `?mode=ro&immutable=1` (cero `-wal`/`-shm` creados ni bloqueos sobre repos externos) con degradación silenciosa si la ruta no existe.

## Validación en Runtime (Obligatoria)
- Verificar todo MP4 con `ffprobe` (`1080x1920`, `yuv420p`, `h264`, `-14 LUFS`, sin freeze en `0.0–1.0s`).
- Verificar covers/thumbnails con `PIL.Image.size`, `cli doctor`, `ruff` y `pytest`.
