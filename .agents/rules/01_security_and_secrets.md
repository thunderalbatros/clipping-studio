---
trigger: always_on
description: Secretos, supply-chain, licencias KS-4 y solo-lectura en repos externos.
---

# Regla 01 — Seguridad, Secretos y Licencias KS-4
1. **Cero secretos:** Nunca imprimir/loguear/commitear `.env`, `*cookies*.txt`, API keys ni credenciales GCP; reportar solo `PRESENTE`/`FALTANTE`.
2. **Exclusión:** `.env`, `*cookies*.txt`, credenciales `*.json`, `_references/`, `models/`, `runs/` siempre en `.gitignore` y `.antigravityignore`.
3. **Prohibido `npx skills add` a ciegas:** Auditar línea por línea y destilar manualmente a `.agents/skills/`.
4. **KS-4 (Comercial):** Prohibido `CC-BY-NC` (`bria-rmbg`, `OmniVoice`, `MatAnyone 2`, `DA3 Large/Giant`, MDX-Net NC). Solo MIT/Apache-2.0/Unlicense (`birefnet-general-lite`, `auto-editor`, `fastembed`, `sqlite-vec`).
5. **Solo lectura externa:** Nunca escribir en `streamerbot`, `centinela-studio`, `web-software-factory`, `yendo_mobile`, `~/.gemini/config/` ni Qdrant Cloud. SQLite externo estrictamente `?mode=ro&immutable=1`.
