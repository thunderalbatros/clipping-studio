---
trigger: always_on
description: Reglas técnicas obligatorias para ejecución multimedia determinista en Windows 10 (HP 250 G8).
---

# Regla 04 — Runtime Multimedia Determinista en Windows 10

1. **`yt-dlp` en Windows (2026):**
   - **Prohibido `--cookies-from-browser chrome/edge/brave`** (rompe por cifrado DPAPI de Windows). Usar siempre `--cookies <ruta_cookies.txt>` exportado desde Firefox o sesión incógnito cerrada.
   - Requerir runtime JS externo para YouTube EJS (`deno` v2.9.8 instalado + flag obligatorio `--js-runtimes deno --remote-components ejs:github`; sin `--remote-components ejs:github` YouTube falla el `n-challenge` y omite formatos 1080p).
   - Para Kick.com usar `curl_cffi` (`--impersonate chrome`) contra Cloudflare JA4.
   - Nunca descargar VODs completos cuando se busca un rango: usar `--download-sections "*HH:MM:SS-HH:MM:SS"` o stream-piping.
2. **`FFmpeg` + `libass` en Windows:**
   - **Nunca pasar rutas absolutas de Windows (`C:\...`) dentro de los filtros `ass=` o `subtitles=`.** Ejecutar `FFmpeg` con `cwd` en la carpeta temporal del clip y pasar ruta relativa simple (`ass=subs.ass`).
   - Encadenar explícitamente `format=nv12` tras `ass=` al usar **`h264_qsv`** (verificado en runtime en la HP 250 G8 con Intel i5-1035G1). Mantener fallback automático a `libx264 -preset veryfast`.
3. **Precisión de cortes y overlays:**
   - **Prohibido `-c copy` en el inicio de un clip:** produce 1–3 segundos congelados por desfase de keyframe y destruye la retención inicial. Re-encodear siempre los tramos recortados.
   - Al superponer overlays/B-roll con retraso temporal (`enable='between(t,s,e)'`), aplicar siempre `setpts=PTS-STARTPTS+{start}/TB` y `eof_action=pass` (evita freeze-frame fantasma).
4. **Higiene de OneDrive y Disco:** Mantener `runs/` y temporales fuera de sincronización activa de OneDrive y limpiar artefactos intermedios pesados al cerrar cada lote.
5. **Validación de salidas:** Todo MP4 final debe verificarse con `ffprobe` (resolución `1080x1920`, `pix_fmt=yuv420p`, audio presente y duración esperada) y loudness `-14.0 LUFS` / `-1.0 dBTP`.
