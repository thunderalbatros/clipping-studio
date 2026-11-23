# Short-Form Craft, Editing & Media Engineering Bible (v3.1)

> Destilado indexable en `runs/studio.db` (`rag_chunks` + `rag_chunks_fts` + `rag_chunks_vec`) que cierra las Brechas 1, 3 y 4 del estudio articulando `Omni-RAG v6` (*Walter Murch*, *Bruce Block*, *Michel Chion*), ciencia algorítmica 5-redes 2026 y recetario determinista open-source en Windows 10.

---

## 1. Narrativa y Montaje Cinematográfico Aplicado a 9:16 (Murch + Bruce Block + Chion)

### 1.1 Rule of Six (Walter Murch — *In the Blink of an Eye*)
Orden jerárquico obligatorio al decidir cada corte en un clip gaming:
1. **Emoción (51%):** Nunca cortar en medio de una expresión genuina de shock, risa o furia por obedecer un temporizador.
2. **Historia (23%):** Cada corte avanza el arco *Setup -> Escalada -> Remate*. Si un tramo no cambia el estado emocional ni la información, se recorta.
3. **Ritmo (10%):** Alternar ráfagas rápidas con micro-respiros ("acordeón emocional"), evitando cortes robóticos cada 1.8s.
4. **Trazado Ocular / Eye-Trace (7%):** Mantener el punto de atención en el eje central (costura webcam/gameplay) para evitar fatiga ocular en pantallas verticales.
5. **Plano 2D (5%) y Espacio 3D (4%):** Evitar *jump-cuts* que salten la posición de la webcam o desorienten al espectador en combates/persecuciones de GTA VI.

### 1.2 Contraste y Afinidad Visual (Bruce Block — *The Visual Story* + Hayden Hillier-Smith)
- **Ley de Intensidad Visual:** Mayor contraste visual/sonoro = mayor intensidad emocional; mayor afinidad = calma/tensión contenida.
- **Aplicación en `9:16` (Cero Freeze-Frames Antifuncionales — Regla 05):** Construir contraste abrupto entre el *setup* estable (encuadre limpio sin zoom, audio controlado) y el *payoff* (punch-in `1.10x–1.12x` cut-guarded con `PySceneDetect` sincronizado a pico RMS, micro-SFX `whoosh`/`impact_sub` a `-16/-18 dB` y máximo 1 `motion_overlay` RGBA fluido `1.5s–2.5s` en escala editorial sin congelar el video).
- **Micro-Arco Narrativo en `20–45s`:** Beat 1 Hook/Stakes (`0–3s`, *Information Gap* de Loewenstein) -> Beat 2 Escalada (`3–20s`, *re-hooks* cada `5–8s`) -> Beat 3 Clímax/Reversal (`20–32s`, pausa *Synchresis* `0.25s` + reacción pico) -> Beat 4 Corte de Remate (*Seamless Loop*, sin cola muerta).

### 1.3 Synchresis (Michel Chion) y Preservación de Silencios Dramáticos
- **Pausa *Synchresis* (`0.20s–0.35s`, estándar `0.25s`):** Un micro-silencio justo antes de un choque, explosión o remate multiplica el impacto percibido.
- **Regla anti-mutilación en `auto-editor`:** Configurar siempre margen de respiración `--margin 0.25s` y umbral conservador de silencio en gaming (nunca recortar tramos donde hay motores, disparos, sirenas o tensión facial muda; solo eliminar aire muerto sostenido `> 0.95s` por debajo de `-30 dBFS`).

### 1.4 Seamless Loop y Cierre Sin Cola Muerta
- Cortar el final del clip exactamente al aterrizar la reacción pico o enlazar la última cadencia con el segundo `0.0` para empujar **`APV >= 100%–130%`**.
- Cero segundos muertos de despedida, mirar el chat sin hablar o transición a pantalla de carga.

---

## 2. Operacionalización Algorítmica 5-Redes 2026 (Reglas de Pipeline)

| Red | Señales Críticas | Implementación en Pipeline | Prohibiciones / Penalizaciones |
| :--- | :--- | :--- | :--- |
| **YouTube Shorts** | `VVSA >= 70–80%`, `APV >= 100–130%`, **Engaged Views** (marzo 2025, base YPP), encuestas satisfacción, `Example Age` (`<2–6h`) | Frame 0 directo en acción (cero placa estática), *Seamless Loop*, título `<55 chars` front-loaded + 3–5 hashtags, `cover_9_16.jpg` en Studio Desktop / `thumbnails.set` | Watermarks cruzados, freeze de 1s en inicio (`-c copy`), Reused / Inauthentic Content (julio 2025) en canal YPP |
| **Instagram Reels** | 1) `3s Skip Rate` + Watch Time/Rewatch, 2) **`Sends Per Reach` (DM Shares)**, 3) Likes/Reach, máx `5` hashtags | Copy orientado a envío por DM (*"Send this to your duo..."*), **Trial Reels** con Variantes A/B (`--ab-variants 2`, distinto hook `0–3s`), portada `9:16` en Caja Segura `3:4` | **`>=10` posts no-originales en 30d = expulsión total de recomendaciones**, engagement bait hueco, re-subir el **mismo MP4 idéntico** en Trial Reels |
| **TikTok** | Batch `200–500` -> `1K–5K` -> viral; `3s Hold >=70%`, Watch Time `18–25s` (`30–60s`), `Share >=1%`, `Save >=2%` | **SEO Cuádruple (ASR + OCR `0–3s` + Caption + Comentarios)**, `cover_timestamp_ms` en API, transformación real anti-pHash, reciclaje seguro solo con **Variante B** | **OCR moderación:** groserías visibles en subtítulos causan *FYP suppression* (bleeper + censura obligatoria `0–7.5s`); pHash duplicado |
| **Facebook Reels** | Watch Time, originalidad verificada, Shares comunitarios | Copy claro de contexto rápido, respeto estricto `top 14% / bottom 35% / lados 6%`, portada en Meta Business Suite | Texto cortado abajo, watermarks externos, reposts sin reacción |
| **X (Twitter)** | `xai-org/x-algorithm` Oct–Nov 2026: **`ShareViaCopyLink +20.0`**, `Reply+Mutual +20.0`, `Reply/DM/Quote +5.0`, `Follow +4.0`, `Like 0.708`, Cold-Start `<2h` (`<50k` followers, `<200` imp), `MinVideoDurationMs=10s` | Subir **MP4 nativo `>=12s`** en `<2h` pico, transformación visual para quedar `<0.72` en `VideoCarouselEmbeddingDedupe`, espaciar `>=3h`, **`link-in-reply`** + `author_reply_seed` | Link externo en post principal, posts `<3h` (`VideoCarouselFatigueMinutes=60`), bots/pods (**impacto 0** en `scoring.rs`), Reports (`-234`) / Mutes (`-58.8`) |

---

## 3. Recetario Determinista Media Engineering (Windows 10 + ONNX + OSS Comercial)

### 3.1 `WyattBlue/auto-editor` (Unlicense) en `src/renderer.py`
- Uso determinista como proveedor de detección de aire muerto con exportación estructurada (`--export json` / análisis de chunks de audio) respetando `--margin 0.25s` (*Synchresis*) y filtro mínimo de pausa `>= 0.95s` para no destruir silencios dramáticos de GTA VI.
- **Fallback automático:** Si `auto-editor` no está disponible o produce >35% de recorte agresivo en una escena ruidosa/tensa, caer limpiamente al analizador PCM RMS existente (`detect_dead_air_segments`).
- **A/B Hook Variants (`--ab-variants 2`):** Generar Variante B alternando texto/banner `0–3s` (o ligero desplazamiento de inicio validado) con nuevo re-encode (`h264_qsv`/`libx264`) para producir un hash perceptual distinto compatible con **IG Trial Reels** y reciclaje seguro en TikTok.

### 3.2 `rembg` + `birefnet-general-lite` (MIT — KS-4 Compliant) en `src/design_engine.py`
- **Modelo obligatorio:** `birefnet-general-lite` (MIT, comercial). **Prohibido `bria-rmbg` (`CC-BY-NC`)**.
- **Control de hilos:** Configurar `SessionOptions.intra_op_num_threads = resource_guard.onnx_threads()` para no saturar la HP 250 G8 (`i5-1035G1`, `16 GB RAM`).
- **Guardrail de cobertura de máscara en GTA VI (`16:9` complejo):**
  - Calcular `mask_coverage = mean(alpha > 128)`.
  - Si `mask_coverage > 0.72` (el modelo englobó demasiado fondo urbano/HUD) o `< 0.04` (falló detección), ejecutar fallback limpio sobre **ROI facial/webcam (`UltraFace` expandido 2.6x)** o máscara elíptica suavizada con contorno *rim-light* para garantizar *Text-Behind-Subject* nítido sin artefactos.
- **Integridad de `onnxruntime`:** Tras cualquier actualización en `.venv`, verificar versión de `onnxruntime` y ejecutar smoke tests reales de `parakeet-tdt-0.6b-v3`, `LR-ASD` (`lr-asd-frontend/backend.onnx`) y `YAMNet` (`yamnet.onnx`).

### 3.3 `FFmpeg 9` + `libass` + Chrome/Edge Headless en Windows
- **`FFmpeg` + `libass`:** Ejecutar siempre con `cwd` en carpeta temporal y ruta relativa `ass=subs.ass:fontsdir=fonts` (nunca `C:\...`), `format=nv12` antes de `h264_qsv`, cero `-c copy` en inicios, `setpts=PTS-STARTPTS+{start}/TB` + `eof_action=pass` en overlays, y `loudnorm` 2-pass (`-14.0 LUFS`, `<= -1.0 dBTP`).
- **Chrome/Edge Headless (patrón `HyperFrames` Apache 2.0):**
  - Invocar con `--headless=new --disable-gpu --force-device-scale-factor=1 --hide-scrollbars --window-size=W,H --virtual-time-budget=2000 --screenshot=<out.png>` (o ajuste compensado de viewport + verificación estricta `PIL.Image.size == (W, H)` con crop/fit exacto cero-distorsión si Windows agrega chrome height).
  - Embeber `Montserrat-Black.ttf` vía `data:font/ttf;base64,...` dentro de `@font-face` para eliminar fallos de carga `file://` en Windows.

### 3.4 Ingesta Resiliente (Kick v2 Centrifugo + Twitch + YouTube)
- **Kick:** API y HLS vía `curl_cffi` (`--impersonate chrome`) contra Cloudflare JA4; chat en tiempo real sobre WebSocket Pusher/Centrifugo con circuit-breaker exponencial ante `429/502/503`.
- **Twitch:** `TwitchDownloaderCLI` (`--threads 6 --bttv=false --ffz=false --stv=false`) para VOD chat + `streamlink --twitch-disable-ads` en `Live-Stream Sniper`.
- **YouTube:** `yt-dlp --js-runtimes deno --remote-components ejs:github --download-sections "*HH:MM:SS-HH:MM:SS"`.

---

## 4. Bounties CPM & Supervivencia Anti-DMCA GTA VI (Brecha 4)
- **Ventana de Burn Rate saludable:** `20%–80%` medido en 2 lecturas separadas (`>=6h`). `<15%` indica campaña estancada; `>85%` riesgo de agotamiento antes de monetizar vistas.
- **Compliance Argentina:** Confirmar elegibilidad global/LATAM antes de producir (sin KYC US-only), mantener cuentas limpias sin VPN/bots y separar el canal de Bounties CPM (`voice_mode: "none"`) del Canal Propio YPP (`human_batch` con Shure SM58 + compilaciones `8–12 min`).
- **Higiene Anti-DMCA:** `YAMNet` AudioSet audita segmentos musicales antes de renderizar; cualquier radio licenciada, DJ de club o música de menú audible sobre diálogo descarta el segmento o activa revisión humana estricta.
