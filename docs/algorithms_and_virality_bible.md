# Biblia Oficial de Algoritmos de Video, RecSys y Ciencia de Viralidad (5 Plataformas, v4.0 — Nov 2026)

> **Propósito:** Base de conocimiento canónica indexada en `runs/studio.db` (`rag_chunks` + `rag_chunks_fts` + `rag_chunks_vec` `float[384]`, dominio `shortform_craft`) para el equipo agéntico de **Clipping Studio**. Fusiona código fuente oficial (`xai-org/x-algorithm` Oct–Nov 2026), documentación oficial de plataformas (YouTube Help Center 2025–2026, Meta Transparency / Engineering Blog, TikTok Creator Portal), papers primarios de RecSys (Google/YouTube, ByteDance, Meta), ciencia cognitiva (Loewenstein, Kahneman, Heath), libros de edición/marketing (Murch, Bruce Block, Chion, Berger, Eyal, Hodent, Eves, Kane) y masterclasses de retención (Hayden Hillier-Smith, Paddy Galloway, Jenny Hoyos, Kallaway, MrBeast).

---

## 1. YouTube Shorts — Arquitectura Oficial, Papers RecSys, Engaged Views (2025–2026) y Políticas YPP

### 1.1 Fundamentos de Ingeniería (Papers Primarios de Google / YouTube)
1. **Covington et al. (2016, RecSys — *Deep Neural Networks for YouTube Recommendations*):**
   - Embudo en 2 etapas: **Candidate Generation** (embeddings Two-Tower + historial de co-visualización/búsquedas reduce miles de millones de videos a cientos) -> **Ranking**.
   - **Feature `Example Age` (Frescura):** YouTube modela explícitamente la antigüedad del video para contrarrestar el sesgo de popularidad histórica y **promover subidas frescas**. Implicación directa: subir clips de un momento viral de GTA VI dentro de las primeras **2–6 horas** del stream captura un impulso estructural de exploración imposible de replicar 3 días después.
2. **Zhao et al. (2019, RecSys — *Recommending What Video to Watch Next: A Multitask Ranking System*):**
   - **MMoE (Multi-gate Mixture of Experts):** Sub-redes expertas compartidas con compuertas específicas que separan **Objetivos de Engagement** (no deslizar, porcentaje visto, rewatch) de **Objetivos de Satisfacción** (likes, shares, suscripciones, encuestas post-visualización 1–5 estrellas).
   - **Shallow Tower (Debiasing de Posición/Selección):** Elimina sesgo de posición para medir calidad intrínseca. Clickbait vacío genera clics pero destruye satisfacción -> penalización a nivel canal.
3. **Chen et al. (2019, WSDM — *Top-K Off-Policy Correction for a REINFORCE Recommender System*):**
   - Optimiza la **recompensa de sesión a largo plazo** (que el usuario regrese a YouTube), no un solo clic aislado. Un video que provoca abandono de la app es castigado aunque tenga buen 3s hook.

### 1.2 Desacople Shorts vs Long-Form, Engaged Views (Marzo 2025) y Buckets de Duración
- **Sistemas independientes:** El motor de recomendación de Shorts está desacoplado del de Long-Form (un Short fallido no hunde tus videos largos, pero el enlace **Related Video** sí transfiere audiencia de Shorts hacia Long-Form `8+ min`).
- **Trampa de Métrica Dual (Cambio Oficial 31 de Marzo 2025 — *Views vs. Engaged Views*):**
  - **Public Views:** Desde marzo 2025, YouTube cuenta 1 vista pública desde el **primer frame reproducido o repetido** (sin umbral mínimo de segundos) para equipararse con TikTok/Reels.
  - **Engaged Views (Métrica Real de Dinero y YPP):** Mide únicamente espectadores que **deciden quedarse tras los primeros segundos** (el criterio histórico real de vista). **Los ingresos del Shorts Pool y los requisitos YPP (10M / 20M) se calculan exclusivamente sobre *Engaged Views*, NUNCA sobre Public Views infladas.** Auditar siempre *Engaged Views* en Analytics Advanced Mode y verificar qué métrica exige cada Bounty CPM.
- **Umbrales Operativos Decisivos:**
  - **Viewed vs. Swiped Away (VVSA — Compuerta #1):** `>= 70%–80% Viewed` = escala viral; `50%–60%` = nicho acotado; `< 45%` = freno inmediato.
  - **Buckets de Duración (incl. expansión hasta 3 minutos):**
    - `< 15s`: exige **`APV >= 120%–160%`** (loop obligatorio) pero monetiza menos por baja retención absoluta.
    - **`20–45s` (Zona Óptima GTA VI):** equilibrio óptimo entre **`VVSA >= 72%`**, **`APV >= 100%–115%`** y alto tiempo absoluto retenido (`25–42s`).
    - `45–60s+` (hasta 3 min): gana solo cuando sostiene narrativa multi-beat con **`AVD >= 38–50s`** aunque `APV` baje a `75%–90%`.
- **Shorts Search SEO (YouTube + Google Search):** Título `< 55 caracteres` con keyword *front-loaded*, audio hablado transcrito por ASR con la entidad exacta (*GTA 6 / streamer / misión*), texto en pantalla limpio y `3–5` hashtags relevantes (`#GTA6 #Shorts`).

### 1.3 Políticas Oficiales YPP (Actualización 15 de Julio 2025 — *Inauthentic Content*)
- **Reused Content vs. Inauthentic Content:** El 15/07/2025 YouTube renombró *Repetitious Content* a **`Inauthentic Content`**, apuntando a contenido masivo, plantillado o generado automáticamente sin aporte creativo humano. La evaluación ocurre **a nivel de CANAL completo** (revisión humana + ML):
  - Clips crudos de streamers con solo subtítulos/zooms/TTS fallan *Reused / Inauthentic Content* **incluso teniendo permiso del streamer o cero reclamos de Content ID**.
- **Arquitectura de Dos Carriles Obligatoria:**
  - **Carril B (Bounties CPM):** `voice_mode: "none"` (monetiza por vistas de campaña, sin depender de YPP).
  - **Carril A (Canal Propio YPP + Puente Long-Form `8+ min`):** Estrictamente separado; en compilaciones `8–12 min` incorporar voz editorial humana (`human_batch` SM58), narrativa temática, contexto y estructura de ensayo/ranking para superar auditoría YPP.

---

## 2. Instagram Reels — Meta Andromeda, GEM, Regla "10 en 30 Días" y Trial Reels

### 2.1 Infraestructura Meta 2025–2026 (Meta Engineering Blog & AI System Cards)
- **Andromeda (Retrieval Engine):** Motor de recuperación neuronal densa con kernels GPU/MTIA y MoE que filtra decenas de millones de candidatos a unos pocos miles en milisegundos, leyendo directamente **fotogramas, audio, entidades visuales y texto** (*"el creativo ES la segmentación"*).
- **GEM (Generative Recommendation Model) & Lattice (Ranking):** Modelo fundacional tipo LLM a escala Meta que ingesta secuencias multimodales cross-superficie (Reels, Feed, Stories) para predecir retención y satisfacción (**UTIS — *User True Interest Survey***).

### 2.2 Las 4 Señales Oficiales de Adam Mosseri (Ratios *Per Reach*)
1. **3s Skip Rate (Tasa de Salto — Compuerta Negativa #1):** Porcentaje de usuarios que abandonan en los primeros `3s`; sustituyó a métricas legacy de reproducción como filtro principal de entrada.
2. **Watch Time & Replay Rate:** Tiempo total retenido + repeticiones (el *Seamless Loop* multiplica el Watch Time agregado).
3. **Sends Per Reach (Envíos por DM — Rey del Alcance No-Conectado):** Para crecer fuera de tus seguidores, **un envío por mensaje directo pesa más que un comentario o un like** porque demuestra moneda social real (*"Send this to your duo..."*).
4. **Likes Per Reach:** Más relevante para audiencia ya conectada (seguidores).

### 2.3 Regla Crítica Anti-Agregadores ("10 No-Originales en 30 Días") y Trial Reels
- **Penalización por Originality Score (Meta 2024–2026):** Si una cuenta publica **`>= 10` piezas detectadas como contenido no original dentro de una ventana móvil de 30 días**, Instagram **expulsa la cuenta entera de TODAS las superficies de recomendación** (Reels, Explore, Feed no-conectado). Recortar mínimamente o poner crédito en texto NO basta: exige transformación visual/editorial real (split-screen compuesto con webcam + motion graphics + subtítulos propios + audio procesado + cero watermarks de TikTok/CapCut/Shorts).
- **Límite oficial de Hashtags (2025–2026):** Máximo **`5` hashtags** por publicación; Instagram Search indexa semántica de caption, audio ASR y texto OCR, no muros de 30 hashtags.
- **Trial Reels (A/B Nativo Oficial):** Expone el Reel primero a **no-seguidores (24h–72h)** sin mostrarlo a tus seguidores ni a tu grilla. **Cero penalización** por probar ganchos/ediciones distintas (`--ab-variants 2`); **penalización algorítmica severa** si re-subís el mismo MP4 idéntico en bucle.
- **Safe-Zones 2026:** Lienzo `1080×1920`, grilla de perfil recortada a **`3:4` (`1080×1440` centrado)** y feed principal `4:5`.

---

## 3. TikTok — ByteDance Monolith, Batch Testing, pHash y Search SEO Triple

### 3.1 Paper Primario: Liu et al. (2022, ByteDance — *Monolith*)
- **Tablas de Embeddings Sin Colisiones (Cuckoo HashMap):** Evita colisiones entre IDs dispersos de usuarios/videos/sonidos y aplica **expiración automática de embeddings** (tendencias muertas caducan rápido).
- **Online Training en Streaming (Kafka + Flink, sincronización en minutos):** Cada swipe en `<1s`, rewatch, share o búsqueda actualiza embeddings y ranking MMoE casi en tiempo real. Por eso el **primer lote de 30–60 minutos (`200–500` impresiones)** decide el destino inmediato del clip.

### 3.2 Curva Duración vs Retención y Umbrales del Lote Semilla
- **`< 15s`:** Exige **`Completion > 100%`** (rewatch loop).
- **`25–60s`:** El algoritmo prioriza **Watch Time absoluto (`>= 18–25s` promedio)** + **`3s Hold >= 70%`** + **`Share Rate >= 1%`** + **`Save Rate >= 2%`**.
- **Batch Scaling:** `200–500` (prueba fría independiente de seguidores) -> `1K–5K` -> `10K–50K` -> viral masivo.

### 3.3 Elegibilidad FYF: Perceptual Hashing (pHash), Audio ID y Search SEO Cuádruple
- **Moderación Pre-FYF (Unoriginal Content):** TikTok aplica **Perceptual Hashing visual (pHash)** + **Audio Fingerprinting** + **OCR/ASR** antes de liberar el clip al FYF. Un simple espejo horizontal, leve zoom o re-upload con watermark dispara *"Ineligible for the For You Feed"* (mata alcance orgánico y pagos).
- **Riesgo Cross-Platform:** Subir primero a YouTube Shorts varias horas antes puede causar falsos positivos de no-originalidad en TikTok; publicar en ventanas cercanas y con master limpio transformativo reduce fricción.
- **Search SEO Cuádruple (2025–2026):** TikTok indexa (1) **Audio hablado (ASR)**, (2) **Texto en pantalla `0–3s` (OCR)**, (3) **Caption natural**, y (4) **Palabras clave en Comentarios** (que generan enlaces azules de búsqueda). **Moderación OCR estricta:** cualquier grosería visible en subtítulos en `0–7.5s` suprime el FYF -> bleeper + censura textual obligatoria.
- **Reciclaje Seguro A/B:** Nunca re-subir un MP4 muerto en 200 vistas sin cambiar los primeros `0–3s` (nuevo corte/orden, nuevo motion banner visual, re-encode con nuevo hash perceptual).
- **Safe-Zones 2026:** Grilla de perfil **`1:1` (`1080×1080`, `y = 420..1500`)**; en `Content Posting API` orgánica usar `post_info.video_cover_timestamp_ms`.

---

## 4. Facebook Reels — Meta AI Unified Ranking y Originalidad

- **Motor Unificado:** Meta consolidó el video nativo bajo Reels (`Andromeda` + `GEM`), premiando **Completion Rate**, **Shares**, **Rewatches** y **debates genuinos** en comentarios.
- **Facebook Content Monetization Program:** Unificó *Ads on Reels*, *In-Stream Ads* y *Performance Bonus*. Exige originalidad verificada (cero reposts vacíos, cero *engagement bait*, cero watermarks).
- **Safe-Zones & Covers:** Márgenes libres **`top 14%`**, **`bottom 35%`**, **`lados 6%`**; portada custom vía **Meta Business Suite**.

---

## 5. X (Twitter) — Código Fuente Real `xai-org/x-algorithm` (Actualización Oct–Nov 2026)

> **Evidencia `[RUNTIME REAL]` verificada directamente en `_references/x-algorithm/` (`xai-value-model/scoring.rs`, `home-mixer/params/param.rs`, `home-mixer/scorers/author_cold_start.rs`, `home-mixer/scorers/immersive_video_scorer.rs`).** Reemplaza por completo las cifras obsoletas de 2023 (`twitter/the-algorithm`).

### 5.1 Arquitectura Phoenix (Rust `home-mixer` + JAX/Python Grok Transformer)
- **Retrieval (Oct 13, 2026):** `recsys_two_tower_model.py` entrenado explícitamente para recuperar posts que los usuarios **leen o consumen hasta el final**.
- **Ranking (Oct 8, 2026):** `recsys_model.py` + `xai-value-model/scoring.rs` predice **30 términos de acción y tiempo continuo**, soportando modo aditivo o multiplicativo:
  - $\text{Score} = \text{skip\_score}(P_{\text{not\_dwelled}}) \times (\sum w_i P_i + b_{\text{engage}}) \times (1 + w_{\text{dwell}} \cdot \text{dwell\_time})$
  - Luego aplica `PostFusionMultiplier = author_diversity * oon`.
- **Aclaración Crítica del Código (`scoring.rs`):** Los pesos multiplican la **probabilidad predicha personalizada** $P(\text{acción})$ de cada usuario, NO conteos brutos. Además, **las interacciones solo cuentan si ocurren en un post servido en el Home Timeline**: navegar por URL directa desde grupos/bots (*engagement pods*) tiene **impacto CERO** en el ranking.

### 5.2 Pesos Exactos de Producción (`home-mixer/params/param.rs`, Nov 2026)

| Parámetro en `param.rs` | Peso Exacto | Implicación Estratégica Directa |
| :--- | :---: | :--- |
| **`ShareViaCopyLinkWeight`** | **`+20.0`** | **Señal positiva #1 en X (`28.2x` un Like):** Diseñar clips que la gente copie por link hacia WhatsApp / Discord / iMessage. |
| **`ReplyWeight` + `BidirectionalFollowReplyWeightBoost`** | **`+5.0` (+`15.0` = `+20.0`)** | Respuestas reales valen `7x` un Like; si hay seguimiento mutuo (*bidirectional follow*), escalan a `+20.0`. |
| **`ShareViaDmWeight`** | **`+5.0`** | Envíos por DM dentro de X (equivalente directo a *Sends Per Reach* de Instagram). |
| **`QuoteWeight`** | **`+5.0`** | Citas con opinión propia: formular el copy de X para invitar *Quote-Tweets* con reacción propia. |
| **`FollowAuthorWeight`** | **`+4.0`** | Consistencia de nicho en el perfil para convertir espectadores en seguidores. |
| **`ShareWeight`** | **`+2.118`** | Compartido general. |
| **`RetweetWeight`** | **`+1.0`** | Amplificación base. |
| **`FavoriteWeight` (Like)** | **`+0.708`** | Señal positiva débil (no optimizar solo para Likes). |
| **`ContClickDwellTimeWeight` / `ClickWeight` / `OpenLinkWeight`** | **`+0.385` / `+0.274` / `+0.200`** | Tiempo de permanencia tras abrir el post/video. |
| **`ReportWeight` / `MuteAuthorWeight` / `NotInterestedWeight` / `BlockAuthorWeight`** | **`-234.0` / `-58.8` / `-53.07` / `-31.2`** | Penalizaciones catastróficas contra rage-bait tóxico, clickbait engañoso o spam. |

### 5.3 Mecánicas Ocultas Descubiertas en el Código Fuente (Ventaja Competitiva Real)
1. **Exploración Cold-Start para Cuentas Nuevas (`author_cold_start.rs`):**
   - Aplica a cuentas con **`< 50,000` seguidores (`ColdStartFollowerCap = 50000`)**.
   - Inyecta posts nuevos con **`< 200` impresiones (`ColdStartImpressionThreshold = 200`)** en los slots `15–16` del feed mediante **Thompson Sampling** (`Beta(α=0.75, β=49.25)`), **SOLO durante las primeras `2 horas` de vida del post (`ColdStartMaxPostAgeSecs = 7200`)**.
   - **Acción:** Publicar en horarios pico US/EU (EST tarde/noche) para que esas 2 horas de Thompson Sampling reciban máxima velocidad de *Copy Link*, *DM Share*, *Quote* y *Reply*.
2. **Umbral Mínimo de Video y Deduplicación por Embeddings (`param.rs`):**
   - **`MinVideoDurationMs = 10_000` (`10s`):** Videos menores a 10 segundos quedan **descalificados** para el peso `VQV` (*Video Quality View*). Todo clip de X debe durar **`>= 12s`** (óptimo `20–45s`).
   - **`EnableVideoCarouselEmbeddingDedupe = true` (`threshold = 0.72`, `v8_search_1`) + `VideoCarouselSemanticIdDedupeLevels = 3`:** Si varios clippers suben el mismo clip crudo de un stream, X **deduplica por similitud coseno de embedding (`>= 0.72`)** y elimina las copias del carrusel de video. Solo sobrevive quien publica **primero (`<2h`)** y con **transformación visual real** (split-screen + motion-graphics + encuadre propio).
   - **`EnableVideoCarouselAuthorDedupe = true` + `VideoCarouselFatigueMinutes = 60` + `author_diversity_decay`:** Nunca publicar 2 clips seguidos en la misma hora en X (te autocanibalizás el alcance). Espaciar **`>= 3 horas`** entre posts.
   - **`EnableVideoCarouselHighRiskFilter = true`:** Cero NSFW/gore/groserías gráficas si querés entrar al Video Carousel.
   - **Links externos:** Mantener siempre **`link-in-reply`** para derivar tráfico a YouTube Long-Form sin penalizar el post nativo de video.

---

## 6. Ciencia Cognitiva, Libros Canónicos y Masterclasses de Edición/Retención

### 6.1 Ciencia Cognitiva del Hook y la Atención (Papers y Libros Faltantes Integrados)
1. **George Loewenstein (1994, *Psychological Bulletin* — *The Psychology of Curiosity: Information-Gap Theory*):**
   - La curiosidad es un estado de privación cognitiva producido al percibir una **brecha explícita entre lo que sabemos y lo que queremos saber**.
   - **Regla práctica:** Un hook sin contexto ("Mirá esto") NO crea brecha porque el cerebro necesita una **base mínima de conocimiento** (qué está en juego) para sentir el vacío. Primero mostrar el riesgo/objetivo en `0–2s`, luego retener la resolución hasta el último `15%–20%`.
2. **Chip & Dan Heath — *Made to Stick* (Framework SUCCESs):**
   - **Simple** (1 sola idea/conflicto por clip), **Unexpected** (violación de expectativa en `0–3s` para detener el scroll), **Concrete** (hecho visual tangible: una apuesta, una explosión, un contador, nunca abstracciones), **Credible** (reacción humana real sin guion falso), **Emotional** (alta activación: asombro, risa, incredulidad -> dispara *Shares/Copy Link*), **Stories** (micro-arco completo).
3. **Daniel Kahneman — *Thinking, Fast and Slow* (Sistema 1 vs. Sistema 2 y *Cognitive Ease*):**
   - En scroll rápido opera **Sistema 1** (automático, visual, sin esfuerzo). Si el espectador necesita activar **Sistema 2** para descifrar un subtítulo largo, un encuadre confuso o una jerga privada del chat, **desliza en `<1.5s`**. Por eso exigimos nivel de lectura de 5.º grado, `<= 3` palabras por golpe, contraste `APCA >= 75` y máximo `2–3` anclas visuales.
4. **Jonah Berger — *Contagious* (STEPPS):** *Social Currency*, *Triggers*, *High-Arousal Emotion* (risa, shock, asombro -> combustible de *Sends Per Reach* en IG y *ShareViaCopyLink* en X).
5. **Nir Eyal (*Hooked*) & Celia Hodent (*The Gamer's Brain*):** Carga cognitiva mínima + recompensa variable en el cierre.

### 6.2 Narrativa de 30 Segundos (Micro-Story Arcs) y Masterclasses de Edición
1. **Micro-Arco Narrativo en `20–45s` (Blake Snyder *Save the Cat* / Dan Harmon *Story Circle* comprimido):**
   - **Beat 1 — Hook / Inciting Incident (`0–3s`):** En medio de la acción o afirmación arrogante con apuesta clara (abre *Information Gap*).
   - **Beat 2 — Escalada / Complicación (`3–20s`):** La situación empeora o suma un giro intermedio (*re-hook* cada `5–8s`, sin aire muerto ni explicaciones sobrantes).
   - **Beat 3 — Clímax / Reversal (`20–32s`):** Ruptura inesperada + micro-pausa *Synchresis* (`0.25s`) + reacción pico del streamer (`punch-in 1.12x` + `motion_overlay` fluido + SFX `-16 dB`).
   - **Beat 4 — Corte de Remate / Seamless Loop (`últimos 0.5–1.5s`):** Cortar exactamente al aterrizar la reacción pico (cero despedida, cero mirar el chat).
2. **Hayden Hillier-Smith (*The Editing Podcast* — Editor jefe de MrBeast / Logan Paul):**
   - **Pacing ≠ Velocidad Constante:** Cortar cada `1.5s` por costumbre genera fatiga y apariencia *AI slop*. El ritmo real es **contraste emocional (tensión y liberación)**: dejar respirar `3–5s` una tensión real para que el corte/punch-in en el clímax golpee 3x más fuerte.
   - **Motivated Cutting (*Kill Your Darlings*):** Cada corte, zoom o gráfico DEBE responder una pregunta o escalar la brecha de información; si no sirve al arco emocional, se elimina.
   - **Jamás destruir una reacción genuina de gaming con sobre-edición:** En clips de streamers, el rostro y la voz auténtica son el activo principal; el montaje amplifica la emoción sin taparla.
3. **Paddy Galloway (CCN Framework):** Satisfacer en simultáneo a **Core** (lore GTA VI), **Casual** (contexto visual en `1–2s`) y **New** (acción comprensible sin conocer al streamer). En portadas: `<= 3` elementos y **Curiosity Gap complementario** (nunca duplicar palabras del título).
4. **Jenny Hoyos (Fórmula Shorts):** Shock visual en Frame 1 -> stakes en `<3s` -> corte quirúrgico en la resolución (evita caída de `20%–25%` del último segundo) -> facilidad cognitiva de 5.º grado.
5. **Kallaway (Triple Hook):** Alineación sin fricción entre **Visual Hook** (entendible en mute) + **Audio Hook** (*Context Lean*) + **Text Hook** (*Pattern Interrupt*).
6. **Derral Eves (*The YouTube Formula*) & MrBeast Production Memo:** El algoritmo sigue al comportamiento humano (`CTR × Retención`). En Long-Form `8+ min` (puente YPP), ganar el **minuto 0–1** y sostener la promesa en **minutos 1–3** con estructura radicalmente simple.

---

## 7. Diagnóstico de Curvas de Retención, Cadencia Multi-Red y Líneas Rojas

### 7.1 Tabla de Diagnóstico Post-Publicación (Para `retention-critic` y `analytics.py`)

| Patrón de Caída en Analytics | Diagnóstico Exacto | Corrección Inmediata en el Siguiente Lote |
| :--- | :--- | :--- |
| **Caída brusca `0–3s` (`VVSA < 60%` / `Skip Rate` alto)** | Falló el Triple Hook (arranque lento, saludo, confusión visual en mute o pronombres huérfanos). | Iniciar `1–3s` más adelante directo en acción/tensión; mejorar *motion banner* `0–2.5s` y encuadre facial inmediato. |
| **Caída `3–10s` (tras buen arranque)** | Promesa rota o pérdida de contexto (*Information Gap* mal calibrado / demasiada explicación lenta). | Recortar contexto excedente (`auto-editor`), asegurar que el conflicto escale antes del segundo `6`. |
| **Pendiente descendente constante `10–25s`** | Falta de *re-hook* o monotonía rítmica (o sobre-edición artificial sin respiración). | Inyectar contraste de ritmo (Hillier-Smith), acortar el clip `5–10s` hacia `24–35s` si no tiene segundo giro real. |
| **Caída en los últimos `2–3s` (`APV < 95%`)** | Cola muerta / transición a chat / resolución demasiado temprana (efecto Jenny Hoyos `-20%`). | Cortar `1.5–2.5s` antes, exactamente en el clímax de reacción para forzar *Seamless Loop*. |
| **Buenas vistas en Shorts, 0 en TikTok (`200–300`)** | Supresión FYF por OCR (grosería `0–7.5s`), pHash duplicado o bajo Share/Save rate. | Auditar censura OCR/audio `0–7.5s`; publicar Variante B transformada (`0–3s` nuevo + re-encode) o enfocar en momento más compartible. |

### 7.2 Cadencia Óptima y Líneas Rojas Anti-Ban
1. **Espaciado mínimo `>= 3 horas` entre publicaciones por red:** Evita autocanibalización por `author_diversity_decay` y `VideoCarouselFatigueMinutes = 60` en X, y superposición de lotes semilla en TikTok/Reels.
2. **Velocidad de publicación (`< 2–6h` desde el momento del stream):** Captura `Example Age` en YouTube, `ColdStartMaxPostAgeSecs = 7200` en X y gana la carrera contra `VideoCarouselEmbeddingDedupe (0.72)` / `Originality Score` de Meta / `pHash` de TikTok.
3. **CERO bots de comentarios / engagement pods / proxies / Multilogin:** Comprobado en el código fuente de X (`scoring.rs`): interacciones vía URL directa tienen **0 impacto en ranking**, y disparan `botmaker`/`scarecrow` y baneos en TikTok/Meta/Whop.
4. **CERO re-subidas idénticas / CERO watermarks cruzados / Separación estricta Carril A (YPP) vs. Carril B (Bounties CPM).**
