---
name: retention-critic
description: >-
  Use this skill when performing multimodal QA on rendered clips and editorial covers/thumbnails
  using ffmpeg-mcp-lite frame extraction, evaluating 5-axis watchability/pacing/safe-zones
  (Samsung Galaxy A36), Squint Test 160px, Spectral Saliency, APCA contrast, -14 LUFS audio,
  and anti-DMCA/anti-AI-slop release gates before publication.
---

# Retention Critic — Clipping Studio

## Cuándo activar
- Después de cada render de clip `9:16`, portada `cover_9_16.jpg` o miniatura `thumbnail_1280x720.jpg`, y **antes** de aprobar la publicación.

## Protocolo de Evaluación (Gate de Salida)
1. **Inspección Técnica de Video (`[RUNTIME REAL]` vía `ffmpeg-mcp-lite` / `ffprobe`):**
   - Confirmar resolución exacta `1080x1920` (9:16), `pix_fmt=yuv420p`, FPS constante (30/60), duración objetivo y audio limpio (`-14.0 LUFS`, `<= -1.0 dBTP`).
   - Extraer **4–6 frames clave en PNG/WebP** (`0.2s`, `1.5s`, `3.0s`, medio, pico de acción y cierre `-0.5s`); nunca subir el MP4 completo al VLM salvo fallo crítico.
2. **Auditoría de Safe-Zones de Video (5 Redes sobre `1080x1920`):**
   - Inspeccionar `*_safezones.png` (`evals/media_check.py`).
   - **Capa Ámbar (TikTok / IG / FB):** `top 180px`, `right 150px`, `bottom 400px` (`450px` en FB). Tarjeta webcam bajo `y = 180` con ojos/boca libres.
   - **Capa Roja (YT Shorts / Universal 5 redes):** todo subtítulo `Montserrat Black` y callout dentro de `[48..888, 288..1248]`.
   - **X:** confirmar impacto visual/textual claro en `0.0–0.5s` en autoplay silencioso.
   - **Filtros duros:** `scene_type == "active_gameplay"`, **cero groserías sin bleep/censura OCR en `0.0–7.5s`**, cero tartamudeos de 1 letra y cero subtítulos duplicados de NPCs.
3. **Gate Visual de Portadas `9:16` y Miniaturas `16:9` (Método C + `seam-gate`):**
   - **Dimensiones exactas (`PIL.Image.size`):** `(1080, 1920)` en `cover_9_16.jpg` y `(1280, 720)` en `thumbnail_1280x720.jpg` (cero recorte DPI, cero pillarbox negro en `16:9`).
   - **Tipografía y Contraste:** confirmar visualmente **`Montserrat Black`** sin fallback, **`APCA Lc >= 75`**, máximo 2–4 palabras complementarias al título y cero AI-tells.
   - **Squint Test (`*_squint_160px.jpg`):** sujeto y texto legibles instantáneamente a `160px` de ancho.
   - **Caja Segura Universal 4-Redes (`*_crop_preview.jpg`):** texto y rostro íntegros simultáneamente en **Instagram `3:4`** y **TikTok `1:1`** (`[90..990, 420..1250]`).
   - **Saliencia Pre-Atentiva (`*_saliency_heatmap.jpg`):** confirmar que el foco FFT 2D cae sobre rostro + texto dentro de la Caja Segura (`>= 55%` de masa saliente).
   - **Chequeo `seam-gate`:** verificar que no existan costuras rotas, halos sucios de recorte `BiRefNet-lite` ni texto chocando contra bordes/rostro.
4. **Compuertas Binarias de Crítico Ciego (ClipKaro `honesty` / `wordcheck` + `CHECKED BY CODE`):**
   - **`CHECKED BY CODE`:** verificar cero corte a mitad de frase, cero falso arranque, cero frame congelado/negro en `0.0–1.5s`.
   - **`alone` (Test del Desconocido):** ¿alguien ajeno al stream entiende en `<3s` sin pronombres huérfanos?
   - **`ending` (`AFTER THE CLIP`):** revisar los 3s posteriores del transcript para confirmar cierre completo y *Seamless Loop* limpio.
   - **`wordcheck`:** confirmar que el transcript/subtítulos y titulares no inventan hechos ni lore inexistente en el clip.
5. **Rúbrica de 5 Ejes (0.0 a 1.0 — Umbral `overall >= 0.75`, `max_retries = 1`):**
   - `hook_3s`, `pacing`, `visual_framing`, `audio_dmca_safety`, `packaging_fit`.
   - Si falla en el reintento (`max_retries = 1`), **descartar el clip** y registrar feedback en `runs/studio.db` para alimentar la memoria episódica del RAG.
