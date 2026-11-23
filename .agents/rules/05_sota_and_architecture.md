---
trigger: always_on
description: SOTA OSS KS-4, enrutamiento 3 carriles, higiene visual/disco, Research-First y apertura multi-cliente.
---

# Regla 05 — SOTA KS-4, 3 Carriles y Reglas CTO

1. **Integrate-over-Build & Ventana SOTA 3m (KS-4):** Antes de adoptar librerías o crear módulos nuevos, verificar SOTA de los últimos 3 meses y priorizar OSS comercial vigente (MIT/Apache-2.0/Unlicense: `auto-editor`, `birefnet-general-lite`, `sqlite-vec`, `fastembed`, `onnxruntime==1.32.0`). Prohibido `CC-BY-NC`.
2. **Cómputo en 3 Carriles:**
   - **Carril 1 (`local_hp`, `$0`):** Ingesta (`yt-dlp`, `curl_cffi`, `live_sniper`), `SQLite WAL`, `FFmpeg h264_qsv`, `auto-editor`, `parakeet`, `YAMNet`, `LR-ASD`, `BiRefNet-lite` (1 imagen); protegido por `resource_guard` (`used_ram_pct > 85%`).
   - **Carril 2 (`colab_t4`, `$0`):** `Demucs`, *video matting* `60fps`, diarización o GPU.
   - **Carril 3 (`gcp_spot_l4`):** Prohibido encender VM por 1 clip; solo lotes `>=5-10` con watchdog `shutdown -h now` y sin `pd-balanced` persistentes.
3. **Calidad Visual y Fluidez:** Prohibido Frame 0 contaminado, subtítulos en portadas (`cover_9_16.jpg` pre-`libass`), repetir título en gancho de portada, *freeze-frames* antifuncionales que corten la fluidez y callouts estáticos pequeños (todo gráfico sobre video corre fluido con escala editorial, transición y SFX).
4. **Research-First / Scene-Match:** Deconstruir outliers virales antes de editar (`scene_researcher`), con purga obligatoria de MP4 temporales de referencia.
5. **Apertura Multi-Cliente y Multi-Bounty:** Prohibido whitelists bloqueantes de streamers o nichos; el pipeline acepta cualquier creador/cliente/bounty vía auto-profiling SQLite.
6. **Higiene de Disco (`C:\`) y Evidencia:** Purga automática de temporales multimedia, `python -m src.cli gc` periódico, y separar siempre `[ESTÁTICO]` vs `[RUNTIME REAL]`.
