# Design Bible — Clipping Studio (Prompt-Cache Ready, v3.1)

> Destilado editorial (<1.800 tokens) fusionando `Omni-RAG v6` (*Müller-Brockmann*, *Bringhurst*, *Albers*, *Gurney*, `CANON_ATOMIC_COLORIMETRY_OKLAB_APCA`, `taste-skill`, `copywriting/ai-tells`) + evidencia CTR gaming 2026 (*Paddy Galloway*, *MrBeast*).

---

## 1. Estrategia de Portadas Verticales (Método C Híbrido en 2 Capas)
- **Prohibido el truco de "Frame 0":** Nunca inyectar una placa estática al segundo `0.0` del MP4; daña el *autoplay* (70%–85% del tráfico) y desploma *Viewed vs. Swiped Away*.
- **Capa 1 (Obligatoria para todas las redes/APIs — cero contaminación de video):**
  - Seleccionar el frame pico determinista (`select_peak_cover_frame`: nitidez Laplaciana + expresión facial `UltraFace` + pico RMS, descartando blur/parpadeo).
  - Emitir `cover_timestamp_ms` (para **TikTok Content Posting API** `post_info.video_cover_timestamp_ms` y selector móvil de **YouTube Shorts**) + `clip_XX_cover_clean.jpg` (`1080×1920`).
- **Capa 2 (Editorial `<1s`, costo `$0`, HITL/Desktop/Canal Propio):**
  - `clip_XX_cover_9_16.jpg` (`1080×1920` exactos) para **YouTube Studio Desktop / `thumbnails.set`**, **Instagram Reels** (*Upload Cover + Adjust Profile Grid*), **Facebook Reels (Meta Business Suite)** y **TikTok Web/App**.
- **Miniaturas Long-Form (`16:9`):**
  - `thumbnail_1280x720.jpg` (`1280×720` exactos) generada **exclusivamente desde `slice_*.mp4` horizontal nativo `1920×1080`** (prohibido usar un master `9:16` con pillarbox negro).

---

## 2. Composición, Grilla y Psicología de CTR (Galloway + MrBeast + Müller-Brockmann)
1. **Regla de los 2–3 Elementos Focales:** Máximo 3 anclas visuales por pieza (ej. rostro expresivo del streamer + sujeto/caos del juego + texto gancho de `2–4` palabras). Todo elemento que no guíe el ojo se elimina.
2. **Curiosity Gap Complementario (Cero Duplicación):** El texto de la portada **nunca** repite el título literal del video; completa la historia abriendo una pregunta visual (`Título`: *"xQc's $500K GTA VI Heist Went Horribly Wrong"* -> `Portada`: `"HE LOST IT ALL"`).
3. **Flujo Izquierda -> Derecha y Regla de Tercios (`16:9`):**
   - Sujeto/reacción del streamer recortado con **`rembg` + `birefnet-general-lite` (MIT)** ubicado en el tercio derecho o izquierdo con mirada hacia el foco de caos.
   - **Text-Behind-Subject:** Tipografía monumental ubicada en capa media (detrás del primer plano del sujeto pero sobre el fondo oscurecido/contrastado), separada con un *rim-light* pictórico sutil (*James Gurney — Color & Light*) que despega la silueta del fondo.
4. **Anclaje en `9:16` — Caja Segura Universal 4-Redes:**
   - Todo texto gancho y punto focal crítico DEBE vivir dentro de **`[x: 90..990, y: 420..1250]`** sobre lienzo `1080×1920`:
     - Sobrevive al recorte de grilla **Instagram `3:4`** (`1080×1440`, `y: 240..1680`).
     - Sobrevive al recorte de grilla **TikTok `1:1`** (`1080×1080`, `y: 420..1500`).
     - Sobrevive a **YouTube Home `4:5`** y márgenes UI de **Facebook Reels (`top 14%`, `bottom 35%`, `lados 6%`)**.
   - **Posición óptima del headline `9:16`:** Costura webcam/gameplay (**`y ≈ 670..830`**) sin tapar ojos/boca ni la acción central.

---

## 3. Tipografía, Colorimetría APCA y Disciplina Anti-AI Slop
1. **Sistema Tipográfico Único:**
   - Fuente obligatoria: **`Montserrat Black`** (`assets/fonts/Montserrat-Black.ttf`, SIL OFL), embebida vía `@font-face` (`data:font/ttf;base64,...`) en Chrome/Edge Headless para evitar fallbacks de sistema en Windows.
   - Tracking negativo editorial **`-0.03em`**, interlineado ajustado **`0.90–0.96`**, mayúsculas contundentes, máximo **`2–4` palabras** (nivel de lectura instantánea en `<0.4s`).
2. **Colorimetría Perceptual (*Albers* + `CANON_ATOMIC_COLORIMETRY_OKLAB_APCA`):**
   - Presupuesto cromático **90/8/2** (90% fotografía/gameplay real con gradiente oscuro selectivo de soporte, 8% blanco editorial `#F8FAFC` / negro tinta `#090B10`, 2% acento cálido de alta luminancia `#FFD400` o `#FF5A1F` solo en 1 palabra de énfasis).
   - **Gate APCA obligatorio:** Contraste perceptual **`Lc >= 75`** entre texto gancho y su superficie local efectiva (validado programáticamente antes de aprobar el render).
   - **Simultaneous Contrast (*Albers*):** Aplicar una viñeta/scrim direccional suave detrás del texto (`rgba(6, 8, 14, 0.72)` -> transparente) en lugar de cajas sólidas con bordes neón (*De-Boxing*).
3. **Anti-Defaults y Anti-Tells (`taste-skill` + `ai-tells.md`):**
   - **Prohibido:** gradientes violeta-IA genéricos, cajas glassmorphism flotantes, stickers/emojis amontonados, flechas rojas baratas de 2016, bordes neón en todo, copy con *"Not X. Y."*, *"The secret:"*, *"Game-changer"*, em-dashes en titulares o signos `?!` repetidos.
   - **Textura editorial:** Grano fílmico SVG sutil (`opacity: 0.045–0.06`, `mix-blend-mode: overlay`) y contraste limpio sin sobre-saturación artificial.

---

## 4. Compuertas Deterministas de QA Visual (Antes de Publicar)
Todo cover/thumbnail emitido por `src/design_engine.py` debe superar 4 validaciones automáticas:
1. **Dimensiones exactas (`PIL.Image.size`):** `(1080, 1920)` para `cover_9_16.jpg` y `(1280, 720)` para `thumbnail_1280x720.jpg` (cero recorte por DPI/monitor de Windows).
2. **Squint Test (`*_squint_160px.jpg`):** Al reducir a `160px` de ancho (tamaño real en feed móvil), el sujeto y el texto de `2–4` palabras siguen siendo legibles en `<0.5s`.
3. **Crop Preview (`*_crop_preview.jpg`):** Vista combinada con guías reales de **Instagram `3:4`** y **TikTok `1:1`** confirmando cero texto o rostro cortado.
4. **Spectral Residual Saliency (`*_saliency_heatmap.jpg`):** Mapa de atención pre-atentiva FFT 2D (`<50 ms` CPU) verificando que el centro de masa de saliencia (`>= 55%` de energía saliente) caiga dentro de la Caja Segura Universal.
