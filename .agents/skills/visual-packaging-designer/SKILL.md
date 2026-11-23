---
name: visual-packaging-designer
description: >-
  Use this skill when designing or auditing 9:16 editorial covers (Method C 2-layer),
  native 16:9 YouTube thumbnails (Text-Behind-Subject via rembg/birefnet-general-lite),
  4-network Universal Safe Box geometry, APCA perceptual contrast, Squint Test 160px,
  or Spectral Residual Saliency QA.
---

# Visual Packaging Designer (Skill #7) — Clipping Studio

## Cuándo activar
- Al generar o auditar portadas verticales `9:16` (`1080×1920`) para clips aprobados (Método C en 2 Capas).
- Al diseñar miniaturas horizontales `16:9` (`1280×720`) para compilaciones YouTube Long-Form (`8+ min`).
- Al evaluar CTR potencial, legibilidad móvil (*Squint Test* `160px`), saliencia pre-atentiva FFT y estética anti-AI slop.

## Protocolo Operativo
1. **Leer la biblia de diseño:** Consultar [docs/design_bible.md](../../../docs/design_bible.md) y, si se requieren fundamentos de grilla/color, consultar `python -m src.cli rag query "<tema>" --federated` (lectura `?mode=ro&immutable=1` de `Omni-RAG v6`).
2. **Método C Híbrido en 2 Capas (Vertical `9:16`):**
   - **Prohibido contaminar Frame 0 del MP4:** nunca insertar placas estáticas al inicio del video.
   - **Capa 1 (`select_peak_cover_frame`):** seleccionar el frame pico por nitidez Laplaciana + expresión facial `UltraFace` + energía RMS; emitir `cover_timestamp_ms` (TikTok API / Shorts selector) y `clip_XX_cover_clean.jpg`.
   - **Capa 2 (`render_editorial_cover_9_16`):** renderizar `clip_XX_cover_9_16.jpg` (`1080×1920` exactos) vía HTML5/CSS3/SVG + Chrome/Edge Headless con `Montserrat Black` embebida en base64, texto gancho de **`2–4` palabras** anclado en la costura (`y ≈ 670..830`) dentro de la **Caja Segura Universal 4-Redes `[x: 90..990, y: 420..1250]`** (seguro en IG `3:4`, TikTok `1:1`, YT Home `4:5` y FB Reels).
3. **Miniaturas Long-Form `16:9` (`1280×720`):**
   - **Fuente exclusiva:** `slice_*.mp4` horizontal nativo `1920×1080` (jamás estirar ni pillarboxear un clip `9:16`).
   - **Subject Matting comercial (KS-4):** usar `rembg` con **`birefnet-general-lite` (MIT)** (prohibido `bria-rmbg`). Validar cobertura de máscara (`0.04..0.72` del frame); si engloba demasiado fondo complejo de GTA VI, aplicar fallback limpio por ROI facial (`UltraFace`).
   - **Text-Behind-Subject:** tipografía monumental `Montserrat Black` (`tracking -0.03em`) entre fondo tratado y cutout con *rim-light* pictórico sutil + grano fílmico SVG (`0.05`).
4. **Reglas de Dirección de Arte (Anti-AI Slop + CTR):**
   - Máximo **2–3 elementos focales**; **Curiosity Gap complementario** (nunca repetir literalmente el título del video).
   - Presupuesto cromático **90/8/2**, *De-Boxing* (scrim direccional suave, cero cajas con bordes neón genéricos ni gradientes violeta-IA).
   - Contraste perceptual **`APCA Lc >= 75`** validado por código antes de cerrar.
   - Prohibir *AI-tells* en el texto (`"Not X. Y."`, `"The secret:"`, `"Game-changer"`, em-dashes, `?!`).

## Validación de Salida (`[RUNTIME REAL]`)
- Verificar programáticamente con `PIL.Image.size` que `cover_9_16.jpg == (1080, 1920)` y `thumbnail_1280x720.jpg == (1280, 720)`.
- Inspeccionar visualmente los 4 artefactos generados por `src/design_engine.py`:
  1. Pieza final (`cover_9_16.jpg` / `thumbnail_1280x720.jpg`) confirmando `Montserrat Black` sin fallback.
  2. `*_squint_160px.jpg` (legibilidad instantánea a tamaño feed móvil).
  3. `*_crop_preview.jpg` (encuadre limpio simultáneo en IG `3:4` y TikTok `1:1`).
  4. `*_saliency_heatmap.jpg` (atención pre-atentiva concentrada en sujeto + headline dentro de Caja Segura).
