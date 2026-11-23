# Guía de Optimización de Tokens en Antigravity IDE (-60% a -80% Cuota)

Esta guía documenta **Frente A** (ya automatizado en este repo) y **Frente B** (ajustes manuales opcionales en tu instalación global `~/.gemini/config/` que el agente no modifica automáticamente por Regla 01).

---

## Frente A — Automatizado en Clipping Studio (Ya Activo)

1. **Hook `PreToolUse` con `rtk 1.2.0`:**
   - Configurado en [.agents/hooks.json](../.agents/hooks.json) apuntando a [scripts/rtk_pretool_hook.py](../scripts/rtk_pretool_hook.py).
   - Comprime salidas verbosas de `git` (lectura), `pytest` (preservando `.venv\Scripts\pytest.exe` vía `rtk test`), `ruff` y `uv` en un **60%–90%**.
   - Hace bypass automático ante pipes (`|`), redirecciones (`>`, `2>&1`), encadenamientos (`;`, `&&`), variables `$env:` y comandos mutantes de Git.
   - Verificá ahorros acumulados en cualquier momento con `rtk gain`.
2. **Carga progresiva (`always_on < 6.5 KB`):**
   - `AGENTS.md` + `.agents/rules/01..04.md` fueron compactados a `< 6.5 KB` totales (~55% menos tokens por turno).
   - Los runbooks detallados viven en las **7 Skills** bajo `.agents/skills/*/SKILL.md` y se cargan solo bajo demanda.
3. **`.antigravityignore` endurecido:**
   - Excluye `docs/01_*.md`..`docs/05_*.md` (138 KB de reportes históricos ya destilados en `docs/*_bible.md` y `runs/studio.db`), `requirements.lock`, `.pytest_cache/`, `.ruff_cache/`, `*.log`, `*.db-wal`, `*.db-shm`.
4. **RAG Local + Puente Federado sin MCP extra:**
   - `src/rag_engine.py` consulta `runs/studio.db` (`FTS5` + `sqlite-vec`) y opcionalmente `web-software-factory/.cache/rag_hot.sqlite` / `rag_library.sqlite` en modo `?mode=ro&immutable=1` desde Python local (`0` tools MCP inyectadas en el system prompt).

---

## Frente B — Ajustes Manuales Recomendados en `~/.gemini/config/`

> Ningún archivo fuera de `clipping studio` fue tocado automáticamente. Aplicá estos 3 pasos manualmente cuando trabajes sesiones largas en Clipping Studio:

### 1. Aislar el servidor MCP `web-factory-omni-rag` (~27 tools ahorradas por turno)
`web-factory-omni-rag` inyecta ~27 esquemas de herramientas en **cada turno** aunque no se use.
- **Acción:** Mové la entrada `web-factory-omni-rag` desde tu config global (`~/.gemini/config/mcp_config.json`) hacia el `.mcp.json` local de `C:\Users\tomas\Desktop\web-software-factory\.mcp.json`.
- **Resultado:** En `clipping studio` seguís consultando los 76.672 chunks de `Omni-RAG v6` con `python -m src.cli rag query "..." --federated` (cero costo de tokens por definiciones MCP).

### 2. Archivar temporalmente las 6 skills globales de UE 5.8 / Blender y plugins ociosos
Si estás en sprint de Clipping Studio:
- Mové las carpetas de skills UE5/Blender (`aaa-cutscene-director`, `blender-cinematic-pipeline`, `cinematic-qa-critic`, `reactive-sound-director`, `scene-layout-architect`, `tech-artist-shaders`) a `~/.gemini/config/skills_archived_ue5/` (mismo patrón que `skills_archived_gcp`).
- Deshabilitá en el **Plugins Manager** los plugins que no uses en video (`android-cli`, `firebase`, `google_maps_platform`, `data-agent-kit`, `chrome-devtools` cuando no audites web).
- Dejá activos únicamente los livianos para Clipping Studio: `context7` y `ffmpeg-mcp-lite` (máximo 4 MCPs livianos).

### 3. Disciplina de Sesiones y Selección de Modelo
- **1 conversación = 1 hito acotado** (evitar hilos de 40+ turnos donde el prefijo acumulado encarece cada mensaje).
- Ejecutar **`/compact`** alrededor del turno 12–15 si el hilo se extiende.
- **Selector de modelo en IDE:**
  - **Flash:** edits mecánicos, tests, formateo, docs, CLI queries.
  - **Pro / High:** diseño de arquitectura, debugging complejo, auditoría multimodal de renders.
- **En runtime Python:** el enrutador (`src/llm_router.py`) ya agrupa `style_bible.md` + `marketing_playbook.md` + `design_bible.md` + `context/*.json` en el prefijo estable para maximizar **Implicit Prompt Caching (-90% costo de entrada)** e inyecta solo `<= 550` tokens del RAG por clip.
