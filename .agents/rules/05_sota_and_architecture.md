---
trigger: always_on
description: SOTA OSS KS-4, enrutamiento 3 carriles, higiene visual dura y evidencia runtime.
---

# Regla 05 — SOTA KS-4, 3 Carriles y Calidad Visual

1. **Integrate-over-Build (KS-4):** Priorizar librerías/modelos OSS comerciales vigentes (MIT/Apache-2.0/Unlicense: `auto-editor`, `birefnet-general-lite`, `sqlite-vec`, `fastembed`, `onnxruntime==1.32.0`) sobre código artesanal. Prohibido `CC-BY-NC`.
2. **Cómputo en 3 Carriles:**
   - **Carril 1 (`local_hp`, `$0`, IP residencial):** Ingesta (`yt-dlp`, `curl_cffi`, `live_sniper`), `SQLite WAL`, `FFmpeg h264_qsv`, `auto-editor`, `parakeet`, `YAMNet`, `LR-ASD`, `BiRefNet-lite` (1 imagen estática). Todo protegido por `resource_guard` (pausar/diferir si `used_ram_pct > 85%`).
   - **Carril 2 (`colab_t4`, 16 GB VRAM, `$0`):** `Demucs` (rescate anti-DMCA), *video matting* temporal (`60fps`), diarización multi-hablante o generación GPU.
   - **Carril 3 (`gcp_spot_l4`, crédito reservado):** Prohibido encender VM para 1 clip; solo lotes `>= 5-10` tareas con watchdog `shutdown -h now` y cero discos `pd-balanced` persistentes.
3. **Calidad Visual Innegociable:** Prohibido Frame 0 contaminado, prohibido subtítulos quemados en portadas/thumbnails (`cover_9_16.jpg` siempre pre-`libass`) y prohibido repetir palabras literales del título en el texto gancho de portada.
4. **Evidencia Obligatoria:** Separar siempre `[ESTÁTICO]` vs `[RUNTIME REAL]` en todo reporte de cierre.
