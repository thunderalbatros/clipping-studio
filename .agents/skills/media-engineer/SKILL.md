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
2. **Ingesta Determinista (Cero VODs de 40 GB):**
   - Descargar solo slices (`--download-sections`) o usar stream-piping (`yt-dlp -o - | ffmpeg -i pipe:0`).
   - En Windows: `--cookies <cookies.txt>` (nunca `--cookies-from-browser`), JS runtime explícito (`--js-runtimes deno --remote-components ejs:github`), `--impersonate chrome` (`curl_cffi`) para Kick, y `load_dotenv(override=True)` al arranque para evitar variables previas de Windows.
   - En Twitch: usar `TwitchDownloaderCLI chatdownload --id <VOD_ID> -o chat.json --bttv=false --ffz=false --stv=false` (sin `--embed-images`) y mantener cuota dura `runs/ <= 4.0 GB`.
3. **ASR y Visión Compatibles con 3 Perfiles (`compute_profile`):**
   - Primario EN/ES: `nvidia/parakeet-tdt-0.6b-v3` vía `onnx-asr` (`int8`), fallback `faster-whisper` (`large-v3-turbo`, `int8`).
   - En `colab_t4` (`sm_75`): forzar siempre `float16`/`int8` + `sdpa`; nunca `bfloat16`, `fp8` ni `flash_attention_2`.
4. **Render `FFmpeg` + `libass` en Windows 10:**
   - Usar fuente `"Segoe UI Black"` y ejecutar `FFmpeg` con `cwd` en la carpeta del clip y filtro relativo `ass=subs.ass` (jamás rutas `C:\...` dentro de `-vf`).
   - Encodear con `h264_qsv` en `local_hp` (`ass=subs.ass,format=nv12` -> `-c:v h264_qsv`), `h264_nvenc` en L4/T4, y fallback automático `libx264 -preset veryfast`.
   - Prohibido `-c copy` en el inicio del clip. En overlays con retardo temporal usar `setpts=PTS-STARTPTS+{start}/TB` y `eof_action=pass`.
   - Normalizar audio a **`-14.0 LUFS`** y true peak **`<= -1.0 dBTP`** mediante **`loudnorm` en 2 pasadas** (Pass 1 `print_format=json` -> Pass 2 `linear=true:measured_I=...`) + **`aresample=48000`** (nunca single-pass a 192 kHz).

## Validación en Runtime (Obligatoria)
- Verificar todo MP4 generado con `ffprobe` o `ffmpeg-mcp-lite`: `1080x1920`, `yuv420p`, `h264`, audio presente, sin frames congelados al segundo `0.0`–`1.0`.
