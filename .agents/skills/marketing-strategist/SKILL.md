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
4. **Empaquetado Nativo 5-Redes (Ciencia Algorítmica Oct–Nov 2026):**
   - **YouTube Shorts:** título `< 55` chars con keyword *front-loaded* (Shorts Search SEO), 3–5 hashtags limpios (`#GTA6 #Shorts`), publicar `< 2–6h` del stream (`Example Age`), `cover_9_16.jpg` en Studio Desktop / `thumbnails.set`, enlace Related Video a Long-Form `16:9`. Auditar **Engaged Views** (no Public Views infladas) y política **Inauthentic Content** (julio 2025).
   - **Instagram Reels:** optimizar **3s Skip Rate** + Watch Time + **`Sends Per Reach`** (DM shares: *"Send this to your duo..."*) con **máx. 5 hashtags**. Usar **Trial Reels** (24–72h a no-seguidores) con `--ab-variants 2` (jamás re-subir el mismo MP4). **Alerta crítica:** `>=10` clips no-originales en 30 días expulsan toda la cuenta de recomendaciones.
   - **TikTok:** SEO cuádruple (**ASR + OCR `0–3s` + Caption + Comentarios**), censura total de groserías en `0–7.5s` (evita *FYP suppression* por OCR), transformación real anti-pHash, `cover_timestamp_ms` en API. Si Variante A muere en `200–300` vistas, reciclar solo con **Variante B** (nuevo hook `0–3s` + nuevo hash).
   - **Facebook Reels:** copy de contexto rápido/debate sano, márgenes `top 14% / bottom 35% / lados 6%`, portada en Meta Business Suite.
   - **X (Twitter — `xai-org/x-algorithm` Oct–Nov 2026):** MP4 nativo **`>= 12s`** (`MinVideoDurationMs=10_000`), `<= 140` chars, 0–2 hashtags, **cero link externo en el post principal** (**`link-in-reply`**). Publicar en `< 2h` pico para capturar **Cold-Start Thompson Sampling** (`<50k` followers, `<200` imp), espaciar **`>= 3h`** (`VideoCarouselFatigueMinutes=60` + `author_diversity_decay`), transformar layout para vencer `VideoCarouselEmbeddingDedupe (0.72)`, y optimizar para **`ShareViaCopyLink (+20.0)`**, **`Reply+Mutual (+20.0)`**, **`DM Share (+5.0)`** y **`Quote (+5.0)`** mediante `author_reply_seed` orgánico.
5. **Líneas Rojas Anti-Ban:** Prohibido bots de comentarios (Playwright/Multilogin/proxies), *rage bait* tóxico y *engagement bait* hueco (*"Comentá SÍ"*). Mantener canal Bounties CPM separado del Canal Propio YPP (`human_batch` SM58).

## Validación de Salida
- Confirmar cumplimiento de cláusulas CPM, `voice_mode: "none"` por defecto, `ab_hook_variants` listos y `author_reply_seed` orgánico sin spam.
