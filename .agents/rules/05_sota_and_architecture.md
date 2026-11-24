---
trigger: always_on
description: Cloud-First GCP L4 + Vertex Serverless, RAG 3-Niveles + Web-First, SOTA KS-4 e higiene CTO.
---

# Regla 05 — Cloud-First, RAG 3-Niveles, SOTA KS-4 y Reglas CTO

1. **Integrate-over-Build & SOTA `<3m` (KS-4):** Verificar SOTA `<3 meses` antes de adoptar librerías o crear módulos; priorizar OSS comercial (`auto-editor`, `birefnet-general-lite`, `sqlite-vec`, `fastembed`, `chonkie`, `pydantic-ai-slim`, `pydantic-evals`, `ffmpeg-normalize`, `onnxruntime==1.32.0`). Prohibido `CC-BY-NC`.
2. **Arquitectura Cloud-First (3 Planos):**
   - **Control + Ingesta (`local_hp`, `$0`):** CLI, ingesta IP residencial (`yt-dlp`, `curl_cffi`, `live_sniper`), `SQLite WAL`, QA liviano y `psutil` (`resource_guard` + `unload_heavy_models()`).
   - **Worker GPU (`GCP Cloud Run Jobs` + `NVIDIA L4`):** Cómputo pesado (video/audio/matting/lotes) en Cloud Run Jobs (`--no-gpu-zonal-redundancy` + GCS FUSE `us-central1`, escala a `$0`/s, cero VMs/discos ociosos). `colab_t4` = rescate `$0`.
   - **Vertex AI Serverless:** `gemini-3.8-flash` / `gemini-3.5-flash-lite` (nunca `gemini-2.5-flash` retirado `2026-12-01`; opt-in Flex `-50%`), `gemini-nano-banana-2.1` (reemplaza Imagen 3/4) y `gemini-3.8-flash-lite-tts`.
3. **RAG 3-Niveles + Web-First:**
   - **N1 (`runs/studio.db`):** Reglas, 7 skills, memoria episódica y campañas (`sqlite-vec` `domain` pre-KNN + `chonkie`).
   - **N2 (`rag_library.sqlite` `?mode=ro&immutable=1` + Qdrant RO):** Libros/matrices (`TextCrossEncoder` rerank).
   - **N3 (Web-First `<3m`):** Web search para SDKs/licencias, cambios algorítmicos y lore vivo en `<untrusted_source_data>`.
4. **Calidad Visual, Research-First e Higiene:** Protocolo *Research-First / Scene-Match* con purga de MP4 temporales; prohibido Frame 0 sucio, subtítulos en portadas, `portada == overlay`, *freeze-frames* antifuncionales y whitelists bloqueantes. QA (`evals/visual_qa.py`) obligatorio antes de `READY_HITL`/Telegram; `cli gc` y separar `[ESTÁTICO]` vs `[RUNTIME REAL]`.
