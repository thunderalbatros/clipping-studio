# AGENTS.md — Clipping Studio (Constitución Operativa v3.1)

## 1. Misión y Principios
Estudio automatizado `9:16` (`1080x1920@60`) y `16:9` anti-AI slop.
- **Modo v1:** GTA VI en inglés (Argentina -> Tier-1): Bounties CPM (5 redes) + Puente Shorts -> YouTube Long-Form `8+ min`.
- **Integrate-over-Build:** Preferir OSS comercial probado (`auto-editor`, `rembg`+`birefnet-general-lite` MIT, `sqlite-vec`, `fastembed`, Chrome Headless) sobre código artesanal.
- **Caveman + FinOps:** Respuesta primero, cero relleno, preservar números/rutas/negaciones. Buscar antes de leer; usar `rag query` en vez de cargar corpus.

## 2. Plano Dev vs. Plano Runtime
- **Dev (IDE):** Arquitectura, `src/`, tests, biblias en `docs/` y QA visual (`ffmpeg-mcp-lite`). Prohibido copiar/importar `_references/`.
- **Runtime (CLI `.venv` Py3.12):** Ingesta parcial (`yt-dlp`+`deno`+`curl_cffi`), ASR (`parakeet-tdt-0.6b-v3`), `UltraFace`+`LR-ASD`, `YAMNet`, `Auto-Editor`, RAG federado, `EditPlan` Pydantic, `h264_qsv`/`libx264`, `DesignEngine` (`cover_9_16`+`thumbnail_1280x720`).

## 3. Equipo (7 Skills bajo demanda en `.agents/skills/`)
1. `marketing-strategist`: Hooks `0-3s`, SEO 5 redes (YT Shorts, IG/Trial Reels, TikTok, FB, X `75x`), puente Long-Form.
2. `av-director`: Murch *Rule of Six*, *Synchresis* `0.25s`, *Seamless Loop*, split `webcam_position: top`, *punch-in* `1.12x`, `auto-editor`.
3. `media-engineer`: Py3.12 determinista, ONNX `resource_guard`, FFmpeg/libass, RAG `sqlite-vec`, Live Sniper.
4. `retention-critic`: QA 4-6 frames safe-zones 5 redes, `-14 LUFS`, anti-DMCA, `Squint 160px`, saliency, Caja Segura 4-redes.
5. `viral-intelligence-analyst`: `/outliers`+`/steal` FFmpeg -> `context/viral_style_dna.json`.
6. `bounty-and-distribution-ops`: Auditoría CPM (burn 20-80%, geo-lock), HITL anti-ban, telemetría.
7. `visual-packaging-designer`: Covers `9:16` (Método C 2 capas), thumbnails `16:9` nativos, `BiRefNet-lite`, APCA `>=75`, saliency FFT.

### Orquestación Determinista y Jerarquía de Veto
- **Flujo:** `bounty-and-distribution-ops` (gate CPM/burn/geo) -> `viral-intelligence-analyst` (outliers/DNA) -> `media-engineer` (ingesta/ASR/YAMNet/LR-ASD) -> `av-director` + `marketing-strategist` -> `visual-packaging-designer` (pre-`libass`) -> `retention-critic` (QA) -> `bounty-and-distribution-ops` (Kit HITL + telemetría).
- **Veto duro:** `retention-critic` veta a `av-director` y `visual-packaging-designer` (`max_retries=1`); `bounty-and-distribution-ops` bloquea producción para campañas sin elegibilidad CPM/burn/geo.
- **RAG explícito:** `bounty-and-distribution-ops` consulta `src/rag_engine.py` (`domain="bounties_dmca"`); `retention-critic` consulta `src/rag_engine.py` (`domain="design_ctr"` / `"episodic_memory"`).

## 4. Verificación, Cómputo y Memoria
- **Etiqueta obligatoria:** `[ESTÁTICO]` (código/tests) vs `[RUNTIME REAL]` (exit `0`, `ffprobe`, inspección visual/API).
- **`COMPUTE_PROFILE`:** `gcp_spot_l4` (`sm_89`, USD 30, watchdog 12m) -> `colab_t4` (`sm_75`, solo `fp16`/`int8`+`sdpa`) -> `local_hp` (HP 250 G8, `h264_qsv`, CPU ONNX, `$0/mes`).
- **Memoria/MCPs:** `runs/studio.db` (`WAL`+`FTS5`+`sqlite-vec`) + puente `?mode=ro&immutable=1` a `Omni-RAG v6`. Máx. 4 MCPs livianos; Qdrant solo lectura; sin `.agents/workflows/`.
