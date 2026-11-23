---
trigger: always_on
description: Guardrails obligatorios de seguridad, manejo de secretos, supply-chain de skills y licencias comerciales.
---

# Regla 01 — Seguridad, Secretos y Licencias Comerciales

1. **Cero exposición de secretos:** Nunca imprimir, loguear ni pegar en respuestas o commits valores de `.env`, `cookies.txt`, tokens (`GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `QDRANT_API_KEY`, `HF_TOKEN`, credenciales GCP). Reportar únicamente `PRESENTE` o `FALTANTE`.
2. **Archivos sensibles protegidos:** Verificar que `.env`, `*cookies*.txt`, `*.json` de credenciales, `_references/` y `runs/` permanezcan listados en `.gitignore` y `.antigravityignore`.
3. **Prohibido instalar skills públicas a ciegas (`npx skills add`):** Toda skill o script externo debe auditarse línea por línea (*Read-the-Whole-File*) y destilarse manualmente a texto plano en `.agents/skills/`.
4. **Lista negra de licencias No Comerciales (Kill-Switch KS-4):** Prohibido introducir en el pipeline monetizable modelos con pesos `CC-BY-NC` o licencias académicas restrictivas (ej.: `k2-fsa/OmniVoice`, `MatAnyone 2`, `Depth Anything 3 Large/Giant`, checkpoints MDX-Net sin licencia comercial explícita).
5. **Solo lectura sobre proyectos externos:** Prohibido modificar archivos en otros proyectos locales (`streamerbot`, `centinela-studio`, `web-software-factory`, `yendo_mobile`) o escribir en la instancia compartida de Qdrant durante v1.
