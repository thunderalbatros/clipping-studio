# FASE 2 + HITO 0 — AUDITORÍA LOCAL, DESTILACIÓN Y VERIFICACIÓN DE ENTORNO

**Fecha:** 22 de noviembre de 2026  
**Equipo:** HP 250 G8 — Intel Core i5-1035G1 (4C/8T, 10ª gen Ice Lake), Intel UHD Graphics (driver `31.0.101.2145`), **15.7 GB RAM**, Windows 10 Pro 64-bit.  
**Modo inicial aprobado:** **Modo B-Lite (GTA VI en inglés)** — `voice_mode: "none"` (Bounties CPM) + puente Shorts -> YouTube Long-Form (8+ min).

---

## 1. Resumen Estricto: `[ESTÁTICO]` vs. `[RUNTIME REAL]`

### 1.1 Artefactos Creados (`[ESTÁTICO]`)
- **Seguridad y Exclusión de Indexado:**
  - `.gitignore` (protege `.env`, `*cookies*.txt`, credenciales JSON/PEM, `_references/`, `runs/`, medios pesados y pesos ML).
  - `.antigravityignore` (excluye `_references/`, `runs/`, `.venv/`, secretos y binarios del indexador global de Antigravity IDE).
  - `.env.example` (plantilla completa para `COMPUTE_PROFILE`, `STUDIO_MODE=streamer_gta6_lite`, `VOICE_MODE=none`, escalera LLM, GCP FinOps, ingesta y Qdrant).
- **Constitución y Reglas Antigravity IDE (Nov 2026 — sin `.agents/workflows/` deprecado):**
  - `AGENTS.md` (separación Plano Dev vs. Plano Runtime, 4 roles, regla estático vs. runtime, degradación elegante y presupuesto de complejidad).
  - `.agents/rules/01_security_and_secrets.md` (`trigger: always_on`)
  - `.agents/rules/02_finops_gcp_30usd.md` (`trigger: always_on`)
  - `.agents/rules/03_anti_dmca_and_voice.md` (`trigger: always_on`)
  - `.agents/rules/04_windows_media_runtime.md` (`trigger: always_on`)
- **4 Agent Skills del Departamento Audiovisual y Marketing (`.agents/skills/`):**
  - `.agents/skills/marketing-strategist/SKILL.md`
  - `.agents/skills/av-director/SKILL.md`
  - `.agents/skills/media-engineer/SKILL.md`
  - `.agents/skills/retention-critic/SKILL.md`
- **Conocimiento Destilado para Prompt Caching (`docs/`):**
  - `docs/style_bible.md` (<2.000 tokens — narrativa acción->reacción, safe-zones Galaxy A36, `-14.0 LUFS / -1.0 dBTP`, *Synchresis*, anti-DMCA GTA VI y `voice_mode`).
  - `docs/marketing_playbook.md` (<2.000 tokens — tesis Argentina->Tier-1, ventana YPP 01/02/2027, checklist de bounties CPM 20%–80%, hooks 0–3s, warm-up y puente Long-Form).
  - `docs/engineering_patterns.md` (<1.800 tokens — mapa exacto de extracción de proyectos locales y `_references/`).
- **Estructura Base y Configuración Hito 0:**
  - Carpetas creadas: `src/`, `config/`, `context/`, `docs/`, `evals/`, `runs/`, `_references/`, `.agents/rules/`, `.agents/skills/`.
  - `config/profiles.yaml` (perfilamiento declarativo `local_hp | gcp_spot_l4 | colab_t4`).
- **4 Repositorios Clonados en `_references/` (Solo Lectura, `--depth 1`):**
  - `_references/agentic-video-editor` (MIT — commit `47248b5`)
  - `_references/cutawan` (MIT — commit `084d0b2`)
  - `_references/ClipKaro` (MIT — commit `e573591`)
  - `_references/clipmint` (MIT — commit `4ca9b70`)

---

### 1.2 Verificaciones Ejecutadas en la Máquina (`[RUNTIME REAL]`)

#### A) Herramientas del Sistema y Entorno Python `.venv`
| Componente | Estado `[RUNTIME REAL]` | Versión / Evidencia Verificada | Acción / Nota |
| :--- | :---: | :--- | :--- |
| **Python (Sistema)** | **OK** | `Python 3.14.7` (default) + **`Python 3.12.10`** instalada | Se creó `.venv` limpio con **`Python 3.12.10`** vía `uv` (máxima compatibilidad con wheels ML/ONNX). |
| **Entorno `.venv` del Proyecto** | **OK** | `.venv\Scripts\python.exe` (CPython `3.12.10`) | Instalados en 4.38s: `yt-dlp==2026.10.26`, `curl-cffi==0.17.0`, `pydantic==2.14.4`, `pysubs2==1.9.1`, `pillow==13.0.0`, `onnxruntime==1.32.0`, `pyyaml==6.0.4`, `python-dotenv==1.2.4`. |
| **Node.js / npm / npx** | **OK** | `Node v24.21.0`, `npm 11.19.0`, `npx 11.19.0` | Actúa como runtime para `context7` y fallback `--js-runtimes node` para `yt-dlp`. |
| **Git / uv** | **OK** | `git 2.55.0.windows.5`, `uv 0.12.24` | Operativos; `uvx` verificado para MCP Python. |
| **FFmpeg / ffprobe** | **OK** | **`ffmpeg 9.0.2-full_build-www.gyan.dev`** | Incluye `h264_qsv`, `h264_nvenc`, `libx264`, `ass` y `subtitles` (`libass`). |
| **`yt-dlp`** | **OK (en `.venv`)** | **`2026.10.26`** en `.venv\Scripts\yt-dlp.exe` (global `2026.08.19` solo como módulo `python -m yt_dlp`) | Usar siempre `.venv\Scripts\yt-dlp.exe` (más reciente y con `curl_cffi` integrado). |
| **`deno` (EJS YouTube 2026)** | **FALTANTE** | No encontrado en `PATH` | **Recomendado instalar (`winget install DenoLand.Deno`)** antes del Prompt 3; mientras tanto `yt-dlp` puede usar `--js-runtimes node` (Node v24 presente). |
| **`TwitchDownloaderCLI`** | **FALTANTE** | No encontrado en `PATH` | **Necesario para Modo B-Lite si se minan picos de chat de VODs de Twitch** (descarga parcial de chat JSON). No necesario si las fuentes iniciales son YouTube VODs/streams. |
| **`cookies.txt`** | **FALTANTE** | No encontrado en ninguna ruta autorizada | Exportar desde Firefox o sesión incógnito cerrada antes de correr descargas YouTube protegidas (nunca `--cookies-from-browser chrome`). |

#### B) Test Real de Hardware Encoder (`h264_qsv` + `libass`) en HP 250 G8
| Prueba Ejecutada | Comando / Filtro | Resultado `[RUNTIME REAL]` |
| :--- | :--- | :--- |
| **`h264_qsv` directo** | `testsrc2 1280x720 30fps -> format=nv12 -> h264_qsv` | **PASS (`exit=0`)**, MP4 generado correctamente. |
| **`h264_qsv` con `init_hw_device + hwupload`** | `-init_hw_device qsv=hw -> format=nv12,hwupload=derive_device=qsv -> h264_qsv` | **PASS (`exit=0`)**, MP4 generado correctamente. |
| **`h264_qsv` + `libass` karaoke (9:16)** | `cwd` relativo + `ass=subs.ass,format=nv12 -> h264_qsv` (`1080x1920`) | **PASS (`exit=0`)**, `ffprobe` verificó `codec_name=h264`, `width=1080`, `height=1920`, `pix_fmt=yuv420p`. |
| **Fallback `libx264`** | `pix_fmt yuv420p -> libx264 -preset veryfast` | **PASS (`exit=0`)**. |

> **Conclusión de hardware:** Intel QuickSync **`h264_qsv` funciona nativamente en esta HP 250 G8** junto con `libass` (con `cwd` relativo y `format=nv12`). No hace falta caer a `libx264` salvo fallback de emergencia.

#### C) Verificación en Runtime de los 2 MCPs Aprobados
| Servidor MCP | Configuración (`C:\Users\tomas\.gemini\config\mcp_config.json`) | Resultado `[RUNTIME REAL]` |
| :--- | :--- | :--- |
| **`context7`** | `cmd.exe /c npx -y @upstash/context7-mcp@4.7.0` | **VERIFICADO OK:** Llamada real a `resolve-library-id("yt-dlp")` devolvió `/yt-dlp/yt-dlp` y `/yt-dlp/yt-dlp-wiki` sin errores. |
| **`ffmpeg-mcp-lite`** | `uvx --python 3.12 --with mcp<2 ffmpeg-mcp-lite` *(respaldo creado en `mcp_config.json.bak_clipping_studio`; no se alteraron otros proyectos)* | **VERIFICADO OK (con fix real de runtime):** La prueba inicial detectó `ModuleNotFoundError: mcp.server.fastmcp` por ruptura de `mcp 2.x` en PyPI; al fijar `--python 3.12 --with mcp<2`, respondió `initialize OK` (`ffmpeg-mcp v1.30.0`), expuso **8 tools exactas** (`ffmpeg_get_info`, `ffmpeg_convert`, `ffmpeg_compress`, `ffmpeg_trim`, `ffmpeg_extract_audio`, `ffmpeg_merge`, `ffmpeg_extract_frames`, `ffmpeg_add_subtitles`) y ejecutó `ffmpeg_get_info` sobre un MP4 real `1080x1920` con `isError=False`. *(Nota: recargar/iniciar nueva sesión en Antigravity IDE para que el IDE monte las 8 tools en su tabla activa).* |

#### D) Auditoría de Secretos y API Keys (`PRESENTE / FALTANTE` — Cero Valores Expuestos)
En **variables de entorno del sistema** y en el `.env` de `clipping studio` (aún no creado), todas figuran `FALTANTE`. En los `.env` existentes de proyectos locales autorizados se detectó disponibilidad previa (que podrás copiar selectivamente a `clipping studio\.env` sin compartir nada fuera de tu máquina):

| Variable | En `clipping studio\.env` / Env Windows | En `.env` de otros proyectos locales | Prioridad para Prompt 3 |
| :--- | :---: | :---: | :--- |
| `USE_VERTEX_AI` / `GCP_PROJECT_ID` / `GOOGLE_APPLICATION_CREDENTIALS` | `FALTANTE` | **`PRESENTE`** | **Alta** (motor LLM primario con créditos GCP) |
| `GEMINI_API_KEY` (AI Studio) | `FALTANTE` | `FALTANTE` | **Media** (recomendada como respaldo gratuito de rotación) |
| `GROQ_API_KEY` | `FALTANTE` | **`PRESENTE`** | **Alta** (fallback gratuito anti-503) |
| `OPENROUTER_API_KEY` | `FALTANTE` | **`PRESENTE`** | **Media** (2º fallback gratuito) |
| `QDRANT_HOST` / `QDRANT_API_KEY` | `FALTANTE` | **`PRESENTE`** | **Baja en v1** (destilación única ya completada; se activa en Fase 2) |
| `HF_TOKEN` | `FALTANTE` | **`PRESENTE`** | **Media** (modelos gated opcionales) |
| `YOUTUBE_API_KEY` | `FALTANTE` | **`PRESENTE`** | **Media** (búsqueda/metadatos de VODs GTA VI) |
| `KICK_CLIENT_ID` / `KICK_CLIENT_SECRET` | `FALTANTE` | **`PRESENTE`** | **Baja en v1** (opcional Modo B) |
| `TWITCH_CLIENT_ID` / `WHOP_API_KEY` | `FALTANTE` | `FALTANTE` | **Baja en v1** (auditoría de bounties es semi-automática con gate humano) |

---

## 2. Auditoría de Proyectos Locales y Destilación Única de Qdrant (Solo Lectura)

### 2.1 Clasificación de Proyectos Locales
| Proyecto Local | Clasificación | Aporte Destilado (ver `docs/engineering_patterns.md`) |
| :--- | :---: | :--- |
| `C:\Users\tomas\.zcode\workspace\default\centinela-studio` | **`EXTRAER PATRÓN`** | Zero-Disk Streaming Pipe (`yt-dlp stdout -> ffmpeg pipe:0`), filtro de escena `gt(scene,0.25)`, frame differencing (`<4%`), Smart HLS Slicing y defensa WAF Kick. |
| `C:\Users\tomas\Desktop\streamerbot` | **`EXTRAER PATRÓN`** | Circuit breaker/watchdog anti-429 (`kick-watchdog.ts`), principios visuales anti-AI slop (*De-Boxing*, 90/8/2, loop visual JSON). |
| `C:\Users\tomas\Desktop\web-software-factory` | **`EXTRAER PATRÓN`** | `.antigravityignore`, plantilla FinOps sin instancias 24/7 y checkpoint mínimo por run (`state.json`). **Descartada** toda la complejidad multi-sala/XState/Astro/Omni-RAG. |
| `C:\Users\tomas\Documents\yendo_mobile` *(solo `.knowledge/`)* | **`EXTRAER PATRÓN`** | **Gemini 2D Spatial Bounding Boxes (`[ymin, xmin, ymax, xmax]` 0–1000)** para auto-detectar la posición de la webcam del streamer en GTA VI sin calibración manual. Código móvil **descartado 100%**. |

### 2.2 Conexión Solo Lectura a Qdrant Compartido (`[RUNTIME REAL]`)
- **Conexión:** **EXITOSA** (lectura estricta, cero escrituras, cero ingesta nueva).
- **Colecciones auditadas:**
  - `hunthub_omni_rag_v5` (**8.021 puntos**, `green`): destilados principios de mezcla/mastering de **Mike Senior** (`-14.0 LUFS / -1.0 dBTP`, high-pass, compresión 4:1, sidechain ducking `-10 dB`), *Synchresis* y micro-silencio `0.2s` de **Michel Chion**, refuerzo cross-modal de **Celia Hodent** (*The Gamer's Brain*) y narrativa visual.
  - `centinela_knowledge` (**3.624 puntos**, `green`): destilados patrones de ingesta Zero-Disk, FinOps GCP Spot y defensas anti-WAF/HLS.
  - `yendo_obsidian_vault` (**4.776 puntos**, `green`): destilados principios del modelo *Hooked* (Trigger, Action, Variable Reward, Investment) y loops virales.

---

## 3. Bloqueos y Dependencias Faltantes Antes del Prompt 3

1. **Crear `.env` en `c:\Users\tomas\Desktop\clipping studio\.env`** (copiando desde `.env.example` y cargando tus credenciales reales de Vertex AI/GCP o `GEMINI_API_KEY`, `GROQ_API_KEY` y `OPENROUTER_API_KEY`).
2. **Instalar `deno` (v2.3+) para YouTube EJS en `yt-dlp`:**
   ```powershell
   winget install DenoLand.Deno
   ```
   *(Alternativa temporal sin instalar: forzar `--js-runtimes node` en `src/`, ya que Node v24.21.0 está disponible).*
3. **Exportar `cookies.txt` (para VODs de YouTube con login/edad/anti-bot):** desde Firefox o ventana incógnito cerrada de Chrome usando *Get cookies.txt LOCALLY*, guardarlo en una ruta local (ej. `context/cookies.txt`, ya protegido por `.gitignore`) y apuntar `YT_COOKIES_PATH` en `.env`.
4. **Opcional para VODs de Twitch (`Modo B-Lite`):** Descargar `TwitchDownloaderCLI.exe` (MIT, `lay295/TwitchDownloader`) y agregarlo al `PATH` si vas a minar chat de Twitch (para VODs/streams de YouTube no hace falta).
5. **Paquetes de ASR (`onnx-asr` / `faster-whisper`) y SDK LLM (`google-genai`):** Se instalarán en `.venv` al inicio del Prompt 3 cuando implementemos `miner.py`, `curator.py` y `renderer.py` (se mantuvieron fuera de Hito 0 para conservar el entorno liviano y rápido).
6. **Recargar ventana de Antigravity IDE:** Para que el servidor `ffmpeg-mcp-lite` recién agregado a `C:\Users\tomas\.gemini\config\mcp_config.json` aparezca activo en tu panel de MCP Servers junto a `context7`.
