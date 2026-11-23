---
trigger: always_on
description: FinOps GCP USD 30, sm_89/sm_75/CPU y presupuesto de tokens.
---

# Regla 02 — FinOps GCP USD 30 y Contexto
1. **Tope USD 30:** ~USD 16 Spot L4 + USD 9 Vertex AI + USD 5 GCS Standard. Alertas 50%/80%/95%; conmutar `COMPUTE_PROFILE` al 90%.
2. **Cero ociosidad:** Watchdog `shutdown -h now` a los 10-15 min. Sin `pd-ssd` huérfanos, IPs estáticas ni Cloud NAT.
3. **Cero VODs en GCP:** Ingestar solo slices desde `local_hp` (evita egress USD 0.12/GB y `403`). Red Standard Tier.
4. **Tokens:** Prompt Cache (`style_bible.md`+`marketing_playbook.md`+`design_bible.md`); RAG `<=550 tok`; `Reviewer` solo 4-6 frames WebP (`max_retries=1`). En IDE: `rtk`, buscar antes de leer, `/compact`.
5. **Dual-compute:** En `colab_t4` (`sm_75`) prohibido `bfloat16`, `fp8`, `flash_attention_2` (solo `float16`/`int8` + `sdpa`).
