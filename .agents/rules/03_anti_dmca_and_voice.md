---
trigger: always_on
description: Higiene anti-DMCA / Content ID para GTA VI y política de voz por tipo de monetización.
---

# Regla 03 — Higiene Anti-DMCA / Content ID y Política de Voz

1. **Filtro preventivo anti-DMCA (GTA VI):** Even con *Mute Licensed Music* activado, GTA VI dispara Content ID en YouTube y mutes en Twitch por radios, música ambiental de locales, menús y DJs. Todo clip candidato debe pasar una verificación de riesgo musical **antes** del render final y publicación; si hay canción licenciada audible sobre el diálogo, **descartar el tramo** o marcarlo para revisión humana estricta.
2. **Seguridad de plataforma (Policy Check):** Descartar automáticamente tramos con desnudez accidental, lenguaje bloqueable por políticas de YouTube/TikTok o pantallas BRB/carga muerta.
3. **Política de voz obligatoria (`VOICE_MODE`):**
   - **Bounties CPM (default Modo B-Lite):** `voice_mode: "none"`. Nunca pisar ni tapar la reacción orgánica del streamer con voz IA (protege naturalidad y evita rechazo por cláusulas anti-voz-IA de campañas).
   - **YPP / Editorial / Puente Long-Form:** Priorizar `human_batch` (Cold Opens de 3–4s grabados con Shure SM58 + Mackie Onyx Artist 1.2). Solo usar `tts` comercial limpio (`kyutai/pocket-tts` o `openbmb/VoxCPM2`) cuando el formato lo justifique y sin violar reglas de campaña.
4. **Transformación real obligatoria:** Nunca publicar cutscenes o gameplay crudo sin reacción del streamer, encuadre 9:16 calibrado, ritmo editado y subtítulos karaoke.
5. **Compuerta humana en Bounties:** Nunca producir ni enviar clips a una campaña CPM sin aprobación humana previa de la auditoría (burn rate 20%–80%, geo-lock, mínimos de vistas y requisitos obligatorios).
