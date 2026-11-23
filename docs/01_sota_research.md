# FASE 1 — AUDITORÍA SOTA, ARQUITECTURA DE REFERENCIA Y RESTRICCIONES REALES: "CLIPPING STUDIO"

**Fecha de auditoría:** 22 de noviembre de 2026  
**Rol:** Arquitecto Principal / Co-CTO  
**Estado de validación:** `[Validación Estática / Documental — Nov 2026]`. Ninguna afirmación de runtime local (`h264_qsv`, drivers Intel, `yt-dlp` sobre IP local, `libass` en Windows 10) se da por probada hasta ejecutar las pruebas reales de Fase 2.

---

## Resumen Ejecutivo y Refutaciones Clave al Brief Inicial

Antes de entrar al detalle, esta auditoría **refuta o corrige 6 supuestos críticos** del brief orientativo con evidencia de noviembre de 2026:

1. **Trampa de Licencia Comercial en `k2-fsa/OmniVoice` (Kill-Switch):** Aunque su código es Apache 2.0, **los pesos preentrenados en HuggingFace están bajo licencia `CC-BY-NC` (No Comercial)** por herencia del dataset Emilia y del tokenizer. Queda **descartado** para producción comercial (bounties/YPP). Reemplazo comercial limpio: voz humana real (Shure SM58) como prioridad v1, **`kyutai/pocket-tts`** (MIT / CC-BY-4.0, ya multilingüe EN+ES en CPU) y **`openbmb/VoxCPM2`** (Apache 2.0, `fp16` en T4).
2. **Trampa de Licencia Comercial en VFX Fase 2+:** `MatAnyone 2` usa **NTU S-Lab License 1.0 (No Comercial)** y `Depth Anything 3` usa licencia mixta (solo `Small`/`Base` son Apache 2.0; `Large`/`Giant` son `CC-BY-NC 4.0`). Congelados y marcados con alerta legal.
3. **ASR SOTA más rápido y liviano que `faster-whisper` y `Qwen3-ASR`:** **`nvidia/parakeet-tdt-0.6b-v3` (CC-BY-4.0)** vía **`onnx-asr` (`int8`)** soporta **Inglés y Español** (25 idiomas europeos), genera timestamps por palabra nativos, supera a Whisper Large-v3 en WER (~6.3% vs ~7.4%) y corre a **15x–30x tiempo real en CPU** (corre incluso en la HP 250 G8 sin GPU, además de T4 y L4). `faster-whisper` + `stable-ts` pasa a rol de fallback universal (99 idiomas / hotwords).
4. **Reemplazo SOTA en la terna de repositorios (`JeremySNR/cutawan` > `mutonby/openshorts`):** `openshorts` depende de Docker + FastAPI + React, tiene carpeta `cloud/` con licencia comercial restrictiva y usa solo rastreo facial/pose (MediaPipe + YOLOv8, sin Active Speaker Detection). **`JeremySNR/cutawan` (MIT)** implementa **UltraFace + LR-ASD (Active Speaker Detection) sobre ONNX Runtime** + `libass` karaoke + remoción de muletillas sin monolito web.
5. **Requisitos reales de `yt-dlp` en noviembre de 2026:** Ya NO alcanza con instalar `yt-dlp` solo. YouTube exige un **runtime JS externo (Deno v2.3+ / EJS)** para resolver retos `nsig` (sin él degrada a 360p o falla), **PO Token (`bgutil-ytdlp-pot-provider`)** en IPs de datacenter (GCP/Colab reciben `HTTP 403`) y exportación estática de `cookies.txt` desde Firefox o sesión incógnito cerrada en Windows (Chrome bloquea por DPAPI). Para Kick.com se exige `curl_cffi` (`--impersonate chrome`) contra Cloudflare JA4.
6. **Realidad táctica de GTA VI (lanzado el 19/11/2026, hace 72 horas):** Existen bounties activos hoy en Whop Content Rewards y Vyro (USD 0.50–2.00+ CPM), **pero los streamers están sufriendo reclamos agresivos de Content ID/DMCA en YouTube y mutes en Twitch incluso con *"Mute Licensed Music"* activado** (música de menús, locales y DJs) además de riesgo por desnudez accidental. Arrancar desde 0 líneas de código con Modo B expone el sprint de 72h a quedar atascado en DMCA y calibración de HUDs.

---

## Rúbrica de Admisión Aplicada

Cada componente se puntúa sobre **100 puntos** (5 ejes de 0–20; umbral mínimo de aprobación: **75/100**):
- **R** = Compatibilidad Runtime Real (L4 `sm_89` + T4 `sm_75` + HP 250 G8 Win10)
- **I** = Impacto vs. Complejidad
- **T** = Impuesto de Tokens / Latencia
- **L** = Licencia Comercial y Seguridad
- **V** = Vigencia (Nov 2026)

**Kill-Switches automáticos:** (KS-1) Exige suscripción paga nueva; (KS-2) Falla en T4 `sm_75` sin fallback para el core permanente; (KS-3) No aporta valor directo a los primeros 10 clips; (KS-4) Pesos o código no comerciales (`NC`) en flujo monetizable.

---

## 1. Terna Final de Repositorios de Referencia (Máx. 4 para `_references/` Solo Lectura)

Política: Cero "Frankenstein". En Fase 2 se clonarán en `_references/` (listado en `.gitignore`, solo lectura) para extraer exclusivamente patrones puntuales hacia nuestro núcleo limpio en Python.

### 1.1 Seleccionados (Aprobados >= 75/100)

| Repositorio | Licencia | Puntaje (R/I/T/L/V) | Total | Patrón Exacto a Extraer | Abogado del Diablo (Por qué NO usar / Dónde rompe en runtime) |
| :--- | :--- | :---: | :---: | :--- | :--- |
| **`poseljacob/agentic-video-editor`** (~510+ ★) | **MIT** | 16 / 18 / 15 / 19 / 18 | **86/100** | Arquitectura multi-agente declarativa (YAML + Pydantic `EditPlan`): `Preprocess` -> `Director` -> `TrimRefiner` -> `Editor` -> `Reviewer` (rúbrica 5 ejes + loop de reintento acotado). | **Dónde rompe:** Usa `MoviePy` en su capa Editor, que dispara consumo de RAM y lentitud en CPU/Windows, y un loop `Reviewer -> Director` sin tope estricto quema tokens rápido. **Mitigación:** Extraer *solo* el esquema `EditPlan`, los prompts de `TrimRefiner`/`Reviewer` y limitar `max_retries=1`; reemplazar `MoviePy` por `FFmpeg` puro. |
| **`JeremySNR/cutawan`** *(Contra-propuesta SOTA)* (~90+ ★) | **MIT** | 18 / 18 / 18 / 19 / 17 | **90/100** | Reframing activo multi-hablante con **UltraFace + LR-ASD (Active Speaker Detection) sobre ONNX Runtime**, remoción de pausas/muletillas y subtítulos karaoke nativos en **`libass`**. | **Dónde rompe:** Comunidad más chica que `openshorts` (~90 ★), empaquetado como app de escritorio; LR-ASD puede confundirse en perfiles laterales extremos (<45° de rostro visible) o cortes rápidos de cámara. **Mitigación:** Extraer únicamente los módulos de inferencia ONNX (UltraFace + LR-ASD) y plantillas `.ass`; agregar suavizado con *deadband* y fallback a split-screen fijo cuando hay 2 rostros simultáneos. |
| **`allthingsaman/ClipKaro`** | **MIT** | 16 / 17 / 16 / 18 / 17 | **84/100** | Anotación de eventos no verbales en el transcript (`[laughter ~2s]`, `[shouting]`, `[voice raised]`), detección de pantallas BRB/silencio, patrón *Agente-Editor -> CLI determinista local* y workaround de `cookies.txt` en Windows contra DPAPI. | **Dónde rompe:** Diseñado alrededor de Claude Code; si el proyecto vive en `Desktop` o `Documents` sincronizados con OneDrive, los temporales de video saturan OneDrive y bloquean archivos en Windows; las cookies exportadas de Chrome activo rotan y caducan rápido. **Mitigación:** Trabajar siempre fuera de carpetas sincronizadas con OneDrive (o excluir `work/`), y exportar cookies desde Firefox o sesión incógnito cerrada. |
| **`kleZ799/clipmint`** | **MIT** | 17 / 16 / 17 / 18 / 16 | **84/100** | Escalera de resiliencia gratuita anti-503/429 (`Gemini multi-key` -> `Groq` -> `OpenRouter` -> `Ollama`), rúbricas de selección por formato y preset de encuadre *webcam-over-gameplay* para Fase 1.5/Modo B. | **Dónde rompe:** Rotar múltiples API keys de Gemini de cuentas gratuitas puede violar ToS de Google si se abusa en paralelo; los modelos gratuitos de OpenRouter/Groq tienen ventanas de contexto menores que Gemini y producen JSON malformado si no hay validación Pydantic estricta. **Mitigación:** Usar Vertex AI / Google AI Pro como primario, Groq/OpenRouter solo como fallback de emergencia secuencial con `structured outputs` validados por Pydantic. |

### 1.2 Candidatos Evaluados y Descartados (o Diferidos)

| Repositorio | Licencia | Puntaje | Veredicto | Motivo Principal y Abogado del Diablo |
| :--- | :--- | :---: | :---: | :--- |
| **`mutonby/openshorts`** (~9.2k ★) | MIT core / **Comercial** (`cloud/`) | 71/100 | **Descartado** | Monolito pesado (Docker + FastAPI + React 18 + Vite) innecesario para arquitectura CLI/Agent-first; licencia dual contaminada en `cloud/`; usa MediaPipe + YOLOv8 (rastrea caras/poses, **no** quién está hablando realmente como el LR-ASD de `cutawan`). Solo se toma nota conceptual de su regla de *deadband* para estabilizar cámara. |
| **`heygen-com/hyperframes`** | Apache 2.0 | 72/100 (en v1) | **Diferido a Fase 2** | **Dispara Kill-Switch KS-3:** Excelente engine HTML/GSAP->MP4 con `lint` + `check`, pero diseñado para motion graphics editoriales (Modo C). No aporta valor a los primeros 10 clips y agrega stack Node/Chromium headless en v1. |
| **`Anil-matcha/AI-Youtube-Shorts-Generator`** | MIT | 42/100 | **Descartado** | Wrapper frágil: su `--mode api` depende de `MuAPI` (pago/cerrado, **KS-1**) y su `--mode local` usa recorte OpenCV básico que genera saltos bruscos de cámara y conflictos de binarios `opencv-python` vs `headless`. |
| **`sherlock-dev-ai/Clipify-AI-Video-Clipper-FREE`** | Open Source (revisar) | 58/100 | **Descartado** | Monolito GUI desktop acoplado a Ollama local (`qwen2.5` no corre viablemente en la CPU de la HP 250 G8 sin GPU dedicada). |
| **`ClipsAI/clipsai`** | MIT | 55/100 | **Descartado** | Librería histórica superada en 2026: segmentación textual básica sin criterio editorial de *hot-take*, sin diarización visual ONNX ligera y dependencia de stack pesado heredado. |

---

## 2. Auditoría de Modelos HuggingFace, Core Determinista, MCPs y Skills Públicas

### 2.1 Modelos HuggingFace (Validación Dual L4 `sm_89` vs. T4 `sm_75` vs. HP 250 G8 CPU)

| Modelo / Componente | Rol | Licencia Exacta | Puntaje (R/I/T/L/V) | Total | Compatibilidad Hardware (L4 / T4 / HP CPU) | Abogado del Diablo (Por qué NO usar / Dónde rompe en runtime) |
| :--- | :--- | :--- | :---: | :---: | :--- | :--- |
| **`nvidia/parakeet-tdt-0.6b-v3`** vía **`onnx-asr` (`int8`)** *(Recomendado Core v1)* | ASR primario EN/ES + word timestamps | **CC-BY-4.0** (Comercial OK con atribución) | 20 / 19 / 20 / 19 / 20 | **98/100** | **L4:** ~1.5 GB VRAM. **T4:** ~2–3 GB VRAM. **HP CPU:** **15x–30x real-time** (1h de podcast en 2–4 min). | Limitado a 25 idiomas europeos (sin japonés/chino/coreano); **no soporta inyección de hotwords/glosarios** (nombres raros de streamers o jerga gaming pueden transcribirse mal); signos españoles `¿`/`¡` requieren sanitización de offsets en algunos wrappers `onnx-asr` y audios largos (>24 min) requieren chunking con VAD. |
| **`faster-whisper` (`large-v3-turbo`, `int8`) + `stable-ts`** *(Fallback Core v1)* | ASR fallback 99 idiomas / precisión subtítulos | **MIT** | 18 / 17 / 16 / 20 / 17 | **88/100** | **L4:** ~2–3 GB VRAM. **T4:** ~2–4 GB VRAM (Tensor Cores INT8 Turing). **HP CPU:** Lento (~0.5x–1.5x RT). | `stable-ts` añade pasadas extra que duplican latencia; Whisper alucina texto repetitivo en silencios/música si no se activa Silero VAD; más lento que Parakeet en T4 y CPU. |
| **`sherpa-onnx` (`pyannote-segmentation-3.0` + `3dspeaker`/`wespeaker`)** | Diarización liviana multi-hablante | **Apache 2.0** (framework) + **CC-BY-4.0** (pesos) | 18 / 17 / 18 / 18 / 18 | **89/100** | **L4/T4:** ONNX CUDA EP. **HP CPU:** Corre nativamente en Windows sin PyTorch. | Requiere ensamblar 2 archivos ONNX (segmentación + embeddings) y ajustar umbral de clustering; en risas superpuestas tiene menor precisión que `pyannote-community-1` en GPU. |
| **`Qwen/Qwen3-ASR-1.7B` + `Qwen3-ForcedAligner-0.6B`** | Especialista ASR + alineación (Opcional Fase 1.5) | **Apache 2.0** | 13 / 15 / 13 / 20 / 19 | **80/100** *(No core v1)* | **L4:** Óptimo (`bf16` + FA2, ~8 GB). **T4:** Solo `float16` + `attn_implementation="sdpa"` (~7–10 GB). **HP CPU:** Inviable. | **Trampa T4 (`sm_75`):** `flash_attention_2` **rompe en runtime** en Turing (sin wheels precompilados) y `bfloat16` degrada por emulación por software; obliga a forzar `sdpa` + `fp16`. Además, 2 modelos juntos ocupan ~10 GB VRAM y no corren en la HP 250 G8. |
| **`BUT-FIT/Pandemonium`** | ASR multi-hablante condicionado (Fase 2) | **Apache 2.0** | 13 / 14 / 14 / 20 / 19 | **80/100** *(Diferido Fase 2)* | **L4:** Holgado. **T4:** Requiere chunking estricto. **HP CPU:** Inviable. | **No hace diarización propia** (depende de correr un diarizer previo); la atención de contexto largo crece cuadráticamente en VRAM en T4 16 GB. Complejidad innecesaria para los primeros 10 clips (**KS-3**). |
| **`kyutai/pocket-tts`** | TTS local USD 0 (Cold Opens / narración backup) | **MIT** (código) / **CC-BY-4.0** (pesos, gated HF) | 19 / 17 / 19 / 18 / 19 | **92/100** | **HP CPU:** **5x–9x real-time**, TTFA 200–430 ms (~100M params). Corre sin GPU. | Soporta EN/ES/FR/DE/PT/IT, pero clonar con referencia en un idioma para hablar otro introduce acento artificial; requiere aceptar términos HF (prohíbe clonar terceros sin consentimiento); muchos bounties prohíben voz IA → **priorizar Shure SM58 humano en v1**. |
| **`openbmb/VoxCPM2`** | TTS alta fidelidad 48kHz (GPU T4/L4) | **Apache 2.0** (Comercial OK) | 16 / 16 / 15 / 20 / 20 | **87/100** | **L4:** Óptimo. **T4:** ~8 GB VRAM en `fp16` (cabe en 15–16 GB). **HP CPU:** Inviable. | 2B parámetros (peso ~4–5 GB en disco, pico ~8 GB VRAM); overkill si el operador graba Cold Opens de 3–4s en lote con Shure SM58 + Mackie Onyx. Reservar para narraciones en inglés si el acento humano limita retención Tier-1. |
| **`k2-fsa/OmniVoice`** | TTS / voice cloning | Apache 2.0 código / **`CC-BY-NC` pesos** | 16 / 15 / 16 / **0** / 19 | **66/100 — RECHAZADO (KS-4)** | T4 OK (~3–4 GB `fp16`). | **KILL-SWITCH COMERCIAL:** Sus pesos en HuggingFace heredan restricción **No Comercial (`CC-BY-NC`)** del dataset Emilia y el tokenizer. Prohibido en canales monetizados o bounties pagos. |
| **`Demucs` (`htdemucs`) / `audio-separator`** | Aislamiento vocal anti-DMCA (Solo si se activa Modo B) | **MIT** (código) / **Pesos mixtos** | 16 / 15 / 15 / 12 / 16 | **74/100** *(Condicional Modo B)* | **T4/L4:** 10–30s por clip de 3 min. **HP CPU:** Lento. | **Doble trampa:** (1) Varios checkpoints MDX-Net/UVR son *non-commercial* (auditar cada `.onnx`/`.ckpt`); (2) Separar música licenciada deja artefactos metálicos y **no elimina el riesgo legal** si el audio original sigue violando copyright. Mejor prevenir eligiendo fuentes limpias. |
| **VFX Fase 2+ (`SAM 3.1`, `MatAnyone 2`, `DA3-Streaming`, `HunyuanVideo-Foley`)** | VFX avanzados GPU | **Mixta con alertas NC** | — | **CONGELADOS (KS-3)** | `SAM 3.1` (~4 GB `fp16`, Meta SAM gated); `DA3-Streaming` (<12 GB, **solo Small/Base Apache 2.0**, Large/Giant `CC-BY-NC`); `MatAnyone 2` (**NTU S-Lab 1.0 NC**); `HunyuanVideo-Foley` (Tencent Community, 10–12 GB BF16, FP8 **no nativo en T4**). | **Triple riesgo:** No aportan a los primeros 10 clips, `MatAnyone 2` y `DA3-Large/Giant` bloquean uso comercial, y `HunyuanVideo-Foley` en FP8 rompe o emula lento en T4 `sm_75`. |

### 2.2 Core Determinista de Ingesta y Render (Hallazgos Críticos Windows 10 + 2026)

| Herramienta | Licencia | Estado | Abogado del Diablo (Por qué NO usar / Dónde rompe en runtime) |
| :--- | :---: | :---: | :--- |
| **`yt-dlp`** + **Deno v2.3+** + **`curl_cffi`** | Unlicense / MIT | **Aprobado Core v1** | **Falla en 2026 si falta Deno:** YouTube migró a EJS; sin Deno v2.3+ en `PATH` (o `--js-runtimes node`) solo baja 360p o falla. En GCP/Colab las IPs de datacenter reciben `403` sin `bgutil-ytdlp-pot-provider` (PO Token). En Windows, `--cookies-from-browser chrome` falla por DPAPI → usar Firefox o `cookies.txt` exportado desde incógnito cerrado. |
| **`TwitchDownloaderCLI`** (`lay295/TwitchDownloader`) | MIT | **Aprobado (Modo B)** | Excelente para `chatdownload -b -e` parcial sin bajar VODs de 40 GB, pero **no soporta Kick.com** (requiere `yt-dlp --impersonate chrome` con `curl_cffi`) y los emotes terceros pueden fallar si cambian endpoints. |
| **`FFmpeg`** (`BtbN` static con `libass` + `oneVPL`) + **`pysubs2`** + **`Pillow`** | LGPL/GPL / MIT / HPND | **Aprobado Core v1** | **3 bugs clásicos en Windows:** (1) `subtitles=` rompe con `C:\...` → ejecutar siempre con `cwd` en carpeta temporal y ruta relativa (`subs.ass`); (2) `h264_qsv` + `libass` en HP 250 G8 falla por formato de superficie si no se encadena `subtitles=subs.ass,format=nv12,hwupload` y driver Intel DCH actualizado; (3) `-c copy` al cortar inicio genera 1–3s congelados por desfase de keyframe → **prohibido `-c copy` en trims de clip**, siempre re-encode preciso. |

### 2.3 Servidores MCP (Tope Estricto v1: Máx. 3 Activos)

Penalización fuerte a servidores con *tool bloat* (>15 tools inflan el system prompt y degradan la selección de herramientas del agente).

| Servidor MCP | Tools | Licencia | Puntaje (R/I/T/L/V) | Veredicto v1 | Abogado del Diablo (Por qué NO usar / Dónde rompe en runtime) |
| :--- | :---: | :---: | :---: | :---: | :--- |
| **`context7`** *(ya presente en el IDE)* | **2** | Hosted/Free | 19 / 18 / 20 / 17 / 19 (**93/100**) | **ACTIVO #1** | Depende de internet y de cobertura del índice; puede devolver snippets de versiones ligeramente distintas si no se especifica librería exacta. Aun así, su impuesto de contexto (2 tools) es imbatible contra alucinaciones de flags de FFmpeg/yt-dlp/GenAI SDK. |
| **`kevinwatt/ffmpeg-mcp-lite`** | **~8** | MIT | 17 / 17 / 18 / 19 / 17 (**88/100**) | **ACTIVO #2** | Repo pequeño (~30 ★); no reemplaza filtergraphs complejos de split-screen/QSV/NVENC (que deben vivir en scripts Python versionados y testeados). Usarlo **solo** para inspección (`info`), extracción de frames PNG/WebP para QA visual del agente `Reviewer` y trims rápidos de prueba. |
| **MCP propio `clipping-studio-mcp` (Fase 1.5, <= 6 tools)** *o mantener en 2 MCPs en v1* | **<= 6** | Propio | 18 / 17 / 19 / 20 / 20 (**94/100**) | **ACTIVO #3 (Opcional)** | Construirlo antes de tener el CLI Python funcionando es sobreingeniería. **Regla v1:** Empezar con **solo 2 MCPs** (`context7` + `ffmpeg-mcp-lite`) y ejecutar nuestro CLI vía Terminal Sandbox; recién empaquetar `clipping-studio-mcp` si el uso diario lo justifica. |
| **`@kevinwatt/yt-dlp-mcp`** | ~6 | MIT | 13 / 13 / 16 / 18 / 16 (**76/100**) | **Descartado v1** | En Windows 2026 oculta flags críticos (`--js-runtimes`, `--cookies`, `--download-sections`, `--impersonate chrome`). Ejecutar `yt-dlp` directo desde wrapper CLI versionado da control total y cero fricción. |
| **`mutonby/openshorts` MCP** | 15+ | MIT/Comercial | 11 / 12 / 10 / 14 / 18 (**65/100**) | **Rechazado** | Obliga a levantar Docker + FastAPI + React entero en la HP 250 G8 e inyecta demasiadas tools acopladas a su propia arquitectura. |
| **`Playwright MCP` / `Qdrant MCP`** | 20+ / 6 | Apache 2.0 | — | **CONGELADOS (Fase 2)** | **KS-3:** `Playwright MCP` consume miles de tokens en snapshots DOM y no se necesita antes de los primeros 10 clips; `Qdrant MCP` se habilita recién con >100 clips empíricos. |

### 2.4 Auditoría de Skills Públicas de Marketing y Audiovisual + Seguridad Supply-Chain

**Política de Seguridad Obligatoria (Nov 2026):** Prohibido ejecutar `npx skills add ...` a ciegas. El ecosistema público de `SKILL.md` en GitHub/registries presenta vectores reales de **supply-chain**: (a) *description-vs-behavior mismatch* (progressive disclosure muestra solo YAML y oculta instrucciones maliciosas en el cuerpo), (b) scripts auxiliares que exfiltran `.env`/keys bajo privilegios locales del usuario, y (c) *prompt injection* persistente en memoria del agente.  
**Gate obligatorio para Fase 3:** Toda skill externa auditada se **destila manualmente a texto plano propio** en `.agents/skills/<nombre>/SKILL.md` (sin copiar binarios ni scripts no inspeccionados línea por línea; aplicar regla *Read-the-Whole-File* + chequeo estático estilo NVIDIA SkillSpector). Además, respetando la convención vigente de **Google Antigravity IDE (Nov 2026)**, **NO usar `.agents/workflows/`** (deprecado oct 2026), sino `AGENTS.md`/`GEMINI.md`, `.agents/rules/*.md` (con frontmatter `trigger`), `.agents/skills/*/SKILL.md` y Lifecycle hooks.

| Fuente / Skill Pública Auditada | Licencia | Puntaje (R/I/T/L/V) | Veredicto | Qué Destilar en Fase 3 y Abogado del Diablo |
| :--- | :---: | :---: | :---: | :--- |
| **`coreyhaines31/marketingskills`** (`skills/video/SKILL.md`, copywriting, hooks) | **MIT** | 18 / 15 / 14 / 18 / 19 (**84/100**) | **Destilar selectivamente (Fase 3)** | **Útil:** Estructuras de hook, claridad de propuesta de valor y copy de títulos/CTAs. **Abogado del Diablo:** ~80% está orientado a SaaS B2B (CRO, SEO, landing pages, paid ads); instalarlo entero inyecta decenas de skills irrelevantes y genera tono "marketero corporativo" (AI slop) letal en TikTok/Shorts. |
| **`heygen-com/hyperframes` (`SKILL.md`)** | **Apache 2.0** | 16 / 17 / 16 / 19 / 20 (**88/100**) | **Destilar en Fase 2 (Modo C)** | **Útil:** Patrón de verificación obligatoria `lint` + `check` antes de aprobar un render. **Abogado del Diablo:** Inútil en los primeros 10 clips (**KS-3**); acoplado a generación HTML/GSAP. |
| **`remotion-dev/skills`** | **MIT** | 15 / 13 / 14 / 19 / 18 (**79/100**) | **Descartado** | **Abogado del Diablo:** Arrastra ecosistema React/Node/Chromium pesado; si en Fase 2 entramos a Modo C Editorial, `hyperframes` (Apache 2.0, HTML/GSAP) es más directo para agentes que Remotion. |
| **Catálogos genéricos (`skillsllm.com`, packs "viral short-form retention")** | Variable / Sin auditoría | 10 / 9 / 10 / 6 / 14 (**49/100**) | **Rechazado** | **Abogado del Diablo:** Recetas rígidas tipo *"cortá cada 1.8s y meté B-roll genérico"* producen exactamente el **AI slop** que TikTok/YouTube penalizan en 2026; trazabilidad de seguridad deficiente y riesgo alto de scripts o prompts inyectados. En Fase 3 crearemos nuestras propias `.agents/skills/` con reglas editoriales concretas (safe-zones Galaxy A36, apertura en remate, pacing por respiración real). |

---

## 3. Plan FinOps de los USD 30 de GCP + Degradación Elegante

### 3.1 Presupuesto Exacto de los USD 30 (Precios Verificados Nov 2026)

| Partida GCP | Recurso / Región Recomendada | Tarifa Referencia (Nov 2026) | Asignación | Capacidad Real Obtenida | Guardrail Obligatorio |
| :--- | :--- | :--- | :---: | :--- | :--- |
| **Cómputo GPU bajo demanda** | `g2-standard-4` **Spot VM** (4 vCPU, 16 GB RAM, 1× NVIDIA L4 24 GB `sm_89`) en **`us-east4`** (o `us-central1`) | ~**USD 0.394/h** (`us-east4`) / ~**USD 0.423/h** (`us-central1`) *(vs. ~USD 0.70/h On-Demand)* | **USD 16.00** | **~38 a 40 horas netas** de GPU L4 (suficiente para cientos de renders NVENC + pruebas ASR/ASD). | **Watchdog `systemd`/cron:** `shutdown -h now` automático tras **12 min** sin procesos `ffmpeg`/`python` ni sesiones SSH activas. **Cero discos `pd-ssd` ociosos:** usar disco mínimo `pd-standard` (50 GB ~USD 2/mes proporcional por días activos) o borrar la VM y recrearla vía startup-script cuando no se use por >48h. |
| **Inferencia LLM / TTS sin `429`** | **Vertex AI API** (Gemini 4 Flash/Hydrogen + Context Caching ~75% desc. + Gemini 3.8 Flash TTS opcional) | Flash: ~USD 0.75–1.50/1M in, USD 3.75–7.50/1M out; TTS: USD 0.50/1M text in, USD 9.00/1M audio out | **USD 9.00** | **>400 ciclos completos** (`Director` + `TrimRefiner` + `Reviewer` visual con frames WebP) sin throttling durante el sprint. | Cachear `style_bible.md` + `marketing_playbook.md`; limitar `Reviewer` a 4–6 frames clave en WebP (no subir el video entero salvo fallo); `max_retries=1`. |
| **Storage + Buffer de seguridad** | **GCS Standard** (misma región `us-east4` / `us-central1`) + **Red Standard Tier** | Storage: **USD 0.020–0.023/GB-mes**. Egress Standard Tier: **200 GiB/mes gratis** (Premium Tier cobra USD 0.12/GB tras 1 GiB). | **USD 5.00** | Almacenamiento temporal de slices y pesos (0 USD transferencia intra-región VM <-> GCS) + colchón anti-desvíos. | **Regla de Oro Anti-Egress:** Jamás descargar VODs de 40 GB en GCP ni usar Premium Tier/Cloud NAT/IP estática reservada. Bajar solo slices de 2–5 min (50–150 MB) desde la HP 250 G8 y subir lo mínimo necesario; lifecycle rule en bucket para auto-borrar temporales a los 7 días. |
| **Alertas y Corte** | Cloud Billing Budgets | USD 0 | — | Alertas al **50% (USD 15)**, **80% (USD 24)** y **95% (USD 28.50)**. | Al cruzar 90% se cambia `compute_profile: gcp_spot_l4` -> `colab_t4` o `local_hp` con 1 línea de config, **sin refactorizar código**. |

### 3.2 Matriz de Compatibilidad y Degradación Elegante (`compute_profile`)

La abstracción `compute_profile` desacopla el pipeline en un único archivo de configuración (`profiles.yaml`), garantizando continuidad a **USD 0/mes** al agotarse los créditos GCP:

| Etapa del Pipeline | `gcp_spot_l4` *(Acelerador USD 30 — L4 24 GB `sm_89`)* | `colab_t4` *(Permanente USD 0 — T4 16 GB `sm_75`, 200 CU/mes)* | `local_hp` *(Permanente USD 0 — HP 250 G8 Win10, SSD 960 GB)* | Estado de Validación |
| :--- | :--- | :--- | :--- | :--- |
| **Ingesta (`yt-dlp` / `TwitchDownloaderCLI`)** | Solo slices si hay PO Token; **preferir ingesta en `local_hp`** | Evitar YouTube directo (IP datacenter = `403` sin PO Token) | **Primario (IP residencial)** + Deno v2.3+ + `cookies.txt` | `[Pendiente Runtime Fase 2]` |
| **ASR + Word Timestamps** | `parakeet-tdt-0.6b-v3` (`onnx-asr` CUDA) / fallback `faster-whisper` `int8` | `parakeet-tdt-0.6b-v3` (`onnx-asr` CUDA) / fallback `faster-whisper` `int8` | **`parakeet-tdt-0.6b-v3` (`onnx-asr` CPU `int8` — 15–30x RT)** | `[Estática OK / Validar CPU Fase 2]` |
| **Diarización + Active Speaker (ASD)** | `cutawan` ONNX (UltraFace + LR-ASD CUDA) + `sherpa-onnx` | `cutawan` ONNX (UltraFace + LR-ASD CUDA `fp16`/`fp32`) | `cutawan` ONNX (UltraFace + LR-ASD CPU) + `sherpa-onnx` CPU | `[Estática OK / Benchmark Fase 2]` |
| **LLM Director / TrimRefiner / Reviewer** | Vertex AI (Gemini 4 Flash/Hydrogen) | Google AI Pro / Antigravity + escalera `clipmint` (Gemini -> Groq -> OpenRouter) | Google AI Pro / Antigravity + escalera `clipmint` | `[Estática OK]` |
| **Precisión / Atención GPU** | `bf16` / `fp16` / `flash_attention_2` o `sdpa` | **Estricto `float16` / `int8` + `sdpa`** *(prohibido `bf16`, `fp8`, `FA2`)* | CPU `int8` / `fp32` | `[Estática Verificada]` |
| **Voz / Cold Opens** | **Shure SM58** (local) / Vertex Gemini TTS | **Shure SM58** (local) / `VoxCPM2` (`fp16`, ~8 GB VRAM) | **Shure SM58** (primario) / `kyutai/pocket-tts` (CPU 5–9x RT) | `[Pendiente Runtime Fase 2]` |
| **Render Video + `libass` Karaoke** | `FFmpeg` **`h264_nvenc`** (8ª gen Ada, AV1/H264 ultrarrápido) | `FFmpeg` **`h264_nvenc`** (Turing) + bus Google Drive 5 TB | `FFmpeg` **`h264_qsv`** (`format=nv12,hwupload`) → fallback **`libx264`** (`-preset veryfast`) | `[Validar h264_qsv Fase 2]` |

> **Conclusión arquitectónica clave:** Gracias a `parakeet-tdt-0.6b-v3` (`onnx-asr`) + `cutawan` (ONNX) + `kyutai/pocket-tts` (CPU) + `FFmpeg` (`h264_qsv`/`libx264`), **el Core v1 funciona incluso 100% offline en la HP 250 G8** si se terminan los USD 30 de GCP *y* se agotan las 200 CU mensuales de Colab T4.

---

## 4. Recomendación Crítica del Modo Inicial Único (Sprint 72 Horas -> 10 Clips Reales)

### 4.1 Veredicto Co-CTO: Arrancar con **MODO A — Clipping de Podcasts / Entrevistas en Inglés (Nicho Tech / IA / Business / Creator Economy)**

**¿Por qué NO arrancar el sprint de 72 horas con Modo B (GTA VI), pese a que GTA VI salió el 19/11/2026?**

| Criterio (72 horas desde 0 líneas de código) | Modo A: Podcasts / Entrevistas EN (Bounties + YPP Tier-1) | Modo B: Streamers GTA VI EN (Bounties Whop/Vyro) |
| :--- | :--- | :--- |
| **Riesgo de bloqueo DMCA / Content ID** | **Muy bajo** (diálogo hablado limpio; sin radios ni música de fondo de juego). | **Crítico hoy (22/11/2026):** YouTube aplica Content ID/bloqueos en GTA VI incluso con *Mute Licensed Music* activado (menús, clubes, DJs) + riesgo de strike por desnudez accidental. |
| **Complejidad técnica para llegar al Clip #1** | **Lineal y determinista:** YouTube 16:9 estable, 1–2 hablantes fijos, 0 variación de HUD, 0 bypass Cloudflare de Kick. | **Alta fricción en día 1:** Calibración distinta de webcam/HUD por cada streamer, minería de chat Twitch/Kick (`curl_cffi`), filtro DMCA de audio, sincronización Multi-POV. |
| **Ventana de monetización y competencia** | **Sostenible y repetible:** Campañas CPM recurrentes en Whop/Clipping.net/Vyro de podcasts/creators + puente directo a Long-Form YouTube Tier-1 (RPM alto) antes del **1/02/2027** (71 días para grandfathering YPP con 4.000h/10M en vez de 8.000h/20M). | **Ultra-saturada en día 3:** Miles de clippers ya compiten sobre los mismos VODs de Kai Cenat/Clix y queman los pools en horas mientras nosotros todavía armamos el repo. |
| **Reutilización del código en Semana 2** | **~85% reutilizable para Modo B** (mismo motor de ingesta, `parakeet-tdt`, `Director`/`TrimRefiner`/`Reviewer`, `libass` karaoke, safe-zones móvil, `compute_profile`). | Construir parches específicos de GTA VI primero ensucia el core antes de validar el pipeline base. |

**Decisión recomendada:** Ejecutar **Modo A (Podcasts/Entrevistas en inglés)** en las primeras 72 horas hasta publicar y medir **10 clips reales** (distribuyendo el mismo master 9:16 en YouTube Shorts, TikTok y Reels, priorizando podcasts con bounties CPM activos en Whop Content Rewards / Clipping.net / Vyro y verificación humana previa de cláusulas sin geo-lock excluyente).  
**Cláusula táctica de escape (solo si decidís apostar a GTA VI esta semana):** Si preferís capturar la ola de GTA VI de inmediato, hacerlo en **Modo B-Lite** restringido exclusivamente a **VODs de YouTube/Twitch verificados sin música licenciada**, usando un único streamer con posición fija de cámara (1 preset de crop) y dejando Multi-POV + Kick.com + separación vocal Demucs fuera de las 72h. **Mi voto técnico firme sigue siendo Modo A primero (72h) -> Modo B en Semana 2** (cuando el ruido de lanzamiento decanta y GTA VI Online/Roleplay sostiene bounties por meses).

### 4.2 Frontera Estricta: Core v1 (72h) vs. Fase 2+ Congelada

- **CORE v1 (Desbloqueado para llegar a 10 clips reales):**
  1. Ingesta parcial por ventana temporal (`yt-dlp` + Deno + `cookies.txt` en `local_hp`).
  2. Transcripción + timestamps palabra a palabra (`parakeet-tdt-0.6b-v3` vía `onnx-asr` `int8`; fallback `faster-whisper`).
  3. Anotación ligera de picos/pausas (patrón `ClipKaro`) + detección de bloques argumentativos autocontenidos abriendo en el *hot take* (`Director` + `TrimRefiner` con `EditPlan` Pydantic, patrón `agentic-video-editor`).
  4. Encuadre 9:16 limpio: 1 hablante (UltraFace + LR-ASD ONNX con *deadband*, patrón `cutawan`) o 2 hablantes (split-screen vertical con subtítulos en la costura central).
  5. Subtítulos karaoke en `pysubs2` + `libass` (con `cwd` relativo) + Cold Open humano opcional de 3s con Shure SM58 + QA visual con `ffmpeg-mcp-lite` (4–6 frames WebP revisados por `Reviewer`) + chequeo manual de safe-zones en **Samsung Galaxy A36**.
  6. Auditoría manual/semi-automática de 1–2 campañas CPM reales (burn rate entre 2 lecturas + cláusulas trampa) con compuerta humana.
- **CONGELADO PARA FASE 2+ (Prohibido tocar antes del Clip #10):**
  - VFX GPU (`SAM 3.1`, `MatAnyone 2`, `Depth Anything 3`, `HunyuanVideo-Foley`).
  - Modo C Editorial / `heygen-com/hyperframes` (HTML/GSAP->MP4).
  - Scout automatizado con `Playwright MCP` (scraping autónomo).
  - Memoria empírica vectorial en **Qdrant Híbrido + CRAG** (activar al superar 100 clips propios con telemetría).
  - `BUT-FIT/Pandemonium`, `Qwen3-ASR`, doblaje/clonación masiva y Multi-POV sincronizado.

---

## 5. Estrategia de Conocimiento (Markdown + Prompt Caching vs. Qdrant) y Top 5 Riesgos de Runtime

### 5.1 Validación Crítica: Markdown Destilado (v1) vs. Qdrant Híbrido Empírico (Fase 2)

**Veredicto: La hipótesis del brief es TÉCNICAMENTE CORRECTA según benchmarks 2026 (CAG vs. RAG en bases pequeñas), con 2 condiciones operativas.**

- **Por qué gana Markdown + Prompt Caching (CAG) en v1:** Con una base estática/semi-estática pequeña (<50 documentos, destilados a `docs/style_bible.md` y `docs/marketing_playbook.md` de <= 10–12 KB cada uno, ~2.5K–3.5K tokens totales), inyectar Markdown estructurado limpio en contexto cacheado logra **mayor precisión que RAG** (elimina por completo errores de *chunking* que rompen tablas/reglas y fallos de retrieval), **0 ms de latencia de búsqueda**, ~75%–90% de descuento en tokens de entrada en Vertex AI/Gemini y **0 horas de ingeniería de pipelines de ingesta**. Además evita envenenar el contexto con flags obsoletos de CLIs almacenados en libros/notas viejas de `.knowledge` (para flags actuales manda `context7`).
- **Abogado del Diablo (Condiciones donde CAG falla y Qdrant Híbrido gana):**
  1. *Invalidación de caché:* Si modificás `style_bible.md` antes de cada clip o hacés llamadas espaciadas horas entre sí (caché frío), perdés el descuento de Prompt Caching. **Regla:** Congelar los Markdown durante cada lote de producción y mantenerlos ultra-compactos.
  2. *Punto de inflexión en Fase 2 (>100 clips propios con métricas reales):* Un registro empírico de clips (*hook*, retención a los 3s, AVD%, plataforma, RPM/bounty, nicho) muta cada día y exige consultas filtradas (`plataforma == "shorts" AND retencion_3s >= 75%`) combinando similitud semántica (Dense) + coincidencia léxica exacta de nombres/juegos/términos (Sparse BM25 + RRF) + verificación CRAG. Ahí el Markdown estático deja de escalar y **Qdrant en modo Híbrido** es objetivamente superior.

### 5.2 Top 5 Riesgos de Runtime (Prioridad de Mitigación en Fase 2)

| # | Riesgo de Runtime | Impacto | Probabilidad | Estado Actual | Mitigación Concreta desde Día 1 |
| :---: | :--- | :---: | :---: | :---: | :--- |
| **1** | **Bloqueo de `yt-dlp` en 2026 (falta de Deno EJS / `403` sin PO Token en GCP / error DPAPI en Windows)** | Crítico (frena ingesta) | **Alta** | `[Pendiente Runtime Fase 2]` | Instalar **Deno v2.3+** en `PATH`, correr descargas parciales desde IP residencial (`local_hp`), usar `cookies.txt` exportado desde Firefox/incógnito cerrado y `curl_cffi` para Kick. |
| **2** | **Incompatibilidad silenciosa T4 `sm_75` vs. L4 `sm_89` (`bfloat16`, `fp8`, `flash_attention_2`)** | Alto (rompe capa USD 0) | **Alta** | `[Validado Teóricamente]` | Centralizar `dtype` y `attn_implementation` en `compute_profile` (`fp16`/`int8` + `sdpa` en `colab_t4`), prohibir dependencias sin fallback ONNX/CPU en el Core v1 y probar un smoke test en T4 antes de agotar los USD 30. |
| **3** | **Fallo de `FFmpeg` + `libass` + `h264_qsv` en HP 250 G8 Windows 10 (rutas, `nv12`, drivers Intel, `-c copy`)** | Alto (frena render local) | **Alta** | `[Pendiente Runtime Fase 2]` | Validar `h264_qsv` + `subtitles=subs.ass,format=nv12,hwupload` con `cwd` relativo en Fase 2; actualizar driver Intel DCH oficial; fallback automático a `libx264`; prohibir `-c copy` al inicio del recorte. |
| **4** | **Fuga silenciosa de los USD 30 de GCP (VM encendida, disco SSD ocioso o egress por VODs)** | Alto (quema el acelerador) | **Media** | `[Diseño Preventivo]` | Watchdog de auto-apagado a 12 min de inactividad, red Standard Tier (200 GiB gratis), sin Cloud NAT/IP estática, `pd-standard` temporal o destrucción de VM, alertas 50%/80%/95% y prohibición estricta de bajar VODs completos en GCP. |
| **5** | **Contaminación legal/plataforma (pesos `CC-BY-NC`, DMCA en música ambiental GTA VI, skills externas con exfiltración)** | Crítico (desmonetización / seguridad) | **Media-Alta** | `[Auditado Estáticamente]` | Lista blanca de licencias comerciales (rechazados `OmniVoice`, `MatAnyone 2`, `DA3-Large/Giant`), gate humano anti-DMCA, cero instalación ciega de skills públicas (`Read-the-Whole-File` + destilación a `.agents/skills/`). |

---

## Decisiones Requeridas Antes de Pasar a Fase 2

Para habilitar la **Fase 2** (escaneo con lista blanca de la HP 250 G8, verificación runtime de `h264_qsv`/Deno/`yt-dlp`, clonación de la terna en `_references/` y destilación única de solo lectura de `.knowledge` + Qdrant hacia `docs/style_bible.md` y `docs/marketing_playbook.md`), confirmo que me detengo aquí y solicito tu validación explícita sobre estos 3 puntos:

1. **Modo Inicial único (72 horas):** ¿Confirmás **Modo A (Podcasts / Entrevistas en inglés)** como corte vertical MVP para los primeros 10 clips, o preferís activar la cláusula **Modo B-Lite (GTA VI en inglés)** aceptando el riesgo operativo de DMCA/HUD?
2. **Terna final de `_references/`:** ¿Aprobás los 4 repositorios (`agentic-video-editor`, `cutawan`, `ClipKaro`, `clipmint`) reemplazando `openshorts` por `cutawan` (ONNX LR-ASD)?
3. **Tope de MCPs en v1:** ¿Aprobás arrancar con **2 MCPs activos** (`context7` + `ffmpeg-mcp-lite`) y CLI Python en Terminal Sandbox, dejando `Playwright MCP` y `Qdrant MCP` congelados hasta post-10 / post-100 clips?
