---
name: bounty-and-distribution-ops
description: >-
  Use this skill when scanning and auditing CPM clipping bounties (Whop Content Rewards, Clipping.net,
  Vyro, ClipBoom), enforcing burn-rate/ToS compliance in runs/studio.db campaigns table, managing HITL
  submissions, and tracking post-publish multi-platform telemetry across 5 networks.
---

# Bounty & Distribution Ops — Clipping Studio

## Cuándo activar
- Al escanear, auditar o priorizar campañas CPM activas (`Whop Content Rewards`, `Clipping.net`, `Vyro`, `ClipBoom`).
- Antes de producir un lote dirigido a una campaña o al registrar métricas post-publicación en `runs/studio.db`.

## Protocolo Operativo
1. **Auditoría Determinista de Bounties CPM (Gate Humano Obligatorio):**
   - Medir *burn rate* del presupuesto entre **2 snapshots separados** registrados en la tabla `campaigns` de `runs/studio.db`:
     - **20%–80%:** zona saludable (`APROBAR` si pasa compliance).
     - **<15%:** sin validación de mercado (`REVISAR`).
     - **>85%:** pool casi agotado antes de acumular vistas (`DESCARTAR`).
   - Revisar cláusulas trampa obligatorias: geo-lock Tier-1 (US/UK/CA), mínimo de vistas por clip, watermark/logo/hashtag exigido, exclusión de voz IA (`voice_mode: "none"`) y streamer/VOD autorizado.
2. **Compliance Anti-Ban & ToS (Regla de Hierro):**
   - **Prohibido** automatizar *submits* masivos en Whop/marketplaces o granjas de cuentas falsas en TikTok/Meta/YouTube (confiscación de pagos y ban permanente).
   - Automatizar **inteligencia, auditoría, producción y empaquetado**; el envío final a la campaña siempre pasa por confirmación humana (*HITL*).
3. **Distribución 5 Redes y Cierre de Telemetría:**
   - Verificar que cada clip aprobado salga con `clip_XX_packaging.json` completo para **YouTube Shorts, TikTok, Instagram Reels, Facebook Reels y X**.
   - Registrar en `runs/studio.db` (`telemetry`) las métricas reales a 24 h y 72 h (`views`, `likes`, `shares`, `avg_view_duration`) para retroalimentar al `curator` y `viral-intelligence-analyst`.

## Validación de Salida
- Tabla veredicto (`APROBAR` | `REVISAR` | `DESCARTAR`) respaldada por registros en `runs/studio.db` y aprobación explícita del operador humano antes de producir.
