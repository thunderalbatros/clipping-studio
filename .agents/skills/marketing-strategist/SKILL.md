---
name: marketing-strategist
description: >-
  Use this skill when auditing CPM clipping bounties (Whop Content Rewards, Clipping.net, Vyro, ClipBoom),
  designing 0-3s viral hooks, crafting 5-network native packaging (YT Shorts, IG Trial Reels, TikTok OCR SEO,
  FB Reels, X 75x author-reply), or planning the Shorts-to-Long-Form YouTube monetization bridge.
---

# Marketing Strategist — Clipping Studio

## Cuándo activar
- Antes de producir un lote: auditar campaña CPM y definir ángulos de retención.
- Al empaquetar masters `9:16` para distribución simultánea en **5 redes** (YouTube Shorts, Instagram Reels, TikTok, Facebook Reels, X) y Variantes A/B.
- Al estructurar compilaciones `16:9` (`8+ min`) para el puente a YouTube Partner Program (YPP).

## Protocolo Operativo
1. **Leer contexto canónico:** Consultar [docs/marketing_playbook.md](../../../docs/marketing_playbook.md), [docs/algorithms_and_virality_bible.md](../../../docs/algorithms_and_virality_bible.md) y `context/streamer_profiles.json`.
2. **Auditoría de Bounties CPM (Gate Humano Obligatorio):**
   - Priorizar gameplay real en Twitch/Kick (descartar `trailer`, `reacting`, `tier list`, `news`, `recap`).
   - Medir *burn rate* en 2 lecturas (`>=6h`): aprobar solo **`20%–80%`**; descartar `<15%` o `>85%`.
   - Auditar geo-lock (compatible Argentina), mínimo de vistas, requisitos obligatorios y `voice_mode: "none"`. Detenerse hasta aprobación humana.
3. **Ingeniería del Gancho (0–3s) + Copy Anti-AI Slop (`ai-tells.md`):**
   - Aplicar **Triple Hook (Kallaway)** + **Framework CCN (Paddy Galloway)**: alinear Audio Hook + Visual Hook (entendible en mute) + Text Hook en `<3s`.
   - Emitir `ab_hook_variants` (2–3 ganchos visuales cortos `<= 4–6 palabras`) que abran un **Curiosity Gap complementario** sin repetir el título literal.
   - **Prohibiciones de copy:** cero *"Not X. Y."*, *"The secret:"*, *"Game-changer"*, em-dashes en titulares, `?!` exagerados ni clickbait que rompa la promesa.
4. **Empaquetado Nativo 5-Redes (Ciencia Algorítmica 2026):**
   - **YouTube Shorts:** título `< 55` chars con keyword *front-loaded* (Shorts Search SEO), 3–5 hashtags limpios (`#GTA6 #Shorts`), `cover_9_16.jpg` en Studio Desktop / `thumbnails.set`, enlace a Long-Form 16:9.
   - **Instagram Reels:** copy con gatillo de envío por DM (**`Sends Per Reach`** > comentarios en no-seguidores: *"Send this to your duo..."*). Planificar **Trial Reels** (A/B 72h a no-seguidores) usando `--ab-variants 2` (nunca re-subir el mismo MP4 idéntico).
   - **TikTok:** keywords SEO habladas + banner `0–3s` (**OCR + ASR**), verificar censura total de groserías en `0–7.5s` (evita *FYP suppression* por OCR), `cover_timestamp_ms` en API. Si Variante A muere en `200–300` vistas, reciclar solo con **Variante B** (nuevo hook `0–3s` + nuevo hash).
   - **Facebook Reels:** copy de contexto rápido/debate sano, márgenes `top 14% / bottom 35% / lados 6%`, portada en Meta Business Suite.
   - **X (Twitter — `x-algorithm`):** MP4 nativo `<= 140` chars, 0–2 hashtags, **cero link externo en el post principal** (usar siempre **`link-in-reply`** para evitar penalización `-30% a -50%+`), y redactar **`author_reply_seed`** para que el operador responda orgánicamente a comentarios reales (multiplicador **`~75x`**).
5. **Líneas Rojas Anti-Ban:** Prohibido bots de comentarios (Playwright/Multilogin/proxies), *rage bait* tóxico y *engagement bait* hueco (*"Comentá SÍ"*). Mantener canal Bounties CPM separado del Canal Propio YPP (`human_batch` SM58).

## Validación de Salida
- Confirmar cumplimiento de cláusulas CPM, `voice_mode: "none"` por defecto, `ab_hook_variants` listos y `author_reply_seed` orgánico sin spam.
