# Biblia Oficial de Algoritmos de Video y Ciencia de Viralidad (5 Plataformas, 2025–2026)

> **Propósito:** Base de conocimiento canónica e indexable en `runs/studio.db` (`knowledge_chunks` / `knowledge_chunks_fts`) para el equipo agéntico de **Clipping Studio**. Combina documentación oficial de plataformas, papers de ingeniería de sistemas de recomendación (RecSys), libros de estrategia digital y masterclasses de retención, articulada con el corpus clásico ya existente en `Omni-RAG v6` y `Qdrant` (Murch, Bruce Block, Chion, Berger, Eyal, Cialdini, Hodent).

---

## 1. YouTube Shorts — Arquitectura Oficial, MMoE, Satisfacción y Políticas YPP

### 1.1 Paradigma Pull y Embudo Neuronal en 2 Etapas (Google / YouTube RecSys)
- **Sistema Pull (centrado en el espectador):** YouTube no "empuja" un video a un ranking global; cuando un usuario abre el feed de Shorts, el motor *extrae* candidatos para ese individuo según historial, co-visualizaciones (*co-watching*), dispositivo, hora y contexto.
- **Etapa 1 — Candidate Generation (Two-Tower / Co-watching Retrieval):** Reduce miles de millones de videos a unos pocos cientos de candidatos relevantes por afinidad de co-visualización, búsquedas e historial.
- **Etapa 2 — Multitask Ranking con MMoE (Multi-gate Mixture of Experts):** Arquitectura neuronal con sub-redes expertas compartidas y compuertas por objetivo que separa explícitamente dos pilares en tensión:
  1. **Objetivos de Engagement:** probabilidad de ver sin deslizar (*Viewed vs. Swiped Away*), porcentaje visto (*APV*) y repeticiones (*loops*).
  2. **Objetivos de Satisfacción (*Viewer Satisfaction*):** likes, compartidos, suscripciones, visitas al canal y **encuestas directas post-visualización** (*"¿Fue satisfactorio este video?"*, 1–5 estrellas). Las encuestas entrenan modelos supervisados que predicen satisfacción a escala de toda la plataforma (confirmado por Todd Beaupré, *Creator Insider*).

### 1.2 Métricas Decisivas y Umbrales Operativos (Shorts Feed + Search)
- **Viewed vs. Swiped Away (VVSA) — La compuerta #1:**
  - **`>= 70%–80% Viewed` (`<= 20%–30% Swiped Away`):** Zona viral; el algoritmo escala agresivamente a lotes mayores.
  - **`50%–60% Viewed`:** Rendimiento promedio; distribución acotada a nicho estrecho.
  - **`< 40%–50% Viewed`:** Freno casi inmediato de distribución.
- **Average Percentage Viewed (APV) y Loop Velocity:**
  - En Shorts exitosos de 18–45s el objetivo es **`APV >= 100%–130%`** (el usuario completa el video y mira el arranque del segundo ciclo).
  - **Regla de Seamless Loop:** Eliminar colas muertas al cierre y cortar justo en la resolución/clímax o enlazar la última frase con el segundo `0.0`.
- **Engaged Views (actualización oficial 2025):** YouTube separa impresiones crudas de visualizaciones con intención real (retención tras zona de fricción + satisfacción de sesión).
- **Shorts Search SEO (YouTube Search + Google Search):**
  - Título `< 55 caracteres` con palabra clave principal al inicio (*front-loaded*), sin clickbait vacío.
  - Transcripción automática (ASR) del audio hablado + texto visible en pantalla + descripción natural con 2–3 palabras clave + 3–5 hashtags limpios (`#GTA6 #Shorts`).

### 1.3 Política Oficial de Reused Content vs. Inauthentic Content (Crítico para YPP)
- **Evaluación a nivel de CANAL (revisión humana + ML):**
  - **Reused Content:** Clips de terceros (streams, gameplay ajeno, compilaciones) sin transformación narrativa/educativa sustancial son rechazados de YPP **incluso si tenés permiso del streamer o cero reclamos de Copyright/Content ID**. Subtítulos genéricos, zooms o TTS automático por sí solos NO califican como transformación suficiente.
  - **Inauthentic Content (antes *Repetitious Content*):** Penaliza plantillas masivas y automatizadas repetitivas.
- **Regla de Oro Arquitectónica para Clipping Studio:**
  - **Canales de Bounties CPM (Whop / Clipping.net / Vyro):** Operan con `voice_mode: "none"` (reacción original del streamer) porque monetizan por vistas verificadas de campaña, no por AdSense YPP.
  - **Canal Propio YPP (Puente a YouTube Long-Form 8+ min):** Debe mantenerse **estrictamente separado** y usar comentario editorial humano (`human_batch` con Shure SM58) + guion analítico/curatorial donde la voz y narrativa propia sean el centro del valor.

---

## 2. Instagram Reels — Meta Transparency Center, Mosseri y Trial Reels

### 2.1 Las 3 Señales Oficiales de Ranking (Adam Mosseri / Meta Transparency)
Instagram evalúa las señales como **ratios por alcance (*per reach*)**, no por números brutos:
1. **Watch Time & Completion / Rewatch Rate (Factor #1):** Tiempo total retenido, retención tras los primeros `3s` y tasa de repetición.
2. **Sends Per Reach (Envíos por Mensaje Directo — Rey del Alcance a No-Seguidores):** Para distribución *unconnected* (personas que no te siguen), **un envío por DM pesa más que un comentario o un like** porque valida moneda social real.
3. **Likes Per Reach:** Más relevante para audiencia conectada (seguidores).
4. **UTIS (User True Interest Survey):** Encuestas directas de Meta para calibrar satisfacción declarada frente a clicks vacíos.

### 2.2 Recuperación Profunda (Andromeda) y Originality Score
- **El creativo ES la segmentación:** Los motores de recuperación profunda de Meta (como *Andromeda*) analizan directamente los fotogramas, audio, entidades semánticas y texto del video para emparejarlo con intereses de usuarios sin depender solo de hashtags.
- **Originality Score:** Meta reemplaza automáticamente en recomendaciones a cuentas agregadoras por el creador original y **descalifica del alcance no conectado** a videos con marcas de agua de otras redes (TikTok/CapCut/Shorts) o reposts sin transformación.

### 2.3 Trial Reels (A/B Testing Nativo Oficial) y Penalizaciones
- **Trial Reels:** Permite publicar un Reel **exclusivamente a no-seguidores durante 72 horas**, oculto de tu grilla y seguidores. Si gana tracción, se promueve al feed principal; si falla, no daña la salud de tu cuenta.
- **Regla anti-spam de Mosseri:** Experimentar con distintos *hooks* o ediciones en Trial Reels tiene penalización cero; **re-subir el mismo MP4 idéntico en bucle sí activa penalización algorítmica**.
- **Engagement Bait degradado:** Pedidos huecos (*"Comentá SÍ para..."*) rinden cada vez menos frente a gatillos genuinos de envío por DM (*"Send this to your duo..."*).
- **Safe-Zones 2026:** Lienzo `1080×1920`, grilla de perfil recortada a **`3:4` (`1080×1440` centrado)** y feed principal **`4:5`**.

---

## 3. TikTok — ByteDance Monolith, Batch Testing, OCR/ASR y Elegibilidad FYF

### 3.1 Motor en Tiempo Real: ByteDance Monolith
A diferencia de sistemas entrenados por lotes nocturnos, TikTok opera sobre **Monolith**:
- **Collisionless Embedding Tables (Cuckoo HashMap):** Evita colisiones de hash entre millones de IDs dinámicos de usuarios, videos y audios, con expiración automática de embeddings fríos.
- **Online Training en Streaming (Kafka + Flink):** Las señales de un usuario (swipe en `<1s`, retención completa, rewatch, share) actualizan sus embeddings de afinidad y el ranking fino (**MMoE**) en **segundos**, absorbiendo *concept drift* instantáneamente.

### 3.2 Batch Testing Escalonado ("200-View Jail") y Pesos de Señal
1. **Lote Semilla (`200–500` impresiones en 30–60 min):** Independiente del número de seguidores; evalúa **3s Hold**, **Completion Rate** (`>100%` en clips `<15s`), **Rewatch Rate**, **Shares**, **Saves** y **calidad conversacional de comentarios**.
2. **Escalamiento exponencial:** Si supera el percentil del nicho -> `1K–5K` -> `10K+` -> viral. Si se frena en `200–300` vistas, falló el gancho inicial o disparó un filtro de elegibilidad.

### 3.3 El Sistema de Doble Filo: OCR + ASR + Perceptual Hashing
TikTok escanea cada video subido con 3 motores automáticos antes de liberarlo al *For You Feed (FYF)*:
- **OCR (Reconocimiento Óptico de Caracteres):** Lee subtítulos quemados, banners `0–3s` y logos. Sirve tanto para **TikTok Search SEO** (indexar palabras clave buscables) como para **moderación automática** (groserías, marcas de agua de competidores o palabras sensibles en subtítulos provocan *FYP suppression* -> valida nuestro bleeper y censura textual `0–7.5s`).
- **ASR (Speech-to-Text):** Transcribe la voz hablada para SEO y para detectar scripts duplicados masivos.
- **Perceptual Hashing (Audio + Video):** Detecta re-subidas idénticas o clips no originales.
- **Regla de Reciclaje Seguro A/B:** Si una Variante A muere en 200 vistas, **nunca** re-subir el mismo archivo; la Variante B debe alterar los primeros `0–3s` (nuevo recorte/orden *Hook-First* + nuevo banner visual + re-encode) para generar nueva huella perceptual.
- **Safe-Zones 2026:** Lienzo `1080×1920`, grilla recortada al centro (**cuadrado seguro `1080×1080`**, `y = 420..1500`). En `Content Posting API` orgánica no se suben JPG externos: usar `post_info.video_cover_timestamp_ms`.

---

## 4. Facebook Reels — Meta AI Ranking, Originalidad y Monetización Unificada

- **Motor Unificado:** Meta consolidó el video nativo bajo el ecosistema Reels con ranking predictivo orientado a **Completion Rate**, **Shares**, **Rewatches** y **conversaciones reales** en comentarios.
- **Originality Guidelines (Pilar de Monetización):** Premia ediciones con valor transformativo/reacción clara y desmonetiza de inmediato reposts directos, loops vacíos, *engagement bait* o infracciones de copyright. El uso asistido de herramientas de edición IA está permitido si aporta valor original; contenido generativo sintético recibe etiqueta automática *"AI Info"*.
- **Facebook Content Monetization Program:** Unificó *Ads on Reels*, *In-Stream Ads* y *Performance Bonus* en un único programa basado en vistas y engagement genuino.
- **Safe-Zones & Covers:** Lienzo `1080×1920`, mantener márgenes libres **`top 14%`**, **`bottom 35%`** y **`lados 6%`**. Subida directa de portada custom en **Meta Business Suite** (*Cover -> Upload Image*).

---

## 5. X (Twitter) — Código Abierto `xai-org/x-algorithm` (Phoenix + SimClusters + TwHIN)

### 5.1 Arquitectura Verificada en GitHub (`xai-org/x-algorithm`)
- **Candidate Sourcing:** **SimClusters** (~145.000 comunidades de interés por grafo de seguimiento para alcance *out-of-network*) + **TwHIN** (embeddings de grafo heterogéneo usuario-autor-entidad).
- **Heavy Ranker — Phoenix (Rust + Python Transformer):** Reemplazó al antiguo MaskNet; predice probabilidades de ~20 acciones discretas, incluyendo **dwell time** y **video watch-through (`50%`, `100%`, replay)**.

### 5.2 Jerarquía Real de Pesos (Extraída del Código Abierto)
| Señal en X | Peso Relativo Aprox. | Implicación Operativa Directa |
| :--- | :---: | :--- |
| **Reply + Respuesta del Autor** | **`~75.0` (~150x un Like)** | El mayor multiplicador orgánico de la plataforma: responder manualmente a los primeros comentarios reales dispara el alcance. |
| **Reply estándar** | **`~13.5` (~27x un Like)** | Redactar posts nativos con postura clara o pregunta natural que invite respuesta. |
| **Video Watch-Through (`>=50%` / `100%`) + Dwell** | **Alto (multiplicador Phoenix)** | Subir siempre el MP4 **de forma nativa** (nunca pegar un link de YouTube en el post principal). |
| **Repost / Retweet** | **`1.0`** | Amplificación secundaria. |
| **Like / Favorite** | **`0.5`** | La señal positiva más débil. |
| **Link externo en post principal** | **Penalización `-30%` a `-50%+`** | Prohibido link en el post principal; usar siempre estrategia **`link-in-reply`** para derivar tráfico a YouTube Long-Form. |
| **Report / Block / Mute / Show Less Often** | **Penalización masiva negativa** | Prohibido *rage bait* tóxico o spam que induzca bloqueos/reportes. |

---

## 6. Libros, Frameworks y Masterclasses de Referencia (Síntesis Ejecutable)

### 6.1 *The YouTube Formula* — Derral Eves
- El algoritmo no "favorece" ni "castiga": **sigue a la audiencia**. Toda decisión se optimiza en el binomio **CTR (empaquetado)** × **Retención (cumplimiento de promesa)**.
- Iterar sobre gráficos de caída de retención (detectar en qué segundo exacto cae la curva y eliminar esa fricción en futuros lotes) y convertir espectadores casuales en comunidad (*fandom*).

### 6.2 *Hook Point: How to Stand Out in a 3-Second World* — Brendan Kane
- En un feed saturado tenés **3 segundos** para detener el scroll; si el *Hook Point* es genérico, el resto del video no existe.
- Los ganchos ganadores son **hiper-específicos, contraintuitivos o visualmente anómalos**. Aplicar modelado estructural: identificar formatos virales probados (`/outliers` + `/steal`) y adaptarlos al nicho en iteraciones rápidas.

### 6.3 Framework CCN y Principios de Packaging — Paddy Galloway
- **Audiencia CCN (Core, Casual, New):** Un clip viral masivo satisface simultáneamente a:
  - **Core** (fans del streamer/GTA VI -> lore, detalle interno),
  - **Casual** (gamers -> contexto rápido en 1–2s),
  - **New** (desconocidos del feed -> acción visual comprensible sin conocer al streamer!).
- **Regla de los 2–3 Elementos y Flujo Izquierda->Derecha:** En portadas/miniaturas nunca superar 3 puntos focales; **nunca repetir en la imagen lo mismo que dice el título** — ambos deben funcionar como piezas complementarias que abren un **Curiosity Gap**.
- **Inversión del Tiempo:** Dedicar máxima energía a la **selección de idea/momento + packaging** antes que sobre-editar un clip débil.

### 6.4 Fórmula de Retención en Shorts — Jenny Hoyos
- **Loop de 3 pasos:** (1) **Shock visual en Frame 1** (cero saludo/relleno), (2) **Establecer expectativas/riesgo en `<3s`** (*foreshadowing* claro), (3) **Cortar exactamente al resolverse la tensión** (ella demostró que un mal último segundo provoca caídas de **20%–25%** en retención; cortar justo en el clímax induce *rewatch loop*).
- **Facilidad Cognitiva (Nivel de lectura de 5.º grado):** Textos en pantalla y subtítulos ultra-simples, escaneables en milisegundos sin esfuerzo mental.

### 6.5 Fórmula del Triple Hook — Kallaway
- Todo inicio `0–3s` debe alinear **3 canales sin disonancia cognitiva**:
  1. **Audio Hook (*Context Lean*):** primera frase que aclara el tema y por qué importa.
  2. **Visual Hook:** movimiento/imagen entendible **incluso en silencio (mute)**.
  3. **Text Hook (*Pattern Interrupt*):** texto breve en pantalla que refuerza o introduce un giro (*"But..."*, *"Until..."*) abriendo un *open loop*.

### 6.6 Memo Interno de Producción — MrBeast (*"How to Succeed in MrBeast Production"*)
- Obsesión quirúrgica por **CTR, AVD y AVP**.
- En Long-Form 16:9 (puente YPP): el **primer minuto** y la ventana **Minuto 1–3** son el campo de batalla decisivo; incluso en caos extremo la estructura narrativa debe mantenerse **radicalmente simple** para evitar confusión y abandono.

### 6.7 Puente con Libros Clásicos Ya Ingestados en `Omni-RAG v6` / `Qdrant` (Cero Duplicación)
- **Contagious (Jonah Berger — STEPPS):** *Social Currency*, *Triggers*, *High-Arousal Emotion* (asombro/risa/sorpresa -> impulsa *Sends Per Reach* en IG y *Shares* en TikTok).
- **In the Blink of an Eye (Walter Murch — Rule of Six):** Emoción (51%) > Historia (23%) > Ritmo (10%) > Trazado Ocular (7%) > Plano 2D (5%) > Espacio 3D (4%).
- **La Audiovisión (Michel Chion):** *Synchresis* (pausa `0.20s–0.35s` antes del remate + sincronía transiente SFX).
- **The Gamer's Brain (Celia Hodent) & Hooked (Nir Eyal):** Carga cognitiva mínima y recompensa variable en el último 15–20% del clip.

---

## 7. Líneas Rojas Anti-Ban y Protocolo Operativo del Estudio

1. **CERO bots de comentarios / cuentas secundarias con Playwright + Multilogin/proxies:** Riesgo crítico de shadowban/baneo permanente y pérdida de pagos en bounties CPM. En X/TikTok se generan **borradores sugeridos (`author_reply_seed`)** para que el operador responda orgánicamente.
2. **CERO re-subidas idénticas:** Si un clip fracasa en el primer lote (`<300` vistas), solo se recicla como **Variante B transformada** (nuevo recorte/banner `0–3s` + nuevo hash perceptual) o mediante **Instagram Trial Reels**.
3. **CERO marcas de agua cruzadas:** Exportar siempre desde el master limpio `1080×1920` de Clipping Studio hacia las 5 redes.
4. **CERO *Rage Bait* denunciable o *Engagement Bait* hueco:** Priorizar **debate benigno** y **gatillos de envío por DM** (*Sends Per Reach*).
5. **Separación estricta de canales:** Canales de Bounties CPM (`voice_mode: "none"`) separados del Canal Propio YPP (`human_batch` con Shure SM58 + narrativa analítica 8–12 min).
