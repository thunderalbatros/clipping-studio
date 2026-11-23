---
trigger: always_on
description: Anti-DMCA GTA VI, voz, anti-bots y gate HITL de bounties.
---

# Regla 03 — Anti-DMCA, Voz y Cuentas Limpias
1. **Anti-DMCA (GTA VI):** Auditar música licenciada (radios/locales/menús) con `YAMNet` pre-render; si contamina diálogo, descartar o HITL.
2. **Policy & OCR:** Descartar desnudez/BRB y censurar/bleepear groserías en `0-7.5s` (audio + subtítulos OCR).
3. **Voz (`VOICE_MODE`):** `none` en Bounties CPM (jamás pisar reacción original). `human_batch` (SM58) o TTS comercial (`pocket-tts`/`VoxCPM2`) solo YPP/editorial.
4. **Transformación y Cuentas Limpias:** Prohibido gameplay crudo, bots de comentarios/proxies/Multilogin y rage-bait tóxico.
5. **Gate HITL:** Nunca producir/enviar a campañas CPM sin aprobación humana (burn `20%-80%`, geo-lock, reglas).
