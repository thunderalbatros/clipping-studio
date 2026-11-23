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

## 3. Roles del Equipo de Agentes (4 Skills en `.agents/skills/`)
1. **`marketing-strategist`** — Auditoría de bounties CPM (burn rate 20%–80%, cláusulas trampa, geo-locks), hooks de 0–3s, empaquetado de títulos/captions y estrategia puente a YouTube Long-Form.
2. **`av-director`** — Dirección narrativa y visual: inicio directo en acción/reacción (nunca en pregunta o contexto muerto), encuadre split-screen webcam-sobre-gameplay, freeze-frame con callout, pacing sin dead air y política `voice_mode`.
3. **`media-engineer`** — Ingeniería determinista: `yt-dlp` + `curl_cffi` + JS runtime, `parakeet-tdt-0.6b-v3` (`onnx-asr` `int8`), `cutawan` ONNX (UltraFace + LR-ASD), `FFmpeg` (`h264_qsv`/`h264_nvenc`/`libx264`), `libass` karaoke, `setpts` y abstracción `compute_profile`.
4. **`retention-critic`** — QA multimodal y gate de salida: inspección de 4–6 frames clave vía `ffmpeg-mcp-lite`, safe-zones móviles (Samsung Galaxy A36), auditoría de audio (`-14 LUFS`, pico `<= -1.0 dBTP`), chequeo anti-DMCA y rúbrica de 5 ejes (`max_retries=1`).

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

## 6. Presupuesto de Complejidad y Restricciones de Alcance (v1)
- **Tope de MCPs activos en Clipping Studio:** Máximo **2 MCPs** (`context7` + `ffmpeg-mcp-lite`). Prohibido activar `Playwright MCP`, `Qdrant MCP` o servidores con >15 tools antes de superar los primeros 10 clips publicados.
- **Conocimiento en v1 (Prompt Caching):** Leer las biblias destiladas `docs/style_bible.md`, `docs/marketing_playbook.md` y `docs/engineering_patterns.md`. Qdrant queda en **solo lectura** hasta superar 100 clips con telemetría propia.
- **Referencias aisladas:** `_references/` es solo lectura (en `.gitignore` y `.antigravityignore`). Prohibido importar módulos directamente desde `_references/`.
- **Congelado hasta post-10 clips:** VFX GPU (`SAM 3.1`, `MatAnyone 2`, `DA3`, `HunyuanVideo-Foley`), `hyperframes`, scraping autónomo con Playwright y Multi-POV complejo.
- **Convenciones Antigravity IDE (Nov 2026):** Prohibido crear `.agents/workflows/` (deprecado). Toda automatización vive en `AGENTS.md`, `.agents/rules/*.md` (plano), `.agents/skills/*/SKILL.md` y CLI Python en `src/`.
