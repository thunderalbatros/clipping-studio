# AGENTS.md — Clipping Studio (Constitución Operativa)

## 1. Misión y Corte Vertical Activo (72 horas -> 10 clips reales)
**Clipping Studio** es un estudio modular y automatizado de video corto vertical (9:16) y compilaciones horizontales (16:9) con estética editorial real (**anti-AI slop**).

- **Modo inicial aprobado (v1):** **Modo B-Lite (GTA VI en inglés)** orientado a monetización viable desde Argentina:
  1. **Bounties CPM** (Whop Content Rewards / Clipping.net / Vyro) distribuyendo el mismo master 9:16 en YouTube Shorts, TikTok e Instagram Reels.
  2. **Puente Shorts -> YouTube Long-Form (8+ min)** en inglés (Tier-1) antes del cambio de umbrales YPP del 1 de febrero de 2027.
- **Regla de voz:** `voice_mode: "none"` por defecto en clips de bounties CPM (nunca pisar la reacción original del streamer). `human_batch` (Shure SM58 + Mackie Onyx Artist 1.2) o `tts` (`kyutai/pocket-tts` / `openbmb/VoxCPM2`) reservados para YPP o editorial.
- **Regla anti-DMCA:** Detectar y descartar música licenciada de radio/menús/locales de GTA VI **antes** de editar o publicar.

---

## 2. Separación Estricta: Plano Dev vs. Plano Runtime

| Plano | Quién opera | Responsabilidad | Qué está PROHIBIDO |
| :--- | :--- | :--- | :--- |
| **Plano Dev (Ingeniería en IDE)** | Agentes de Antigravity IDE (`AGENTS.md`, `.agents/rules/`, `.agents/skills/`) | Diseñar arquitectura, escribir código determinista en `src/`, ejecutar tests/auditorías, destilar biblias en `docs/` y validar renders vía `ffmpeg-mcp-lite` + `context7`. | Nunca mezclar prompts ad-hoc con ejecución de producción; nunca copiar repos enteros desde `_references/`. |
| **Plano Runtime (Pipeline Python)** | CLI determinista en `.venv` (`Python 3.12`) + `FFmpeg` + `yt-dlp` + modelos ONNX/API | Ingesta parcial, ASR, detección de picos, curación estructurada (`EditPlan` Pydantic), render `h264_qsv`/`h264_nvenc`/`libx264`, verificación de frames y empaquetado. | Nunca depender de GUI manual, nunca descargar VODs de 40 GB completos, nunca ejecutar llamadas LLM sin esquema Pydantic ni fallback. |

---

## 3. Roles del Equipo de Agentes (6 Skills en `.agents/skills/`)
1. **`marketing-strategist`** — Ingeniería de hooks 0–3s, títulos/SEO Tier-1, empaquetado nativo para **5 redes** (YouTube Shorts, TikTok, Instagram Reels, Facebook Reels, X) y estrategia puente a YouTube Long-Form (8+ min).
2. **`av-director`** — Dirección narrativa y visual Senior: inicio directo en acción/reacción, split-screen `webcam_position: top` (tarjeta `<=1.8x` anclada bajo `y=180`), jump-cuts selectivos respetando *Synchresis* (`0.25s`), *punch-in zoom* RMS `1.12x`, SFX CC0 motivados y consumo de `context/viral_style_dna.json`.
3. **`media-engineer`** — Ingeniería determinista Python 3.12: `yt-dlp` + `curl_cffi` + `deno` EJS + `streamlink`, `parakeet-tdt-0.6b-v3` (`onnx-asr` `int8`), `UltraFace` + `LR-ASD` ONNX, `YAMNet` ONNX, `FFmpeg` (`h264_qsv`/`h264_nvenc`/`libx264`), `libass` (`Montserrat Black` vía `fontsdir=fonts`), *profanity bleeper* 0–7.5s, `SQLite WAL/FTS5`, `resource_guard` y *Live-Stream Sniper*.
4. **`retention-critic`** — QA multimodal y gate de salida: inspección de 4–6 frames `*_safezones.png` vía `ffmpeg-mcp-lite`, safe-zones de las **5 redes**, auditoría de audio (`-14 LUFS`, pico `<= -1.0 dBTP`), chequeo anti-DMCA, cero groserías sin bleep en 0–7s y rúbrica de 5 ejes (`max_retries=1`).
5. **`viral-intelligence-analyst`** — Ingeniería inversa de outliers virales en IG Reels, FB Reels, YT Shorts, TikTok y X (`/outliers` + `/steal` FFmpeg: cortes/min, duración óptima, `share-to-view`, `vph` -> `context/viral_style_dna.json`).
6. **`bounty-and-distribution-ops`** — Auditoría financiera/compliance de bounties CPM (`Whop Content Rewards`, `Clipping.net`, `Vyro`, `ClipBoom` -> burn rate 20%–80%, geo-lock, tabla `campaigns` en `runs/studio.db`), entregas HITL anti-ban y analítica post-publicación.

---

## 4. Verificación Estática vs. Verificación en Runtime
Todo agente debe etiquetar explícitamente sus afirmaciones:
- **`[ESTÁTICO]`**: revisión de código, esquemas Pydantic, configs, documentación o sintaxis.
- **`[RUNTIME REAL]`**: comando ejecutado y verificado con código de salida `0`, archivo real inspeccionado con `ffprobe` o respuesta real de MCP/API.
- **Regla de hierro:** Nunca declarar un módulo "listo" únicamente por validación estática si involucra `FFmpeg`, `yt-dlp`, encoders de hardware o APIs externas.

---

## 5. Degradación Elegante tras Agotar los USD 30 de GCP
Toda etapa de cómputo lee `COMPUTE_PROFILE` (`gcp_spot_l4 | colab_t4 | local_hp`):
- **`gcp_spot_l4` (Acelerador finito USD 30):** `g2-standard-4` Spot L4 (`sm_89`) + Vertex AI. Watchdog de apagado automático a los 12 min de inactividad. Al llegar a 90% de gasto, conmutar sin refactorizar código.
- **`colab_t4` (USD 0/mes, 200 CU):** NVIDIA T4 (`sm_75`). **Estricto `float16`/`int8` + `sdpa`** (prohibido `bfloat16`, `fp8` y `flash_attention_2`).
- **`local_hp` (USD 0/mes permanente):** HP 250 G8 (i5-1035G1, 16 GB RAM, Intel QuickSync **`h264_qsv` verificado en runtime**), `parakeet-tdt-0.6b-v3` (`onnx-asr` CPU `int8`), `libx264` como fallback.

---

## 6. Presupuesto de Complejidad y Restricciones de Alcance (v2.5)
- **Tope de MCPs activos en Clipping Studio:** Máximo **4 MCPs livianos** (`context7` + `ffmpeg-mcp-lite`, más `mcp-server-sqlite` sobre `runs/studio.db` y `firecrawl-mcp` acotado a `scrape`/`extract`). Prohibido activar `Playwright MCP`, `Qdrant MCP` o servidores pesados (>15 tools) en la HP 250 G8.
- **Conocimiento y Persistencia:** `runs/studio.db` (SQLite `WAL` + `FTS5`) es la fuente de verdad operativa; Prompt Caching implícito (`gemini-3.8-flash` @ `global`, >6,144 tokens) inyecta `docs/style_bible.md`, `docs/marketing_playbook.md`, `context/gta6_living_brief.md`, `context/gta6_lore_index.json`, `context/golden_references.json` y `context/viral_style_dna.json`. Qdrant queda en **solo lectura** hasta superar 100 clips con telemetría propia.
- **Referencias aisladas:** `_references/` es solo lectura (en `.gitignore` y `.antigravityignore`). Prohibido importar módulos directamente desde `_references/`.
- **Congelado hasta post-25 clips:** VFX GPU pesados (`SAM 3.1`, `MatAnyone 2`, `DA3`, `HunyuanVideo-Foley`), self-hosting de `Postiz` en Docker local y Multi-POV complejo.
- **Convenciones Antigravity IDE (Nov 2026):** Prohibido crear `.agents/workflows/` (deprecado). Toda automatización vive en `AGENTS.md`, `.agents/rules/*.md` (plano), `.agents/skills/*/SKILL.md` y CLI Python en `src/`.
