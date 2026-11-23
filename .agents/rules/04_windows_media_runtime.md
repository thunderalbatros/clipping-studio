---
trigger: always_on
description: Invariantes multimedia deterministas Windows 10 (HP 250 G8).
---

# Regla 04 — Runtime Multimedia Windows 10
1. **`yt-dlp`:** Prohibido `--cookies-from-browser` (DPAPI); usar `--cookies <txt>`. Obligatorio `--js-runtimes deno --remote-components ejs:github` (YouTube 1080p); `--impersonate chrome` (Kick); siempre `--download-sections`.
2. **`FFmpeg`+`libass`:** Nunca `C:\...` en `ass=`/`subtitles=`; usar `cwd` temporal y `ass=subs.ass:fontsdir=fonts`. Encadenar `format=nv12` tras `ass=` en `h264_qsv` (fallback `libx264 -preset veryfast`).
3. **Cortes/Overlays:** Prohibido `-c copy` al inicio de clips (freeze keyframe). En overlays con delay usar `setpts=PTS-STARTPTS+{start}/TB` + `eof_action=pass`.
4. **Validación:** Verificar MP4 con `ffprobe` (`1080x1920`, `yuv420p`, `-14.0 LUFS`/`-1.0 dBTP`) y covers/thumbnails con `PIL.Image.size` (`1080x1920`/`1280x720`). `runs/` fuera de OneDrive.
