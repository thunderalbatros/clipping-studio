---
name: av-director
description: >-
  Use this skill when designing clip narrative arcs, selecting timestamps from transcripts/peaks,
  specifying 9:16 split-screen webcam-over-gameplay layouts, freeze-frame callouts, audio pacing,
  auto-editor dead-air tuning, or generating structured EditPlan manifests for Modo B-Lite (GTA VI).
---

# Audiovisual Director — Clipping Studio

## Cuándo activar
- Al transformar un transcript + picos de chat/audio en un `EditPlan` estructurado (Pydantic/YAML).
- Al refinar puntos exactos de entrada/salida (`TrimRefiner`), *Seamless Loops*, pausas *Synchresis* y encuadres 9:16.

## Protocolo Operativo
1. **Leer biblias y memoria episódica:** Consultar [docs/style_bible.md](../../../docs/style_bible.md), [docs/shortform_and_editing_bible.md](../../../docs/shortform_and_editing_bible.md) y `context/streamer_profiles.json`. Consultar el RAG (`python -m src.cli rag query "<tema>"`) para recuperar Top-3 ganadores y Top-2 errores episódicos antes de cerrar cortes.
2. **Montaje Motivado (Walter Murch *Rule of Six* + Bruce Block):**
   - **Jerarquía de Murch:** Emoción (51%) > Historia (23%) > Ritmo (10%) > Trazado Ocular (7%) > Plano 2D (5%) > Espacio 3D (4%). Jamás cortar con metrónomo robótico cada 1.5s–1.8s.
   - **Acción -> Reacción (Frame 0 vivo):** El segundo `0.0` abre en el evento visual o frase detonante, nunca en saludo, preámbulo muerto ni placa estática.
   - **Contraste Visual (*Bruce Block*):** Alternar *setup* estable con *payoff* de alta intensidad (`punch-in zoom` `1.10x–1.12x`, micro-SFX motivado `-16/-18 dB`, máx. 1 freeze-frame callout `0.6s–0.9s`).
   - **Seamless Loop (`APV >= 100%`):** Cortar el cierre exactamente en el clímax/remate sin cola muerta ni despedida para disparar repeticiones orgánicas.
   - **Momentos Visuales Silenciosos (ClipKaro Contact-Sheet):** Revisar frames cuando el streamer queda mudo ante un bug/choque viral.
   - **Contexto Vivo:** Consultar `context/gta6_living_brief.md` y `context/viral_style_dna.json`.
   - **Cero cutscenes crudas / cero subtítulos de NPCs:** Subtitular solo al streamer y amigos en Discord (`LR-ASD` activo).
3. **Limpieza de Aire Muerto (`auto-editor` + *Synchresis*):**
   - Integrar `auto-editor` como motor determinista de aire muerto con **`--margin 0.25s`** obligatorio (preservando la pausa *Synchresis* de Michel Chion antes de impactos/remates y silencios dramáticos con audio del juego).
   - Solo recortar aire muerto sostenido (`>0.95s` bajo `-30 dBFS`), con fallback automático al analizador PCM.
4. **Composición 9:16 Calibrada (Estudio Senior):**
   - **Layout Streamer GTA VI (`webcam_position: "top"`):** Banda superior (`692 px`) con tarjeta webcam redondeada `<= 1.8x` anclada bajo `y = 180`; banda inferior (`1228 px`) con gameplay `1080p60` y exclusión geométrica `+48 px` (`no_webcam_gameplay_overlap == True`).
   - **Subtítulos `Montserrat Black` discretos:** cero `\kf`, `<= 3` palabras / `<= 16` chars, centrados en `(540, 1120)` dentro de la Caja Roja universal, sin tartamudeos de 1 letra y con censura `0–7.5s` (audio + texto OCR).
5. **Política de Voz y Anti-DMCA:**
   - `voice_mode: "none"` obligatorio en bounties CPM; `human_batch` (Shure SM58) reservado al canal YPP/Long-Form.
   - Rechazar cualquier tramo con música licenciada de radio/locales/menús de GTA VI audible sobre el diálogo (`YAMNet`).

## Validación de Salida
- Entregar un `EditPlan` Pydantic válido con `start_trim`, `end_trim`, `dmca_clear`, `ab_hook_variants` (2–3 ganchos cortos para Trial Reels/A-B) y justificación en 1 línea de retención `0–3s` y cierre *Seamless Loop*.
