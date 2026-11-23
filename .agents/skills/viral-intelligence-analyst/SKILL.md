---
name: viral-intelligence-analyst
description: >-
  Use this skill to discover viral short-form outliers across YouTube Shorts, TikTok, Instagram Reels,
  Facebook Reels, and X, reverse-engineer their pacing/cuts/hooks with FFmpeg (/outliers + /steal),
  and update context/viral_style_dna.json and context/golden_references.json.
---

# Viral Intelligence Analyst — Clipping Studio

## Cuándo activar
- Al actualizar el radar diario de tendencias (`python -m src.cli trends`) en las 5 redes (YT Shorts, TikTok, IG Reels, FB Reels, X).
- Al diseccionar estructuralmente un video viral ganador para extraer su *Style DNA* (ritmo, cortes/min, duración de gancho, energía inicial y cadencia de subtítulos).

## Protocolo Operativo (Destilado de `/outliers` + `/steal` — Anti-AI Slop)
1. **Descubrimiento de Outliers (`/outliers`):**
   - Nunca medir éxito por vistas absolutas aisladas; calcular **velocidad derivada**: `views_per_hour`, `like_ratio` y, cuando esté disponible, `share_to_view` y `comment_to_like`.
   - Priorizar videos publicados en las últimas **24–72 horas** en cuentas similares o del mismo nicho (`#GTA6`, streamers Tier-1 EN/ES).
   - Respetar rate limits y ToS: ingesta pasiva acotada (1 corrida/día, máx. 15–20 candidatos por plataforma) usando YouTube Data API v3, Kick API vía `curl_cffi`, `yt-dlp --dump-single-json` y `firecrawl-mcp` scoped.
2. **Disección Estructural con FFmpeg (`/steal`):**
   - Extraer métricas cuantitativas de los 3–5 outliers top sin plagiar contenido:
     - Duración total y ventana del gancho inicial (`0.0–3.0 s`).
     - Densidad de cambios de plano (`scdet` / cortes por minuto).
     - Energía acústica de apertura (`RMS dBFS` en `0–3 s` vs promedio del clip).
     - Patrón de título, texto en pantalla y disparador emocional (*Curiosity Gap*, *Slapstick Payoff*, *Disbelief*, *Viral Bug*).
3. **Actualización de Contexto Cacheado:**
   - Persistir resultados en `context/golden_references.json` y `context/viral_style_dna.json`, manteniendo formato compacto para el Implicit Caching de `gemini-3.8-flash`.

## Validación de Salida
- Entregar métricas verificables (`vph`, duración óptima, cortes/min, patrón de hook) con cero URLs rotas ni datos inventados.
