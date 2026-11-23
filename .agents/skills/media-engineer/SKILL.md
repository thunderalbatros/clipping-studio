---
name: media-engineer
description: >-
  Use this skill when building or debugging deterministic media ingestion (yt-dlp, Deno/Node EJS,
  curl_cffi, TwitchDownloaderCLI), ASR (parakeet-tdt-0.6b-v3 onnx-asr, faster-whisper),
  ONNX speaker/face tracking, FFmpeg filtergraphs (h264_qsv, h264_nvenc, libass), or compute_profile routing.
---

# Media Engineer — Clipping Studio

## Cuándo activar
- Al implementar, optimizar o depurar pipelines de descarga parcial, transcripción, diarización, encuadre ONNX y render `FFmpeg`.
- Al verificar flags actualizados de librerías (`yt-dlp`, `FFmpeg`, `onnxruntime`, `pysubs2`, `pydantic`) usando `context7`.

## Protocolo Operativo
1. **Consultar documentación viva y patrones locales:**
   - Usar `context7` antes de escribir flags complejos de `yt-dlp`, `FFmpeg` o SDKs.
   - Revisar [docs/engineering_patterns.md](../../../docs/engineering_patterns.md) para reusar patrones ya validados de `_references/` y proyectos locales.
2. **Ingesta Determinista (VOD + Live Sniper):**
   - Descargar solo slices (`--download-sections`), Smart HLS `#EXT-X-BYTERANGE` vía `curl_cffi` o *HLS `.ts` Ring-Buffer* (`90 s` en RAM) en `Live-Stream Sniper`.
   - En Windows: `--cookies <cookies.txt>` (nunca `--cookies-from-browser`), JS runtime explícito (`--js-runtimes deno --remote-components ejs:github`), `--impersonate chrome` (`curl_cffi`) para Kick, `streamlink --twitch-disable-ads` para Twitch live y `load_dotenv(override=True)` al arranque.
   - En Twitch VOD: usar `TwitchDownloaderCLI chatdownload --id <VOD_ID> --threads 6 --collision Overwrite --log-level Error --bttv=false --ffz=false --stv=false` con ventana acotada `-b`/`-e` en VODs >3 h, respetando `runs/ <= 4.0 GB`.
3. **ASR, Visión y Guardas de Hardware (`compute_profile` + `resource_guard`):**
   - Primario EN/ES: `nvidia/parakeet-tdt-0.6b-v3` vía `onnx-asr` (`int8`), `UltraFace` + `LR-ASD` ONNX (`<=240px` anti-OOM), `YAMNet` ONNX AudioSet anti-DMCA.
   - Aplicar `apply_thread_budget()` (`min(4, cores - 2)`) y `heavy_local_allowed()` de `src/resource_guard.py`.
   - En `colab_t4` (`sm_75`): forzar siempre `float16`/`int8` + `sdpa`; nunca `bfloat16`, `fp8` ni `flash_attention_2`.
4. **Render `FFmpeg` + `libass` Senior en Windows 10:**
   - Usar fuente comercial SIL OFL **`Montserrat Black`** (`assets/fonts/Montserrat-Black.ttf`) cargada con `ass=subs.ass:fontsdir=fonts` desde `cwd` relativo (jamás rutas `C:\...`).
   - Preservar FPS nativo (`60fps`), `lanczos`, `h264_qsv -global_quality 18 -look_ahead 0` (`format=nv12`) con fallback `libx264 -preset fast -crf 18`.
   - Aplicar *profanity bleeper* en `0.0–7.5 s`, jump-cuts selectivos acústicamente verificados, *punch-in zoom* vía `overlay` sin jitter, mezcla de SFX CC0 (`assets/sfx/`) y **`loudnorm` 2-pass** (`-14.0 LUFS`, `<= -1.0 dBTP`) + `aresample=48000` + `alimiter=limit=0.87:level=false`.
   - Mantener dependencias fijadas en `pyproject.toml` / `requirements.lock` y cobertura en `tests/` (`pytest`).

## Validación en Runtime (Obligatoria)
- Verificar todo MP4 generado con `ffprobe` o `ffmpeg-mcp-lite`: `1080x1920`, `yuv420p`, `h264`, audio presente, sin frames congelados al segundo `0.0`–`1.0`.
