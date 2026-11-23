# Engineering Patterns — Clipping Studio (Mapa de Reutilización y Extracción)

> Mapa conciso (<1.800 tokens) de qué se reutiliza o extrae de los proyectos locales auditados y de `_references/` (solo lectura), evitando monolitos "Frankenstein" y sobreingeniería.

---

## 1. Clasificación de Proyectos Locales Auditados (Solo Lectura — Sin Modificar)

| Proyecto Local | Clasificación | Qué Extraemos Exactamente | Qué Descartamos Estrictamente |
| :--- | :---: | :--- | :--- |
| **`C:\Users\tomas\.zcode\workspace\default\centinela-studio`** | **`EXTRAER PATRÓN`** | 1. **Zero-Disk Streaming Pipe** (`docs/research/MASSIVE_VIDEO_INGESTION_GUIDE_2026.md` + `tools/lib/video-stream-pipe.mjs`): patrón `yt-dlp -o - \| ffmpeg -i pipe:0` para extraer audio 16kHz + keyframes sin bajar MP4s de 30 GB.<br>2. **Scene Gradient Filter** (`select=gt(scene,0.25)`) + **Frame Differencing** (`<4%` delta omite VLM).<br>3. **Smart HLS Slicing** y experiencia WAF Kick/Cloudflare (`curl_cffi` TLS/JA4). | Bot 24/7 de chat en vivo, infraestructura Node/TS de Centinela, pipelines de slots/casino y shadow-mode RLHF. |
| **`C:\Users\tomas\Desktop\streamerbot`** | **`EXTRAER PATRÓN`** | 1. **Circuit Breaker y Watchdog anti-429** (`docs/KICK_CONNECTION_RESILIENCE_AND_WATCHDOG.md`, `core/src/ingestor/kick-watchdog.ts`): backoff exponencial y rotación defensiva para no quemar IP ante 502/503.<br>2. **Principios visuales anti-AI slop** (`knowledge/AGENT_CREATIVE_AND_VISUAL_AWARENESS_2026.md`: *De-Boxing*, regla 90/8/2, loop de crítica multimodal en JSON). | Suite de casino/slots, overlays OBS, DB SQLite de bonushunts y paneles web. |
| **`C:\Users\tomas\Desktop\web-software-factory`** | **`EXTRAER PATRÓN`** | 1. **`.antigravityignore`** para excluir `_references/`, `runs/`, medios y secretos del indexador.<br>2. **Schema liviano de FinOps Budget** (`.agents/governance/gcp_finops_budget.json`: techo de gasto, política `allow24x7Instances: false`, caché implícito).<br>3. **Checkpoint JSON simple por run** (inspirado en `current_factory_state.json`, reducido a 1 archivo `run_state.json` por lote para reanudar sin re-transcribir). | Toda la maquinaria pesada: Omni-RAG v5 local, XState multi-sala, 9+1 agentes, LightningCSS/Astro, JEV/beads y sweeps web. |
| **`C:\Users\tomas\Documents\yendo_mobile`** | **`EXTRAER PATRÓN`** *(solo `.knowledge/`)* | 1. **Gemini 2D Spatial Bounding Boxes** (`.knowledge/gemini-multimodal-qa-sota.md`): pedir `[ymin, xmin, ymax, xmax]` en escala `0–1000` a Gemini Flash Vision para **auto-detectar la caja de la webcam del streamer en GTA VI** en 1 sola llamada API.<br>2. **Modelo Hook** (destilado de `yendo_obsidian_vault`). | 100% del código móvil Expo/React Native/Supabase/fintech. |

---

## 2. Patrones Exactos a Extraer de `_references/` (Clonados Solo Lectura)

Regla: Prohibido importar código directo desde `_references/`. Reescribir limpio en `src/` bajo Python 3.12 + Pydantic v2.

| Repo en `_references/` (Commit) | Archivo / Módulo Exacto | Patrón a Implementar en `src/` | Qué NO Copiar |
| :--- | :--- | :--- | :--- |
| **`agentic-video-editor`** (`47248b5`, MIT) | `src/models/schemas.py`<br>`src/agents/{director,trim_refiner,reviewer}.py`<br>`.claude/policies/ffmpeg-overlay-pts-timing.md` | - Contratos Pydantic: `WordTimestamp`, `Shot`, `EditPlanEntry`, `EditPlan`, `ReviewScore` (5 ejes 0–1 + feedback).<br>- Flujo `Preprocess -> Director -> TrimRefiner -> Editor -> Reviewer` con `max_retries=1`.<br>- Regla FFmpeg: `setpts=PTS-STARTPTS+{start}/TB` + `eof_action=pass` en overlays retrasados. | `MoviePy`, servidor FastAPI `src/web/` y UI web. |
| **`cutawan`** (`084d0b2`, MIT) | Módulos ONNX UltraFace + LR-ASD y plantillas `libass` | - Reframing activo por hablante con **UltraFace + LR-ASD en `onnxruntime`** (corre en CPU HP 250 G8, T4 y L4 sin PyTorch pesado).<br>- Render karaoke `.ass` con `libass` y remoción de muletillas/pausas. | App Electron + Vite + React + dependencia obligatoria de OpenAI API. |
| **`ClipKaro`** (`e573591`, MIT) | `clipper/events.py`, `clipper/facecam.py`, `clipper/captions.py`, `clipper/fetch.py` | - Anotaciones no verbales en transcript (`[laughter]`, `[shouting]`, `[voice raised]`) y descarte de BRB/Starting Soon.<br>- Regla GTA VI: **no subtitular NPCs del juego**, iniciar en la acción/caída (no tras el grito) y mantener subtítulos fuera de rostros/botones.<br>- Workaround Windows `cookies.txt` contra DPAPI. | Acoplamiento exclusivo a Claude Code y `PostKaro` auto-posting sin gate humano. |
| **`clipmint`** (`4ca9b70`, MIT) | `docs/gaming-clips.md`, fallback ladder LLM y ranking por formato | - Escalera anti-503/429: `Vertex/Gemini -> Groq -> OpenRouter` con salida JSON validada por Pydantic.<br>- Filtro de picos de chat ignorando spam pedido (*"type W"*).<br>- Guardado de transcripción por chunks para reanudar sin repetir trabajo. | Empaquetado `.exe` GUI y acoplamiento a Ollama local. |

---

## 3. Contratos Base del Núcleo Propio (`src/` — Se programa en Prompt 3)
- **Configuración única (`config/profiles.yaml`):** Define `compute_profile` (`local_hp | gcp_spot_l4 | colab_t4`), encoder (`h264_qsv` -> `libx264` / `h264_nvenc`), precisión (`int8`/`fp16` + `sdpa`) y `voice_mode` (`none | human_batch | tts`).
- **Checkpoint minimalista por ejecución (`runs/<run_id>/state.json`):** Guarda fase completada (`ingested -> transcribed -> curated -> rendered -> reviewed`) para no repetir descargas ni transcripciones ante errores.
- **Trazabilidad de compilación Long-Form:** Cada `clip_manifest.json` guarda `source_url`, `start_sec`, `end_sec`, `dmca_clear`, `scores` y ruta del master 9:16 + coordenadas 16:9.
