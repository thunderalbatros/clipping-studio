"""Comprehensive unit tests for `src/trends.py` (YouTube Data API v3, Kick Clips, Serper Social & Living Brief)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import src.trends as tr_mod


class _FakeResp:
    def __init__(self, status_code: int, payload: dict[str, Any], text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._payload


def test_infer_hook_style_all_categories() -> None:
    assert tr_mod._infer_hook_style("Rockstar hid this Area 51 secret")[0] == "Secret / Lore Discovery"
    assert tr_mod._infer_hook_style("GTA 6 car physics glitch broke the bridge")[0] == "Physics Chaos / Viral Bug"
    assert tr_mod._infer_hook_style("6 star police helicopter heist escape")[0] == "High-Stakes Action / Chase"
    assert tr_mod._infer_hook_style("Funny alligator NPC reaction in Vice City")[0] == "Comedic Chaos / NPC AI"
    assert tr_mod._infer_hook_style("Lucia driving at sunset")[0] == "Gameplay Moment"


def test_youtube_kick_social_and_living_brief_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tr_mod, "load_dotenv", lambda *a, **kw: False)

    # 1. Missing YOUTUBE_API_KEY returns empty list cleanly
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    assert tr_mod.fetch_youtube_gta6_shorts_trends() == []

    # 2. Full YouTube search + videos statistics calculation
    monkeypatch.setenv("YOUTUBE_API_KEY", "test_yt_key_dummy")
    pub_iso = (datetime.now(UTC) - timedelta(hours=2)).isoformat()

    def _fake_get(url: str, params: dict[str, Any] | None = None, timeout: int = 15) -> _FakeResp:
        assert timeout <= 20
        if url.endswith("/search"):
            return _FakeResp(200, {"items": [{"id": {"videoId": "vid_001"}}, {"id": {"videoId": "vid_002"}}]})
        return _FakeResp(
            200,
            {
                "items": [
                    {
                        "id": "vid_001",
                        "snippet": {"title": "GTA 6 Police Chase Escape!", "channelTitle": "SpeedClips", "publishedAt": pub_iso},
                        "statistics": {"viewCount": "24000", "likeCount": "1800"},
                        "contentDetails": {"duration": "PT34S"},
                    },
                    {
                        "id": "vid_002",
                        "snippet": {"title": "GTA 6 Secret UFO Found", "channelTitle": "LoreHub", "publishedAt": pub_iso},
                        "statistics": {"viewCount": "8000", "likeCount": "600"},
                        "contentDetails": {"duration": "PT28S"},
                    },
                ]
            },
        )

    monkeypatch.setattr(tr_mod.curl_requests, "get", _fake_get)
    yt_refs = tr_mod.fetch_youtube_gta6_shorts_trends(hours_back=24, max_results=5)
    assert len(yt_refs) == 2
    assert yt_refs[0]["source_id"] == "vid_001"
    assert yt_refs[0]["duration_sec"] == 34.0
    assert yt_refs[0]["views_per_hour"] > yt_refs[1]["views_per_hour"]
    assert yt_refs[0]["topic"] == "High-Stakes Action / Chase"

    # 3. Kick whitelist trends
    monkeypatch.setattr(
        tr_mod,
        "load_whitelist",
        lambda: {
            "xqc": {"platforms": ["kick", "twitch"], "kick_slug": "xqc"},
            "jynxzi": {"platforms": ["twitch"]},
        },
    )
    monkeypatch.setattr(
        tr_mod,
        "fetch_kick_gta6_clips",
        lambda slug, time_window="week", limit=5: [
            {
                "id": f"kick_{slug}_1",
                "title": "Insane physics bug on highway",
                "page_url": f"https://kick.com/{slug}/clips/1",
                "duration": 29.0,
                "views": 15000,
                "views_per_hour": 1250.0,
            }
        ],
    )
    kick_refs = tr_mod.fetch_kick_whitelist_trends(limit_per_streamer=2)
    assert len(kick_refs) == 1
    assert kick_refs[0]["platform"] == "kick_clips"
    assert kick_refs[0]["topic"] == "Physics Chaos / Viral Bug"

    # 4. Social network Serper outliers
    monkeypatch.setenv("SERPER_API_KEY", "test_serper_dummy")

    def _fake_post(url: str, headers: dict[str, str], json: dict[str, Any], timeout: int = 15) -> _FakeResp:
        assert timeout <= 20
        q = str(json.get("q") or "")
        plat_slug = "tiktok" if "tiktok" in q else "x"
        return _FakeResp(
            200,
            {"organic": [{"title": f"Funny alligator in GTA 6 ({plat_slug})", "link": f"https://example.com/{plat_slug}/1", "displayLink": plat_slug}]},
        )

    monkeypatch.setattr(tr_mod.curl_requests, "post", _fake_post)
    soc_refs = tr_mod.fetch_social_network_outliers(limit_per_network=1)
    assert len(soc_refs) == 4

    # 5. End-to-end `run_trends_radar` writing isolated temp files + idempotent living brief update
    golden_tmp = tmp_path / "golden_references.json"
    brief_tmp = tmp_path / "living_brief.md"
    brief_tmp.write_text("# Living Brief\n\n## Bloque A\nBase notes.\n\n## Bloque D — Radar Automático 48h (Golden References)\nOld line\n", encoding="utf-8")

    monkeypatch.setattr(tr_mod, "GOLDEN_REFS_PATH", golden_tmp)
    monkeypatch.setattr(tr_mod, "LIVING_BRIEF_PATH", brief_tmp)
    monkeypatch.setattr(tr_mod, "insert_telemetry", lambda *a, **kw: None)

    out = tr_mod.run_trends_radar(top_n=5, all_networks=True, refresh_style_dna=False)
    assert golden_tmp.is_file()
    saved = json.loads(golden_tmp.read_text(encoding="utf-8"))
    assert len(saved["golden_references"]) == 5
    assert "youtube_shorts" in out["sources"]["networks_covered"]
    updated_brief = brief_tmp.read_text(encoding="utf-8")
    assert "Old line" not in updated_brief
    assert "SpeedClips" in updated_brief
