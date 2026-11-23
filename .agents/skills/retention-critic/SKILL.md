---
name: retention-critic
description: >-
  Use this skill when performing multimodal QA on rendered clips using ffmpeg-mcp-lite frame extraction,
  evaluating 5-axis watchability/pacing/safe-zones (Samsung Galaxy A36), verifying -14 LUFS audio,
  and enforcing anti-DMCA and anti-AI-slop release gates before publication.
---

# Retention Critic — Clipping Studio

## Cuándo activar
- Después de cada render de clip candidato y **antes** de mover el master 9:16 a la carpeta de publicación.

## Protocolo de Evaluación (Gate de Salida)
1. **Inspección Técnica (`[RUNTIME REAL]` vía `ffmpeg-mcp-lite` / `ffprobe`):**
   - Confirmar resolución exacta `1080x1920` (9:16), `pix_fmt=yuv420p`, FPS constante (30/60), duración objetivo y pista de audio sin clipping (`<= -1.0 dBTP`, target `-14.0 LUFS`).
   - Extraer **4–6 frames clave en PNG/WebP** (segundo `0.2`, `1.5`, `3.0`, punto medio, pico de acción y cierre `-0.5s`) — nunca subir el MP4 completo al VLM salvo fallo crítico.
2. **Auditoría Visual de Safe-Zones 2026 (Doble Capa sobre `1080x1920` en HP 250 G8):**
   - Priorizar la inspección directa de `*_safezones.png` generados por `evals/media_check.py`.
   - **Capa Ámbar (TikTok / Reels):** `top 180px`, `right 150px`, `bottom 400px`.
   - **Capa Roja (YouTube Shorts / Universal 3 redes):** `top 288px` (15%), `right 192px` (18%), `bottom 672px` (35%), `left 48px` (4%). Todo subtítulo karaoke y callout debe quedar dentro de la caja segura universal despejada (`x` en `[48, 888]`, `y` en `[288, 1248]`).
   - **Filtro obligatorio de escena:** Rechazar inmediatamente todo candidato donde `scene_type != "active_gameplay"` en campañas de gameplay (cero tráilers, menús o reacciones a otros videos).
   - **Cero rostros cortados** y **cero subtítulos duplicados** sobre subtítulos nativos de GTA VI.
3. **Compuertas Binarias de Crítico Ciego (patrón ClipKaro + `CHECKED BY CODE`):**
   - **Pre-chequeo determinista (`CHECKED BY CODE`):** Antes del VLM/LLM, verificar por código que no haya corte a mitad de frase, falso arranque ni frame negro/congelado en `0.0–1.5s`.
   - **Gate 1 — `alone` (Test del Desconocido):** ¿Alguien que no vio el stream entiende en <3s qué pasa sin pronombres huérfanos ("that thing", "he")? Si falla -> **RECHAZAR o pedir Cold Open**.
   - **Gate 2 — `ending` (Cierre Completo revisando `AFTER THE CLIP`):** Comparar el cierre con los 3 segundos posteriores del transcript (`AFTER THE CLIP`); si el remate/punchline quedó afuera o corta una idea a medias -> **AJUSTAR `end_trim`**.
4. **Rúbrica de 5 Ejes (0.0 a 1.0 — Umbral de aprobación `overall >= 0.75`):**
   - `hook_3s`: ¿El segundo `0.0–3.0` arranca en acción/reacción real con Open Loop claro, sin frame congelado ni preámbulo muerto?
   - `pacing`: ¿Hay ritmo de acordeón emocional (tensión/liberación) sin dead air ni sobre-cortes frenéticos tipo "AI slop"?
   - `visual_framing`: ¿La webcam, el gameplay y los subtítulos karaoke son nítidos y respetan safe-zones?
   - `audio_dmca_safety`: ¿Audio limpio (`-14 LUFS`, `<= -1.0 dBTP`), voz entendible, cero música licenciada de radio GTA VI audible y `voice_mode` conforme a campaña?
   - `packaging_fit`: ¿El título/gancho cumple las reglas del bounty CPM o el puente a YouTube Long-Form?
5. **Bucle de Retroalimentación Acotado:**
   - Si `overall < 0.75` o falla una compuerta binaria, emitir feedback puntual y accionable para `av-director` / `media-engineer`.
   - **Límite duro:** `max_retries = 1`. Si falla en el segundo intento, **descartar el clip** (no quemar tokens intentando salvar footage mediocre).
