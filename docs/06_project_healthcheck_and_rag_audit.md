# Checkpoint de Auditoría Profunda y Healthcheck Integral — `HEAD 371fd68`

> **Fecha:** 2026-11-23 | **Commit auditado:** `371fd68` | **Modo:** Solo lectura absoluta (cero modificaciones en `src/`, `evals/`, `tests/`, `.agents/`, `config/`, `.venv/`, repos externos ni Qdrant Cloud).  
> **Política de evidencia:** Separación estricta entre `[ESTÁTICO]` (código, tests, linters, schemas) y `[RUNTIME REAL]` (ejecución real, `studio.db`, salidas `runs/`, inspección visual de frames/covers, consultas vivas a SQLite/Omni-RAG/Qdrant).

---

## Resumen de Semáforo Real

| Dimensión | `[ESTÁTICO]` | `[RUNTIME REAL]` | Veredicto honesto |
| :--- | :---: | :---: | :--- |
| **Core Pipeline 9:16 (Twitch -> Render -> QA)** | 🟢 `38/38 pytest`, `ruff 0` | 🟡 3 clips aprobados | Funciona E2E en Twitch EN (`jynxzi`, `thedevildoggamer`, `joezmcfly`), pero con 4 bugs visuales P0 abiertos. |
| **RAG Híbrido + Federado Omni-RAG v6** | 🟢 `115` chunks + `vec0` | 🟢 3/3 queries OK | Operativo, `0` `-wal`/`-shm` en repo externo. Brechas: `design_engine` no lo consulta, `curator` usa `use_vectors=False` y `knowledge_chunks` (40 filas) es huérfano. |
| **DesignEngine (Covers 9:16 + Thumbs 16:9)** | 🟢 `6/6` tests | 🔴 Bug P0 en 9:16 | `1280x720` 16:9 sale limpio (`Montserrat Black` + `BiRefNet-lite`), pero `cover_9_16.jpg` extrae frame **post-subtítulos `libass`** (doble texto). |
| **Bounties CPM (`campaigns`)** | 🟢 `4/4` tests | 🔴 8/8 filas inválidas | Las 8 campañas en `runs/studio.db` son snippets web (blogs, YouTube, App Store), **no** briefs CPM ejecutables. |
| **Live Sniper / Voice / Analytics / ES** | 🟢 Tests unitarios OK | 🔴 Sin E2E real | `live_sniper` solo probó canal offline (`15s`), `voice_engine` y `analytics` sin corridas reales, **0 corridas en español (`ES`)**. |
| **Harness Agéntico y Typecheck** | 🟡 `ruff 0`, `ty` falla | 🟡 Solo hook `rtk` | `rtk_pretool_hook.py` activo y verificado en vivo; faltan guard de seguridad `PreToolUse`, `ty` limpio (332 líneas de error), grounding determinista y Golden Eval Set. |

---

## BLOQUE 1 — Auditoría Real del RAG Ya Implementado (`[ESTÁTICO]` + `[RUNTIME REAL]`)

### 1.1 Arquitectura Activa en Runtime (`HEAD 371fd68`)

1. **Prefijo Cacheado de Vertex AI (`src/llm_router.py` -> `build_cached_system_instruction`, L60–L103):**
   - Concatena 8 archivos estáticos + reglas invariantes en memoria (`_STATIC_BIBLE_PREFIX`) para activar **Implicit Context Caching** de Gemini (`>6,144` tokens).
   - **Tamaño medido `[RUNTIME REAL]`:** **`42,524` chars (~`10,631` tokens est., `5,196` palabras)**.
   - Desglose exacto del prefijo:
     - `docs/style_bible.md`: `6,071 B` (~`1,492` tok)
     - `docs/marketing_playbook.md`: `6,157 B` (~`1,517` tok)
     - `docs/design_bible.md`: `5,909 B` (~`1,456` tok)
     - `context/platform_algorithms_index.json`: `6,880 B` (~`1,720` tok)
     - `context/gta6_living_brief.md`: `3,279 B` (~`803` tok)
     - `context/gta6_lore_index.json`: `7,052 B` (~`1,763` tok)
     - `context/golden_references.json`: `5,945 B` (~`1,436` tok)
     - `context/viral_style_dna.json`: `1,026 B` (~`245` tok)
   - Invocado en `src/curator.py` en 4 puntos: Pass-1 (`L285`), BlindCritic (`L394`), Pass-2 refine (`L442`) y Packaging (`L587`).

2. **RAG Local Híbrido (`src/rag_engine.py` -> `query_rag` L709, `build_curator_rag_context` L862, `format_rag_context` L840):**
   - Motor: SQLite `rag_chunks` (**`115` filas**) + `rag_chunks_fts` (BM25 `unicode61`) + `rag_chunks_vec` (`vec0` `float[384]`, **`115` vectores** con `fastembed` `BAAI/bge-small-en-v1.5`), fusión **RRF ($k=60$)**, contexto acotado a `<=550` tokens (`2,200` chars).
   - **Hallazgo crítico #1 (`[ESTÁTICO]`):** `src/curator.py` llama `build_curator_rag_context()` en Pass-2 (`L451`, `stage="pass2"`) y Packaging (`L559`, `stage="packaging"`), pero la firma en `src/rag_engine.py:868` tiene **`use_vectors: bool = False`** por defecto -> **en producción `curator.py` solo ejecuta BM25 (sin búsqueda vectorial `sqlite-vec`)**.
   - **Hallazgo crítico #2 (`[ESTÁTICO]`):** `src/design_engine.py` **NO importa ni consulta `src/rag_engine.py` en ningún punto**. Todo su comportamiento CTR/APCA/Safe-Box está hardcodeado en Python/HTML.

3. **3 Consultas Reales de Solo Lectura (`[RUNTIME REAL]`):**

| Query | Parámetros | Latencia | Hits | Top Result & Observación |
| :--- | :--- | :---: | :---: | :--- |
| `design_ctr` | `domain="design_ctr"`, `top_k=4`, `use_vectors=True` | `3,841.1 ms` *(cold-start ONNX `bge-small`)* | `4` | `visual-packaging-designer/SKILL.md` (`rrf=0.0325`, `bm25=1, vec=2`) + `docs/design_bible.md` (`vec=1`). |
| `episodic_memory` | `domain="episodic_memory"`, `top_k=4`, `use_vectors=True` | **`36.2 ms`** *(warm)* | `4` | Recupera 3 `BlindCritic APPROVED_CRITIC` (`thedevildoggamer`, `jynxzi`, `joezmcfly`) + `studio.db#clips` (`WINNER`). |
| `--federated` | `federated=True`, `top_k=4` (vs `web-software-factory/.cache`) | `2,771.6 ms` | `4` | Mezcla `studio_local` + `omni_rag_library` (`hooked_habit_forming.md`) + `omni_rag_hot`. **Ruido detectado:** `"hook"` matcheó `React Hook Form` en `omni_rag_hot`. |

- **Verificación de aislamiento externo (`[RUNTIME REAL]`):** `web-software-factory/.cache/` inspeccionado antes y después de la query federada: `wal/shm before: []`, `wal/shm after: []`, `new_files: set()`. El URI `?mode=ro&immutable=1` (`src/rag_engine.py:668`) cumple 100% la Regla 01.

---

### 1.2 Anomalía de Esquema en `runs/studio.db`: `knowledge_chunks` vs. `rag_chunks`

| Tabla | Filas | Definida en | Contenido actual | Estado |
| :--- | :---: | :--- | :--- | :--- |
| `knowledge_chunks` (+ `_fts`) | `40` | `src/db.py:114` (`upsert_knowledge_chunks`, `search_knowledge_fts`) | Solo 4 fuentes: `algorithms_and_virality_bible.md` (21), `marketing_playbook.md` (7), `platform_algorithms_index.json` (6), `style_bible.md` (6). | **Huérfana (legacy v2.5).** Cero callers activos en `src/` salvo `db.py` stats (`L615`) y un evento viejo `algorithms_corpus_ingested` en `telemetry`. |
| `rag_chunks` (+ `_fts`, `_vec`) | `115` | `src/rag_engine.py:88` (`rebuild_rag_index`, `query_rag`) | Cubre las 6 brechas: `shortform_craft` (44), `gta6_streamers` (20), `bounties_dmca` (20), `media_engineering` (16), `design_ctr` (10), `episodic_memory` (5). | **Activa (v3.1).** 100% superconjunto de `knowledge_chunks` + vectores `float[384]`. |

**Conclusión:** `knowledge_chunks` y `knowledge_chunks_fts` son restos huérfanos de v2.5 duplicados; deben deprecarse y dropearse en migración limpia post-checkpoint (no tocados aquí por regla de solo lectura).

---

### 1.3 Catálogo de Qdrant Cloud (`[RUNTIME REAL]`, Solo Lectura)

- **Credenciales en `.env`:** `QDRANT_HOST: PRESENTE`, `QDRANT_API_KEY: PRESENTE`.
- **Anomalía de configuración:** `.env` declara `QDRANT_COLLECTION_CLIPPING_V2=clipping_studio_empirical`, pero esa colección **no existe** en Qdrant Cloud.

| Colección en Qdrant Cloud | Puntos | Contenido real detectado | Relevancia para Clipping Studio |
| :--- | :---: | :--- | :--- |
| `centinela_knowledge` | `3,624` | Live streaming (Kick/Twitch Chat streams, Kleinberg-Hawkes bursts), GCP FinOps SOTA, `rtk`/Token Reduction, Agent Harness. | **Alta** para `live_sniper`, FinOps GCP y Harness. |
| `hunthub_omni_rag_v5` | `8,021` | UE 5.4/5.8, USD/MaterialX, Blender, ComfyUI, MMAudio, *Visual Consciousness / Plates & Palettes*. | **Baja–Media** (solo teoría visual/colorimetría y audio foley). |
| `yendo_obsidian_vault` | `4,776` (`927` fuentes, `103` libros) | **Psicología/Conducta/Gamificación:** *Hooked*, *Nudge*, *Thinking, Fast and Slow*, *Flow*, *The Power of Habit*, *Predictably Irrational*, *The Paradox of Choice*, *Emotional Design*, *Actionable Gamification*, *For the Win*, *Laws of UX*. **Diseño/UI:** *Refactoring UI*, *Practical UI*, *Animation at Work*, *The Art of Game Design*. **Harness/AI:** *CANON HARNESS ENGINEERING 2026*, *LLMOps*. Más `816` chunks `video_masterclass` (mayormente WWDC/UI/React Native). | **Media–Alta** en psicología de atención/hábito/UI; **faltan** libros dedicados a edición/retención/packaging de video. |

- **Qué falta destilar (o ya está solo en resúmenes breves locales):** No existen en Qdrant libros completos de edición/retención/packaging (*In the Blink of an Eye* — Walter Murch, *Audio-Vision* — Michel Chion, *The YouTube Formula* — Derral Eves, *Hook Point* — Brendan Kane, *Contagious* — Jonah Berger, *Made to Stick* — Heath). Hoy solo existen sus principios destilados en `docs/algorithms_and_virality_bible.md` y `docs/shortform_and_editing_bible.md`.

---

### 1.4 Mapa de Consumo por Skill (7 Skills) y Peso de Reglas

- **Peso fijo siempre cargado (`AGENTS.md` + `.agents/rules/*.md`):** **`6,108` bytes (~`1,527` tokens)** — muy saludable y compacto (`AGENTS.md`: 2,496 B; `01_security`: 1,008 B; `02_finops`: 851 B; `03_anti_dmca`: 815 B; `04_windows`: 938 B).
- **Consumo por Skill (`.agents/skills/*/SKILL.md`):**

| Skill | Peso (~tok) | Fuentes explícitas que consume |
| :--- | :---: | :--- |
| `av-director` | ~902 | `docs/style_bible.md`, `docs/shortform_and_editing_bible.md`, `context/gta6_living_brief.md`, `context/streamer_profiles.json`, `context/viral_style_dna.json`, `rag query` |
| `bounty-and-distribution-ops` | ~555 | `runs/studio.db` (`campaigns`) — **no** referencia `marketing_playbook.md` ni `rag query` |
| `marketing-strategist` | ~919 | `docs/marketing_playbook.md`, `docs/algorithms_and_virality_bible.md`, `context/streamer_profiles.json` |
| `media-engineer` | ~995 | `docs/engineering_patterns.md`, `docs/shortform_and_editing_bible.md`, `runs/studio.db`, `src/design_engine.py`, `src/rag_engine.py` |
| `retention-critic` | ~924 | `evals/media_check.py`, `runs/studio.db` — **no** referencia `design_bible.md` ni `rag query` |
| `viral-intelligence-analyst` | ~555 | `context/golden_references.json`, `context/viral_style_dna.json` |
| `visual-packaging-designer` | ~899 | `docs/design_bible.md`, `src/design_engine.py`, `rag query --federated` (Omni-RAG) |

- **Información operativa/de dominio que FALTA hoy (sin ingestar docs volátiles de librerías):**
  1. **Briefs reales verificados de Bounties CPM** (reglas exactas de Whop Content Rewards, Clipping.net, Vyro, ClipBoom: payout mínimo, ventanas de aprobación, requisitos de audio/watermark/hashtags, exclusiones geo Argentina).
  2. **Memoria episódica real post-publicación** (`publish_metrics` = 0 filas reales en `telemetry`: CTR real, retención 3s/30s, AVD, RPM/CPM por red).
  3. **Glosario/slang y perfiles de creadores hispanos (`ES`)** validados en runtime (hoy solo hay 4 perfiles teóricos en `streamers.yaml`: `westcol`, `ibai`, `elrubius`, `illojuan` sin corridas).
  4. **Reglas de campañas musicales (Sound Campaigns)** y criterios de cuándo la música es *requisito* vs *riesgo DMCA*.
  5. **Playbook de chromakey / layouts de webcam transparentes** (Jynxzi, Kai Cenat, CaseOh, etc.).

---

## BLOQUE 2 — Verificación de Calidad Visual, Módulos v2.5/v3.4 y las 8 Campañas (`[RUNTIME REAL]`)

### 2.1 Mapa de Madurez de `src/`

> Baseline estático verificado: `pytest` = **`38 passed in 14.64s`** (nota: `0` mocks con `MagicMock`/`monkeypatch` en los tests revisados; usan fixtures sintéticos/temporales y SQLite real), `ruff check` = **`0` errores**, `ty check src` = **`exit 1`** (`332` líneas de errores de tipado).

| Clasificación | Módulos `src/` | Evidencia verificable |
| :--- | :--- | :--- |
| **`VERIFICADO_E2E_RUNTIME`** | `miner`, `transcriber`, `curator`, `renderer`, `callout`, `captions`, `rag_engine`, `trends`, `style_analyzer`, `compiler`, `design_engine` *(parcial)* | 3 clips Twitch EN renderizados y aprobados por `media_check.py` (`twitch_gta6_e2e_01`, `e2e_02`, `twitch_jynxzi_v2_01`), `compilation_16_9_master.mp4` + `thumbnail_1280x720.jpg`, `cover_9_16.jpg` (con bug de subtítulos), `trends` (12 YT + 6 Kick + 12 TikTok), `style_analyzer` (10 updates), `rag_engine` (3/3 queries vivas OK). |
| **`SOLO_TESTS_UNITARIOS_MOCK`** | `speaker_guard`, `bounties`, `live_sniper`, `analytics`, `voice_engine` | `bounties` corrió pero solo raspó 8 páginas web irrelevantes; `live_sniper` solo corrió `15.25s` contra `twitch:jynxzi` **offline** (`messages_seen=0`); `speaker_guard` (UltraFace/LR-ASD) sin corrida verificada con métricas por clip en `studio.db`; `analytics` y `voice_engine` solo ejercitados en `test_longform_voice_analytics.py` (`3` tests sintéticos). |
| **`SIN_PROBAR`** | *(ninguno de los 16 clave sin al menos test unitario)* | **Brechas de cobertura runtime:** 0 corridas en español (`ES`), 0 ejecuciones en `colab_t4` o `gcp_spot_l4`, 0 publicaciones reales medidas con `analytics.py`. |

---

### 2.2 Inspección de las `8` Campañas en `runs/studio.db` (`campaigns`)

Todas las filas tienen `verdict = PENDING_HITL`, `burn_rate_pct = 0.0`, `geo_lock = AR_ELIGIBLE`, `voice_policy = none`:

| `campaign_id` | Plataforma | Título almacenado | CPM USD | ¿Real o Ruido Web? |
| :--- | :--- | :--- | :---: | :--- |
| `whop_2cf655602a` | `whop` | `Articles` | `$0.00` | ❌ **Ruido web** (artículo de blog *"How to Run a GTA 6 Clipping Campaign"*) |
| `whop_abc445c519` | `whop` | `Content Rewards` | `$1.50` | ⚠️ **Snippet parcial** (página general `/join-content-rewards-hub/` con mención a *GTA VI Lucia vs Fanvue AI Lucia*, sin reglas ni burn-rate real) |
| `clipping_net_ecea1d1d23` | `clipping_net` | `The Original & Largest Clipping Network` | `$0.00` | ❌ **Ruido web** (homepage de `clipping.net`) |
| `clipping_net_94ec81012b` | `clipping_net` | `Clipping Campaigns for Brands` | `$0.00` | ❌ **Ruido web** (landing `/brands` de `clipping.net`) |
| `vyro_6cae4acd72` | `vyro` | `I Tried Every Clipping Platform...` | `$0.00` | ❌ **Ruido web** (video de YouTube `watch?v=eaYPT1Remus`) |
| `vyro_91b1d35c03` | `vyro` | `Best Clipping Platforms 2026...` | `$0.00` | ❌ **Ruido web** (blog `ssemble.com`) |
| `clipboom_df0b8926a5` | `clipboom` | `ClipBoom: Gana con tus clips - Google Play` | `$0.00` | ❌ **Ruido web** (ficha de Google Play Store) |
| `clipboom_d6921b29ee` | `clipboom` | `ClipBoom - App Store - Apple` | `$0.00` | ❌ **Ruido web** (ficha de Apple App Store) |

**Diagnóstico:** `src/bounties.py` hoy hace búsqueda web genérica (Serper) y guarda cualquier URL que devuelva resultados; **0 de 8 campañas son ejecutables** para producción comercial.

---

### 2.3 Auditoría Visual y de Código: Subtítulos en Covers, Bugs v2.5/v3.4 y Miner

1. **Bug de subtítulos en `cover_9_16.jpg` — CONFIRMADO `[ESTÁTICO]` + `[RUNTIME REAL]`:**
   - **Causa raíz exacta:** En `src/cli.py:85-90`, `render_editorial_cover_9_16(video_or_img=out_mp4, ...)` recibe `out_mp4` (`clip_02_master_9_16.mp4`), el cual ya pasó por `ass=subs.ass:fontsdir=fonts` en `src/renderer.py:767`.
   - **Verificación visual (`runs/twitch_jynxzi_v2_01/cover_9_16_cover_clean.jpg` y `cover_9_16.jpg`):** Ambas imágenes tienen quemado el subtítulo `"ALRIGHT, DONNIE."` justo debajo del titular `"FIRE SPREADS FAST"`.
   - **Dimensiones y tipografía (`[RUNTIME REAL]`):**
     - `cover_9_16.jpg`: exactamente `(1080, 1920)` (`423,868 B`), `Montserrat Black` verificado visualmente.
     - `runs/test_compile_16_9/thumbnail_1280x720.jpg`: exactamente `(1280, 720)` (`254,777 B`), `Montserrat Black` + cutout `BiRefNet-lite` con rim-light cálido y **cero subtítulos** (porque extrae del `slice` 16:9 crudo pre-render).

2. **Estado de los 3 bugs visuales en video v2.5/v3.4 (`[ESTÁTICO]` + `[RUNTIME REAL]` en `qa_frame_2.png` y `qa_frame_4.png`):**
   - **(a) Caja azul/cyan de `callout.py`:** ❌ **NO resuelto.** `src/callout.py:130` sigue dibujando `draw.rectangle([bx0, by0, bx1, by1], outline=(0, 229, 255, 245), width=5)`.
   - **(b) Centrado de `"WASTED"` y borrón rojo superior:** ❌ **NO resuelto.** En `runs/twitch_jynxzi_v2_01/clip_02_senior_qa/qa_frame_4.png`:
     - `"wasted"` aparece **descentrado hacia la derecha** porque `gameplay_crop_px = [356, 0, 950, 1080]` desplaza el encuadre hacia la izquierda para no solapar la webcam.
     - Detrás de la webcam en la banda superior se filtra el **borrón rojo con el fantasma `"ed"`** de `"wasted"` (`renderer.py:725`: `gblur=sigma=24` toma el mismo gameplay crop como fondo superior).
   - **(c) Recorte para streamers *chromakey* (Jynxzi):** ❌ **NO resuelto.** En `qa_frame_2.png` y `qa_frame_4.png` se confirma que Jynxzi transmite con fondo transparente/chromakey (se ve el pasto/rojo del juego detrás de él), y `renderer.py` no tiene lógica `chromakey` ni centrado de gameplay para overlays sin marco.

3. **Auditoría de `src/miner.py` (Clip-as-a-Beacon y corridas en Español):**
   - **Clip-as-a-Beacon (`6h–24h` -> `vod_offset` padre):** ❌ **NO implementado.** `miner.py` busca clips de Kick (`time_window="week"` en `/api/v2/channels/{slug}/clips`) y descarga el clip directo, o descarga VODs de Twitch usando chat peaks (`TwitchDownloaderCLI`) o audio RMS. **No existe** lógica de `vod_offset` ni ventana `6h–24h` que use clips virales de comunidad como baliza para cortar el VOD padre con contexto previo/posterior.
   - **Corridas reales en español (`ES`):** ❌ **0 corridas.** Todos los runs en `runs/` (`kick_xqc_v2_01`, `test_gta6_01`, `twitch_gta6_e2e_01`, `e2e_02`, `twitch_jynxzi_v2_01`) son de streamers en inglés (`xqc`, `thedevildoggamer`, `joezmcfly`, `jynxzi`).
   - **Hallazgo extra de entorno:** `deno` figura `FALTANTE` en `PATH` de la shell actual (verificar si está en ruta no-PATH o instalar antes de ingestar YouTube 1080p, Regla 04).

---

## BLOQUE 3 — Harness Agéntico, Reglas Transversales y Brechas P0 de Escalabilidad

### 3.1 Harness Real en `HEAD 371fd68`

| Componente del Harness | Estado en `371fd68` | Evidencia y Brecha |
| :--- | :---: | :--- |
| **`.agents/hooks.json` + `scripts/rtk_pretool_hook.py`** | 🟢 Activo (`[RUNTIME REAL]`) | Verificado en vivo al ejecutar `pytest` (reescribió `CommandLine` con `rtk 1.2.0`). Solo comprime `git`, `pytest`, `ruff`, `uv`. |
| **(a) Guard `PreToolUse` seguridad (cero escritura fuera del repo / lectura `.env`)** | ❌ **Faltante** | `.agents/hooks.json` tiene únicamente `rtk-shell-compressor` (fail-open). No bloquea `write_file`/`edit_file`/`run_command` fuera del workspace ni lectura de `.env`/`*cookies*.txt`. |
| **(b) Typecheck (`ty` / `basedpyright`)** | 🟡 **Instalado pero roto** | `ty` está instalado (`C:\Users\tomas\.local\bin\ty.EXE`), pero `ty check src` falla (`exit 1`, `332` líneas de diagnóstico, p. ej. `src/curator.py:294` varianza de `list[tuple[Path, str]]`). No está integrado en CI ni hooks. `basedpyright`/`mypy` no instalados. |
| **(c) Validación determinista de citas/timestamps (*word-index grounding*)** | ❌ **Faltante** | `src/curator.py:_checked_by_code` (L315) solo valida duración, DMCA, meta-talk y densidad; **no** verifica que `start_trim`/`end_trim` o `hook_headline` anclen en índices de palabra reales del `AnnotatedTranscript`. |
| **(d) *Golden Eval Set* offline** | ❌ **Faltante** | `evals/` contiene solo `media_check.py` (validador técnico binario de MP4). No existe dataset offline de pares `(transcript/slice -> expected EditPlan / reject)` para regresión de curaduría/hook/DMCA. |

---

### 3.2 Auditoría de Reglas Transversales en `.agents/rules/`

| Regla transversal requerida | Estado actual | Dónde vive hoy / Qué falta formalizar |
| :--- | :---: | :--- |
| **SOTA OSS comercial KS-4** | 🟢 Formalizada | `.agents/rules/01_security_and_secrets.md` (punto 4). |
| **Integrate-over-Build** | 🟡 Parcial | Solo mencionada en 1 línea de `AGENTS.md` (Sec. 1); **no** está en `.agents/rules/`. |
| **Cloud-First GPU eficiente por lotes (Colab T4 / Spot L4)** | 🟡 Parcial | `02_finops_gcp_30usd.md` cubre tope USD 30, watchdog y restricciones `sm_75`, pero **no** formaliza estrategia de *batching* GPU (agrupar N slices antes de encender L4/T4). |
| **Cero subtítulos en covers/thumbnails** | ❌ **Faltante** | No existe en `.agents/rules/` (ni validada en `evals/media_check.py` / `design_engine.py`). |
| **Evidencia runtime obligatoria (`[ESTÁTICO]` vs `[RUNTIME REAL]`)** | 🟡 Parcial | Solo en `AGENTS.md` (Sec. 4); **no** formalizada en `.agents/rules/`. |

---

### 3.3 Escalabilidad Comercial (2 Bloqueadores) + Optimizaciones GCP Pendientes

#### Bloqueador 1: Multi-Cliente / Bounties Arbitrarios (de 9 perfiles fijos -> `creators` + Auto-Profiler)
Hoy `config/streamers.yaml` y `context/streamer_profiles.json` tienen **9 streamers hardcodeados** (5 EN + 4 ES). Para aceptar cualquier bounty de Whop/Clipping.net/Vyro sin editar YAML a mano, los cambios mínimos son:
1. **Tabla `creators` en `runs/studio.db` (`src/db.py`):** `(creator_id PK, platform, handle, language, webcam_mode ['box'|'chromakey'|'none'], webcam_box_json, style_profile_json, auto_profiled_at, updated_at)`.
2. **Auto-Profiler en `src/miner.py` / `src/curator.py`:** Si `streamer_id` no existe en `creators`, en el primer slice ejecuta UltraFace + Pass-1 para inferir `webcam_box`, `webcam_mode` (detectar borde/transparencia chromakey), idioma (desde `parakeet-tdt-0.6b-v3` / metadata) y guarda automáticamente en `creators` + `rag_chunks` (`gta6_streamers`).
3. **Resolución unificada:** `curator.py` y `renderer.py` leen `creators` en `studio.db` con fallback a `config/streamers.yaml`.

#### Bloqueador 2: Política de Audio 3-Modos (`YAMNet`) para Sound Campaigns
Hoy el gate anti-DMCA es **binario** (`transcriber.py` marca `dmca_flag = True` si `music_prob_peak >= 0.45` -> `curator.py:188-201` y `325` rechazan obligatoriamente el clip y `pkg.bounty_safe = False`). Esto **bloquea el 100% de las Sound Campaigns** de Whop Content Rewards donde usar la canción patrocinada es obligatorio. Cambios mínimos:
1. **Flag CLI `--audio-policy` (`src/cli.py` + `EditPlan`/`RunState` en `src/schemas.py`):** `strict_clean` (default, comportamiento actual para YPP/GTA VI radio) | `sound_campaign` (permite música YAMNet cuando la campaña lo requiere, exigiendo que no ahogue diálogo o permitiendo overlay de track oficial) | `keep_original` (solo loguea telemetría YAMNet sin rechazo duro, p. ej. clips cortos con audio viral permitido).
2. **Gate parametrizado (`src/curator.py:185` y `L325`):** Pasar `audio_policy` a `_apply_pass1_hard_gates`, `_checked_by_code` y `generate_clip_packaging` (`bounty_safe` evalúa compatibilidad con `campaign.voice_policy` / `audio_policy`, no solo `dmca_clear`).

#### Optimizaciones GCP Pendientes (dentro del tope USD 30)

| Capacidad GCP / Cloud | Estado actual | ROI real dentro de USD 30 |
| :--- | :--- | :--- |
| **Vertex AI Batch API (50% descuento)** | No usada (solo llamadas síncronas en `llm_router.py`) | **Alto** para curaduría nocturna / backfills de >20 slices: reduce costo de `gemini-3.8-flash` un 50% (~USD 0.002/clip). |
| **Explicit Context Caching vs Implicit** | Solo Implicit (`build_cached_system_instruction`, 10.6k tok) | **Medio:** Implicit ya ahorra 90% en ráfagas cortas (`$0.075/1M`), pero en lotes espaciados >5 min pierde cache hit; Explicit TTL 60m estabiliza el ahorro en lotes de 10+ clips. |
| **Batching en Colab T4 (`sm_75`, $0/mes) antes de Spot L4** | `0` corridas en Colab T4 o Spot L4 | **Muy Alto:** Descargar ASR + `BiRefNet-lite` + `h264_nvenc` por lotes en Colab T4 (`200 CU/mes`, USD 0) preserva casi íntegros los USD 30 de GCP para Vertex AI y picos de lanzamiento GTA VI. |
| **GCS Standard Lifecycle (auto-delete 7d)** | Buckets configurados en `.env`, sin política verificada | **Medio:** Evita acumulación silenciosa de costos de almacenamiento dentro de la cuota de USD 5 GCS. |

---

## Plan Priorizado: Cierre P0 (Pre-10 Clips) vs. P1 (Post-10 Clips)

### 🔴 P0 — Parche Final Obligatorio Antes de Publicar los Primeros 10 Clips
1. **Cero subtítulos en `cover_9_16.jpg`:** Exportar frame/video limpio pre-`libass` en `renderer.py` (o extraer el frame peak antes de quemar `subs.ass`) y pasarlo a `render_editorial_cover_9_16` en `cli.py:85`.
2. **Arreglar los 3 defectos visuales de video:**
   - Eliminar el borde cyan/azul de `src/callout.py:130` (usar acento sutil o solo vignette + pill).
   - Corregir el fondo de la banda superior (`renderer.py:725`) para no mostrar texto borroso `"ed"` de `"WASTED"` (usar crop sin HUD o fondo oscuro neutro).
   - Soporte `chromakey` / centrado real de gameplay para streamers sin caja de webcam (evita `"WASTED"` desplazado a la derecha en Jynxzi).
3. **Política de audio 3-modos (`--audio-policy strict_clean | sound_campaign | keep_original`):** Desbloquea campañas musicales de Whop sin romper protección DMCA en YPP.
4. **Curación humana/real de 3–5 bounties reales en `campaigns`:** Limpiar los 8 snippets web falsos y cargar briefs reales verificados (con CPM, burn-rate y reglas reales) + validar `deno` en PATH para ingesta YouTube 1080p.
5. **Activar `use_vectors=True` en `build_curator_rag_context` y limpiar `ty check src` + guard `PreToolUse` de seguridad** (bloquear lectura `.env` y escritura fuera del repo).

### 🟡 P1 — Post-Primeros 10 Clips (Guiado por Métricas Reales)
1. **Tabla dinámica `creators` + Auto-Profiler** en `runs/studio.db` (escalar más allá de los 9 perfiles fijos).
2. **Clip-as-a-Beacon (`6h–24h` -> `vod_offset` padre)** en `src/miner.py` + primera corrida E2E en español (`ES`).
3. **Depurar tabla huérfana `knowledge_chunks`** en `studio.db`, filtrar `omni_rag_hot` en queries federadas (evitar ruido React) y conectar `design_engine` a `rag_engine` si se dinamizan plantillas.
4. **Word-index grounding determinista** en `curator.py` + **Golden Eval Set** offline en `evals/`.
5. **Batching GPU en Colab T4 + Vertex AI Batch API** y cierre de ciclo real de `analytics.py` (`publish_metrics` -> `episodic_memory`).
