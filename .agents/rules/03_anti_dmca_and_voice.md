---
trigger: always_on
description: Anti-DMCA GTA VI, voz, anti-bots y gate HITL de bounties.
---

# Regla 03 — Anti-DMCA (3 Modos), Voz y Cuentas Limpias
1. **Política de Audio `--audio-policy` (`YAMNet` pre-render):**
   - `strict_clean` *(default 16:9 YPP / multi-red)*: rechaza o recorta tramos con música licenciada (`>=0.45`) y marca el slice para futuro rescate `Demucs` en Carril 2 (Colab T4).
   - `sound_campaign` *(Content Rewards musicales)*: exige audio limpio sin música ajena de fondo y exporta `sound_campaign_climax_ms` para sincronizar el Sonido Oficial in-app (TikTok/Reels/Shorts).
   - `keep_original`: registra `dmca_advisory` informativo sin rechazar el clip, conservando mezcla original con `loudnorm` (`-14 LUFS`).
2. **Policy & OCR:** Descartar desnudez/BRB y censurar/bleepear groserías en `0-7.5s` (audio + subtítulos OCR).
3. **Voz (`VOICE_MODE`):** `none` en Bounties CPM (jamás pisar reacción original). `human_batch` (SM58) o TTS comercial (`pocket-tts`/`VoxCPM2`) solo YPP/editorial.
4. **Transformación y Cuentas Limpias:** Prohibido gameplay crudo, bots de comentarios/proxies/Multilogin y rage-bait tóxico.
5. **Gate HITL:** Nunca producir/enviar a campañas CPM sin aprobación humana (burn `20%-80%`, geo-lock, reglas).
