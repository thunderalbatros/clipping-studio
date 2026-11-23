---
trigger: always_on
description: Guardrails FinOps estrictos para los últimos USD 30 en créditos de GCP y degradación elegante a USD 0/mes.
---

# Regla 02 — Guardrails FinOps GCP (Presupuesto Finito USD 30)

1. **Presupuesto cerrado:** Asignación orientativa USD 16 (`g2-standard-4` Spot L4 en `us-east4`/`us-central1` bajo demanda), USD 9 (Vertex AI Gemini Flash/TTS), USD 5 (GCS Standard misma región + buffer). Alertas obligatorias al 50%, 80% y 95%.
2. **Cero recursos ociosos 24/7:** Toda VM Spot en GCP debe iniciar con watchdog de apagado automático tras **10–15 min de inactividad** (`shutdown -h now`). Prohibido dejar discos `pd-ssd` persistentes cobrando con la VM apagada, IPs estáticas reservadas o Cloud NAT.
3. **Cero descarga de VODs completos desde GCP:** Nunca bajar VODs de 20–40 GB en GCP (evita egress a USD 0.12/GB y bloqueos `403` de YouTube en IP datacenter). Ingestar solo slices parciales desde `local_hp` (IP residencial) y usar red **Standard Tier** (200 GiB/mes gratis).
4. **Control de tokens LLM:** Usar Prompt Caching implícito/estructurado sobre `docs/style_bible.md` y `docs/marketing_playbook.md`; enviar al `Reviewer` solo **4–6 frames clave en WebP** (no videos enteros por defecto) y fijar `max_retries=1`.
5. **Compatibilidad dual obligatoria (`sm_89` -> `sm_75` -> CPU):** Todo código en `src/` debe leer `COMPUTE_PROFILE`. En `colab_t4` (`sm_75`) queda prohibido `bfloat16`, `fp8` y `flash_attention_2` (forzar `float16`/`int8` + `sdpa`). Al agotarse los USD 30, cambiar `COMPUTE_PROFILE` sin tocar lógica.
