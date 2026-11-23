# Style Bible — Clipping Studio (Prompt-Cache Ready, v1 Modo B-Lite GTA VI)

> Destilado único (solo lectura) desde Qdrant (`hunthub_omni_rag_v5`, `centinela_knowledge`, `yendo_obsidian_vault`), `.knowledge/` locales y patrones verificados de `_references/`. Optimizado para Prompt Caching (<2.000 tokens).

---

## 1. Estética Editorial Anti-AI Slop
- **Objetivo:** Que cada clip 9:16 parezca editado por un editor humano de agencia de gaming/broadcast, nunca por un generador genérico.
- **Prohibiciones absolutas (señales de AI slop penalizadas en 2026):**
  - Cortes robóticos cada 1.8s sin motivación narrativa ni respiración.
  - Subtítulos genéricos gigantes tapando la cara del streamer o los botones de TikTok/Shorts/Reels.
  - Cutscenes o gameplay mudo sin reacción del streamer ("contenido no original").
  - Cajas redundantes con bordes neón genéricos (*De-Boxing* + presupuesto cromático **90/8/2**: 90% imagen limpia, 8% soporte tipográfico sobrio, 2% acento cinético de alto contraste).
  - Emojis excesivos, B-roll genérico de stock desconectado de la escena o voz IA sobre reacciones orgánicas de streamers.

---

## 2. Narrativa y Ritmo de Cortes (Modo B-Lite GTA VI)
1. **Regla de Oro de Apertura (Acción -> Reacción):**
   - El segundo `0.0` abre **en el evento visual o frase detonante** (el choque, la traición, el disparo, la apuesta), **nunca** 2 segundos después en el grito ni en preámbulo muerto.
   - Si el streamer hace una predicción arrogante y falla 3 segundos después (*setup -> payoff*), abrir directo en la predicción sin saludo previo.
2. **Duración Objetivo:**
   - **Bounties CPM estándar:** **22s a 48s** (alta tasa de completado + replay loop natural).
   - **Historia completa / Multi-beat / Puente YPP:** **55s a 75s** solo si sostiene 2+ giros reales de tensión.
3. **Limpieza Selectiva de Aire Muerto:**
   - Eliminar silencios muertos >0.45s, pantallas BRB/carga y muletillas ("um", "uh").
   - **Excepción crítica (*Synchresis* — Michel Chion / Celia Hodent):** Conservar micro-silencios de **0.20s–0.35s** justo antes de un remate o explosión, y nunca cortar pausas llenas de disparos, motores o tensión visual inminente.
4. **Motion-Graphics Overlay Fluido (Cero Freeze-Frame Antifuncional — Regla 05):**
   - Prohibido congelar el video (`freeze-frame`) rompiendo la fluidez o dibujar callouts estáticos pequeños.
   - Máximo **1 `motion_overlay` RGBA animado por clip** en el pico cómico/caos (`1.5s–2.5s`, tipografía monumental `Montserrat Black`, escala editorial con transición suave y micro-SFX sincronizado a `-16/-18 dB`) mientras el video sigue corriendo fluido.

---

## 3. Composición Visual 9:16 (`1080x1920`) y Safe-Zones Móviles
Referencia de QA físico: **Samsung Galaxy A36** (pantalla vertical real en TikTok, YouTube Shorts e Instagram Reels).

- **Layout Primario GTA VI (`webcam_position: "top"`):**
  - **Banda superior (`~36%` / `692 px`):** Tarjeta webcam redondeada del streamer (`<= 1.8x`, anclada bajo `y = 180` sobre fondo ambiental difuminado, nunca vacío negro plano; detectada vía UltraFace + Gemini 2D Spatial Bounding Box `[ymin, xmin, ymax, xmax]` escala `0–1000`, nunca capturando rostros de NPCs).
  - **Banda inferior (`~64%` / `1228 px`):** Gameplay `1080p60` centrado sobre la acción principal con exclusión geométrica `+48 px` (`no_webcam_gameplay_overlap == True`).
  - **Costura central (entre webcam y gameplay):** Ubicación primaria de subtítulos `Montserrat Black` discretos y `motion_overlay`.
- **Safe-Zones Intocables (Margen libre de texto y callouts):**
  - **Superior:** `top 14%` (`~270 px`; `y >= 180` para tarjeta webcam) libre.
  - **Derecha:** `right 18%` (`x <= 888 px`) libre (columna universal de botones Like/Comentarios/Compartir en Shorts/TikTok/Reels).
  - **Inferior:** `bottom 35%` (`y <= 1248 px` para subtítulos/callouts; `bottom 22%` mínimo visual).
  - **Izquierda:** `left 4.5%–6%` (`x >= 48–65 px`) de respiro mínimo.
- **Reglas de Subtítulos Karaoke (`pysubs2` + `libass`):**
  - Máximo **2 a 4 palabras por golpe**, 1–2 líneas, fuente sans-serif bold limpia con trazo oscuro nítido (`Outline 4`, `Shadow 0–1`), palabra activa resaltada en color de acento (amarillo/blanco o acento del canal).
  - **Regla anti-duplicación (patrón ClipKaro):** Subtitular **solo** al streamer y amigos en Discord; **nunca** subtitular líneas de personajes/NPCs de GTA VI (el juego ya muestra sus propios subtítulos). Palabras ininteligibles se omiten antes que imprimir errores.

---

## 4. Audio, Mezcla y Psicoacústica (Destilado Mike Senior + Chion)
- **Estándar de Masterización Obligatorio:**
  - Loudness integrado: **`-14.0 LUFS`** (rango aceptable `-14.5` a `-13.5 LUFS`).
  - True Peak máximo: **`<= -1.0 dBTP`** (cero clipping digital en altavoces móviles).
- **Cadena DSP Limpia (FFmpeg / Pedalboard en Fase 2):**
  - High-pass suave a **60–80 Hz** sobre voz para limpiar retumbo de micrófono.
  - Compresión controlada (~3:1–4:1) para que susurros y gritos del streamer mantengan inteligibilidad sin reventar oídos.
  - Si hay SFX puntual de acento (whoosh/impacto en freeze-frame), alinear el transiente con el corte visual (`-20ms` a `0ms` de *synchresis*) y aplicar ducking `-8` a `-10 dB` si convive con voz.
- **Política de Voz (`VOICE_MODE`):**
  - **Bounties CPM (default):** `voice_mode: "none"` (100% voz original del streamer + audio limpio del juego).
  - **YPP / Editorial / Compilaciones:** `human_batch` (Cold Open 3–4s grabado con **Shure SM58 + Mackie Onyx Artist 1.2**) o `tts` comercial limpio (`kyutai/pocket-tts` / `openbmb/VoxCPM2`). Prohibido `OmniVoice` (`CC-BY-NC`).

---

## 5. Higiene Anti-DMCA / Content ID (Regla de Supervivencia GTA VI)
- **Realidad verificada (Nov 2026):** GTA VI dispara Content ID en YouTube y mutes en Twitch por radios, música ambiental de locales, menús y DJs **incluso cuando el streamer activó *Mute Licensed Music***.
- **Protocolo obligatorio pre-render:**
  1. Priorizar VODs donde el streamer tiene música licenciada silenciada o pistas OBS separadas.
  2. Detectar segmentos con música comercial reconocible de fondo durante la etapa de minería/curación; **descartar cualquier tramo con canción comercial audible sobre el diálogo**.
  3. Descartar tramos con desnudez accidental o triggers de restricción de edad (protege elegibilidad del canal en Shorts y YPP).
  4. Nunca publicar un clip dudoso "a ver si pasa": un bloqueo regional o claim destruye las vistas válidas para bounties CPM.
