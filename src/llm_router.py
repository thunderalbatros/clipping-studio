"""Resilient multimodal structured-output LLM router (Vertex gemini-3.8-flash -> 2.5-flash -> Groq -> OpenRouter)."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any, TypeVar

from dotenv import load_dotenv
from google import genai
from google.genai import types
from openai import OpenAI
from pydantic import BaseModel

from src.db import get_daily_llm_spend_usd, insert_telemetry

T = TypeVar("T", bound=BaseModel)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_TRANSIENT_MARKERS = ("429", "503", "rate limit", "resource_exhausted", "unavailable", "timeout")
_DOTENV_LOADED = False
_STATIC_BIBLE_PREFIX: str | None = None
_VERTEX_CLIENTS: dict[tuple[str, str], genai.Client] = {}


def _get_vertex_client(project: str, location: str) -> genai.Client:
    key = (project, location)
    if key not in _VERTEX_CLIENTS:
        _VERTEX_CLIENTS[key] = genai.Client(vertexai=True, project=project, location=location)
    return _VERTEX_CLIENTS[key]


def _estimate_vertex_cost_usd(prompt_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost using Gemini 3.8 Flash rates ($0.75/1M uncached in, $0.075/1M cached in, $3.75/1M out)."""
    uncached = max(0, prompt_tokens - cached_tokens)
    return round((uncached * 0.75 + cached_tokens * 0.075 + output_tokens * 3.75) / 1_000_000.0, 6)


def _ensure_dotenv() -> None:
    """Load project .env once with override=True so stale OS keys do not shadow it."""
    global _DOTENV_LOADED
    if not _DOTENV_LOADED:
        env_path = PROJECT_ROOT / ".env"
        load_dotenv(dotenv_path=env_path if env_path.exists() else None, override=True)
        _DOTENV_LOADED = True


def reset_cached_prefix() -> None:
    """Reset in-memory cached static prefix after context files are refreshed."""
    global _STATIC_BIBLE_PREFIX
    _STATIC_BIBLE_PREFIX = None


def build_cached_system_instruction(task_instruction: str) -> str:
    """Prepend identical canonical docs + GTA VI Lore + Golden References (>6144 tokens) for Implicit Caching."""
    global _STATIC_BIBLE_PREFIX
    if _STATIC_BIBLE_PREFIX is None:
        style_path = PROJECT_ROOT / "docs" / "style_bible.md"
        brief_path = PROJECT_ROOT / "context" / "gta6_living_brief.md"
        play_path = PROJECT_ROOT / "docs" / "marketing_playbook.md"
        lore_path = PROJECT_ROOT / "context" / "gta6_lore_index.json"
        golden_path = PROJECT_ROOT / "context" / "golden_references.json"
        dna_path = PROJECT_ROOT / "context" / "viral_style_dna.json"
        algo_path = PROJECT_ROOT / "context" / "platform_algorithms_index.json"
        style_txt = style_path.read_text(encoding="utf-8").strip() if style_path.exists() else ""
        brief_txt = brief_path.read_text(encoding="utf-8").strip() if brief_path.exists() else ""
        play_txt = play_path.read_text(encoding="utf-8").strip() if play_path.exists() else ""
        lore_txt = lore_path.read_text(encoding="utf-8").strip() if lore_path.exists() else "{}"
        golden_txt = golden_path.read_text(encoding="utf-8").strip() if golden_path.exists() else "{}"
        dna_txt = dna_path.read_text(encoding="utf-8").strip() if dna_path.exists() else "{}"
        algo_txt = algo_path.read_text(encoding="utf-8").strip() if algo_path.exists() else "{}"
        _STATIC_BIBLE_PREFIX = (
            "=== CANONICAL STUDIO STYLE BIBLE ===\n"
            f"{style_txt}\n\n"
            "=== CANONICAL MARKETING PLAYBOOK ===\n"
            f"{play_txt}\n\n"
            "=== PLATFORM ALGORITHMS & VIRALITY INDEX (5 NETWORKS) ===\n"
            f"{algo_txt}\n\n"
            "=== DAILY GTA VI LIVING BRIEF ===\n"
            f"{brief_txt}\n\n"
            "=== GTA VI EXPERT LORE & EASTER EGGS INDEX ===\n"
            f"{lore_txt}\n\n"
            "=== TODAY'S VIRAL GOLDEN REFERENCES ===\n"
            f"{golden_txt}\n\n"
            "=== EMPIRICAL VIRAL STYLE DNA (/steal) ===\n"
            f"{dna_txt}\n\n"
            "=== INVARIANT ENGINE RULES ===\n"
            "- Output strictly valid JSON matching the requested Pydantic v2 schema.\n"
            "- Reject non-gameplay footage (trailers, YouTube reactions, menus, BRB) when auditing gameplay VODs.\n"
            "- Reject meta-stream talk (gifted subs, donation shoutouts, raid welcomes) from viral short clips.\n"
            "- Enforce anti-DMCA hygiene: reject audible licensed radio/club/trailer music, especially for clips >60s.\n"
        )
    return f"{_STATIC_BIBLE_PREFIX}\n=== TASK INSTRUCTION ===\n{task_instruction.strip()}"


def _is_transient_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _TRANSIENT_MARKERS)


def _strip_json_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = _CODE_FENCE_RE.sub("", text).strip()
    return text


def _load_image_bytes(item: bytes | Path | str) -> tuple[bytes, str]:
    if isinstance(item, bytes):
        mime = "image/webp" if item[:4] == b"RIFF" else "image/png"
        return item, mime
    p = Path(item)
    data = p.read_bytes()
    ext = p.suffix.lower()
    mime = "image/jpeg" if ext in {".jpg", ".jpeg"} else ("image/webp" if ext == ".webp" else "image/png")
    return data, mime


def _vertex_candidates() -> list[tuple[str, str]]:
    env_loc = os.getenv("GCP_LOCATION", "us-central1").strip() or "us-central1"
    env_model = os.getenv("GEMINI_MODEL", "").strip()
    candidates: list[tuple[str, str]] = []
    if env_model and env_model not in {"gemini-3.8-flash", "gemini-2.5-flash", "gemini-4-flash"}:
        candidates.append((env_model, "global"))
        candidates.append((env_model, env_loc))
    for item in [("gemini-3.8-flash", "global"), ("gemini-2.5-flash", env_loc)]:
        if item not in candidates:
            candidates.append(item)
    return candidates


def _call_vertex_ai(
    prompt: str,
    response_model: type[T],
    system_instruction: str | None,
    max_retries: int,
    images: list[bytes | Path | str | tuple[bytes | Path | str, str]] | None = None,
    media_resolution: str = "MEDIA_RESOLUTION_HIGH",
) -> tuple[T, str]:
    project = os.getenv("GCP_PROJECT_ID", "").strip()
    if not project:
        raise RuntimeError("GCP_PROJECT_ID missing for vertex_ai provider")

    default_res = getattr(types.MediaResolution, media_resolution, types.MediaResolution.MEDIA_RESOLUTION_HIGH)
    contents: Any = prompt
    if images:
        parts: list[Any] = []
        for item in images:
            if isinstance(item, tuple):
                img_obj, item_res_name = item
                item_res = getattr(types.MediaResolution, item_res_name, default_res)
            else:
                img_obj, item_res = item, default_res
            raw, mime = _load_image_bytes(img_obj)
            parts.append(types.Part.from_bytes(data=raw, mime_type=mime, media_resolution=item_res))
        parts.append(prompt)
        contents = parts

    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.2,
        response_mime_type="application/json",
        response_json_schema=response_model.model_json_schema(),
    )

    daily_cap = float(os.getenv("LLM_DAILY_BUDGET_USD", "2.50") or 2.50)
    spent_today = get_daily_llm_spend_usd()
    if spent_today >= daily_cap:
        raise RuntimeError(f"Daily Vertex AI USD budget cap reached (${spent_today:.4f} >= ${daily_cap:.2f}); falling back.")

    last_exc: Exception | None = None
    for model, location in _vertex_candidates():
        for attempt in range(max_retries + 1):
            t0 = time.perf_counter()
            try:
                client = _get_vertex_client(project, location)
                resp = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )
                latency_ms = int((time.perf_counter() - t0) * 1000)
                parsed = response_model.model_validate_json(_strip_json_fences(resp.text or ""))
                um = getattr(resp, "usage_metadata", None)
                prompt_toks = int(getattr(um, "prompt_token_count", 0) or 0)
                cached_toks = int(getattr(um, "cached_content_token_count", 0) or 0)
                output_toks = int(getattr(um, "candidates_token_count", 0) or 0)
                est_usd = _estimate_vertex_cost_usd(prompt_toks, cached_toks, output_toks)
                insert_telemetry(
                    "llm_router",
                    "llm_usage",
                    {
                        "provider": "vertex_ai",
                        "model": model,
                        "location": location,
                        "prompt_tokens": prompt_toks,
                        "cached_tokens": cached_toks,
                        "output_tokens": output_toks,
                        "est_cost_usd": est_usd,
                        "latency_ms": latency_ms,
                    },
                )
                print(
                    f"[LLM_ROUTER] provider=vertex_ai model={model} loc={location} "
                    f"latency_ms={latency_ms} prompt_tokens={prompt_toks} cached_tokens={cached_toks or None} "
                    f"est_usd=${est_usd:.5f} status=OK"
                )
                return parsed, f"vertex_ai:{model}"
            except Exception as exc:
                last_exc = exc
                if "refresh" in str(exc).lower() and os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
                    os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
                    _VERTEX_CLIENTS.pop((project, location), None)
                    continue
                if _is_transient_error(exc) and attempt < max_retries:
                    time.sleep(1.5 * (2**attempt))
                    continue
                break
    assert last_exc is not None
    raise last_exc


def _call_openai_compat(
    provider_name: str,
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    response_model: type[T],
    system_instruction: str | None,
    max_retries: int,
    images: list[bytes | Path | str | tuple[bytes | Path | str, str]] | None = None,
) -> tuple[T, str]:
    if not api_key:
        raise RuntimeError(f"API key missing for {provider_name}")

    schema_json = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
    sys_msg = (
        f"{system_instruction or 'You are a precise structured JSON engine.'}\n\n"
        f"Respond ONLY with a valid JSON object matching this exact JSON Schema:\n{schema_json}"
    )

    user_content: Any = prompt
    if images and provider_name == "openrouter":
        blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for item in images:
            img_obj = item[0] if isinstance(item, tuple) else item
            raw, mime = _load_image_bytes(img_obj)
            b64 = base64.b64encode(raw).decode("ascii")
            blocks.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        user_content = blocks

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=45.0)
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        t0 = time.perf_counter()
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0.2,
                max_tokens=2048,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": sys_msg},
                    {"role": "user", "content": user_content},
                ],
            )
            latency_ms = int((time.perf_counter() - t0) * 1000)
            content = resp.choices[0].message.content or ""
            parsed = response_model.model_validate_json(_strip_json_fences(content))
            print(f"[LLM_ROUTER] provider={provider_name} model={model} latency_ms={latency_ms} status=OK")
            return parsed, provider_name
        except Exception as exc:
            last_exc = exc
            if _is_transient_error(exc) and attempt < max_retries:
                time.sleep(1.5 * (2**attempt))
                continue
            raise
    assert last_exc is not None
    raise last_exc


def generate_structured(
    prompt: str,
    response_model: type[T],
    system_instruction: str | None = None,
    force_provider: str | None = None,
    providers_order: list[str] | None = None,
    max_retries: int = 2,
    images: list[bytes | Path | str | tuple[bytes | Path | str, str]] | None = None,
    media_resolution: str = "MEDIA_RESOLUTION_HIGH",
) -> tuple[T, str]:
    """Generate a Pydantic-validated response using Vertex (3.8-flash -> 2.5-flash) -> Groq -> OpenRouter."""
    _ensure_dotenv()

    default_order = ["vertex_ai", "openrouter"] if images else ["vertex_ai", "groq", "openrouter"]
    order = [force_provider] if force_provider else (providers_order or default_order)
    errors: list[str] = []

    for provider in order:
        try:
            if provider == "vertex_ai":
                return _call_vertex_ai(
                    prompt,
                    response_model,
                    system_instruction,
                    max_retries,
                    images=images,
                    media_resolution=media_resolution,
                )
            if provider == "groq":
                groq_model = os.getenv("GROQ_MODEL", "").strip()
                if not groq_model or groq_model == "llama-3.3-70b-versatile":
                    groq_model = "openai/gpt-oss-120b"
                return _call_openai_compat(
                    provider_name="groq",
                    api_key=os.getenv("GROQ_API_KEY", "").strip(),
                    base_url="https://api.groq.com/openai/v1",
                    model=groq_model,
                    prompt=prompt,
                    response_model=response_model,
                    system_instruction=system_instruction,
                    max_retries=max_retries,
                    images=None,
                )
            if provider == "openrouter":
                return _call_openai_compat(
                    provider_name="openrouter",
                    api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
                    base_url="https://openrouter.ai/api/v1",
                    model=os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash").strip() or "google/gemini-2.5-flash",
                    prompt=prompt,
                    response_model=response_model,
                    system_instruction=system_instruction,
                    max_retries=max_retries,
                    images=images,
                )
            raise ValueError(f"Unknown provider: {provider}")
        except Exception as exc:
            err_name = type(exc).__name__
            err_msg = str(exc).splitlines()[0][:140]
            print(f"[LLM_ROUTER] provider={provider} status=FAIL err={err_name}: {err_msg}")
            errors.append(f"{provider}: {err_name}")

    raise RuntimeError(f"All LLM providers failed: {'; '.join(errors)}")


def _run_self_test() -> int:
    from src.schemas import BlindCriticResult

    prompt_1 = (
        "Evaluate a hypothetical 30s GTA VI vertical clip with clip_id='selftest_001'. "
        "Assume it opens cleanly on an intense police chase, passes all gates, and scores 0.88 overall."
    )
    prompt_2 = (
        "Evaluate a hypothetical 28s GTA VI vertical clip with clip_id='selftest_002'. "
        "Assume it opens cleanly on an alligator chaos encounter, passes all gates, and scores 0.86 overall."
    )
    sys_cached = build_cached_system_instruction("Return a concise BlindCriticResult evaluation.")

    print("=== LLM ROUTER SELF-TEST 1: VERTEX AI (3.8-flash @ global + IMPLICIT CACHE) ===")
    res_v1, prov_v1 = generate_structured(prompt_1, BlindCriticResult, system_instruction=sys_cached, force_provider="vertex_ai")
    res_v2, prov_v2 = generate_structured(prompt_2, BlindCriticResult, system_instruction=sys_cached, force_provider="vertex_ai")
    print(f"VERTEX_PARSED_1: clip_id={res_v1.clip_id} overall={res_v1.overall} approved={res_v1.approved} prov={prov_v1}")
    print(f"VERTEX_PARSED_2: clip_id={res_v2.clip_id} overall={res_v2.overall} approved={res_v2.approved} prov={prov_v2}")

    print("=== LLM ROUTER SELF-TEST 2: FORCED GROQ ===")
    res_g, prov_g = generate_structured(prompt_1, BlindCriticResult, system_instruction="Return a concise BlindCriticResult.", force_provider="groq")
    print(f"GROQ_PARSED: clip_id={res_g.clip_id} overall={res_g.overall} approved={res_g.approved} prov={prov_g}")

    print("=== LLM ROUTER SELF-TEST 3: SIMULATED PRIMARY FAILURE -> AUTO FALLBACK ===")
    orig_proj = os.environ.get("GCP_PROJECT_ID", "")
    os.environ["GCP_PROJECT_ID"] = ""
    try:
        res_fb, prov_fb = generate_structured(
            prompt_1,
            BlindCriticResult,
            system_instruction="Return a concise BlindCriticResult.",
            providers_order=["vertex_ai", "groq"],
        )
        print(f"FALLBACK_PARSED: clip_id={res_fb.clip_id} overall={res_fb.overall} prov={prov_fb}")
    finally:
        if orig_proj:
            os.environ["GCP_PROJECT_ID"] = orig_proj

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Clipping Studio Resilient LLM Router")
    parser.add_argument("--self-test", action="store_true", help="Run live structured-output tests on Vertex AI and Groq")
    args = parser.parse_args()
    if args.self_test:
        return _run_self_test()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
