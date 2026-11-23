---
name: av-director
description: >-
  Use this skill when designing clip narrative arcs, selecting timestamps from transcripts/peaks,
  specifying 9:16 split-screen webcam-over-gameplay layouts, freeze-frame callouts, audio pacing,
  or generating structured EditPlan manifests for Modo B-Lite (GTA VI) and future modes.
---

# Audiovisual Director — Clipping Studio

## Cuándo activar
- Al transformar un transcript + picos de chat/audio en un `EditPlan` estructurado (Pydantic/YAML).
- Al refinar puntos exactos de entrada/salida (`TrimRefiner`) y encuadres 9:16.

## Protocolo Operativo
1. **Leer la biblia editorial:** Consultar [docs/style_bible.md](../../../docs/style_bible.md) antes de aprobar cualquier corte o estilo visual.
2. **Selección Editorial (Anti-AI Slop — Modo B-Lite GTA VI):**
   - **Acción -> Reacción, nunca al revés:** El clip abre en el choque, disparo, traición o evento visual que dispara el caos — NO después del grito del streamer.
   - **Ritmo de Acordeón Emocional (Walter Murch / Hayden Hillier-Smith):** Cortar únicamente por cambio psicológico/emocional (*motivated cutting*), nunca con metrónomo robótico de 1.5s. Alternar planos rápidos de caos con micro-respiros de tensión y plantear **Open Loops** cada 3–5s cuya resolución llegue al final.
   - **Momentos Visuales Silenciosos (patrón Contact-Sheet de ClipKaro):** No depender solo del transcript; revisar hojas de frames (1 frame/10s -> zoom 1 frame/s) para detectar bugs, choques o gestos virales donde el streamer se queda mudo.
   - **Contexto Vivo (`context/gta6_living_brief.md`):** Leer siempre el brief diario antes de redactar ganchos o asignar `matched_trend_id` para Multi-POV (evita alucinaciones de lore).
   - **Cero cutscenes crudas:** Descartar tramos de cinemática o gameplay sin comentario/reacción activa del streamer (riesgo de rechazo por "contenido no original").
   - **Filtro de subtítulos inteligentes (patrón ClipKaro):** Subtitular solo la voz del streamer y amigos en Discord; **nunca** duplicar subtítulos de NPCs/diálogos internos del juego.
   - **Limpieza de aire muerto selectiva:** Cortar silencios muertos >0.45s y muletillas ("um", "uh"), pero **conservar** micro-pausas de tensión (0.2s–0.35s) previas a un impacto o remate (*Synchresis* de Chion) y conservar sonidos clave del juego (explosiones, disparos).
3. **Composición 9:16 Calibrada (Estudio Senior):**
   - **Layout Streamer GTA VI (`webcam_position: "top"`):** Banda superior (`692 px`) con tarjeta webcam redondeada `<= 1.8x` anclada bajo `y = 180` sobre backdrop difuminado (o split limpio si `webcam_w >= 540 px`); banda inferior (`1228 px`) con gameplay nativo `1080p60` y exclusión geométrica `+48 px` (`no_webcam_gameplay_overlap == True`).
   - **Subtítulos `Montserrat Black` discretos** (cero `\kf`, `<= 3` palabras / `<= 16` chars, centrados en `(540, 1120)` dentro de la Caja Roja universal, excluyendo NPCs vía `LR-ASD` y tartamudeos de 1 letra).
   - **Edición dinámica motivada:** jump-cuts selectivos solo sobre silencio acústico real `<-30 dBFS` (`>0.95s`, preservando `0.25s` de *Synchresis*), máx. 2 *punch-in zooms* `1.10x–1.12x` en picos vocales/acción, 1–2 micro-SFX CC0 (`pop`/`whoosh` a `-16/-18 dB`) y máximo 1 freeze-frame callout (`0.6s–1.0s`).
   - **Style DNA:** Consultar `context/viral_style_dna.json` cuando exista para alinear duración y ritmo a los outliers ganadores.
4. **Política de Voz y Anti-DMCA:**
   - Respetar `voice_mode: "none"` en bounties CPM; solo activar `human_batch` (Shure SM58) para Cold Opens de YPP/Long-Form.
   - Rechazar cualquier candidato con radio licenciada o música ambiental de GTA VI audible sobre el diálogo.

## Validación de Salida
- Entregar un `EditPlan` válido con timestamps precisos (`start_trim`, `end_trim`), tipo de layout, bandera `dmca_clear` y justificación en 1 línea de por qué retiene en los primeros 3 segundos.
