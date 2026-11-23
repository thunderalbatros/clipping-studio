"""Karaoke ASS subtitle generator calibrated to 2026 universal 3-network safe zones (Hito 1.4)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pysubs2

from src.schemas import AnnotatedTranscript, EditPlan, WordTimestamp

_FILLERS = {"um", "uh", "uhh", "umm", "er", "erm"}


def _parse_ass_color(hex_str: str, fallback: pysubs2.Color) -> pysubs2.Color:
    """Parse ASS '&HAABBGGRR' color string into pysubs2.Color(r, g, b, a)."""
    cleaned = hex_str.strip().upper().replace("&H", "").replace("&", "")
    if len(cleaned) == 8 and all(c in "0123456789ABCDEF" for c in cleaned):
        aa = int(cleaned[0:2], 16)
        bb = int(cleaned[2:4], 16)
        gg = int(cleaned[4:6], 16)
        rr = int(cleaned[6:8], 16)
        return pysubs2.Color(rr, gg, bb, aa)
    return fallback


def _chunk_words(words: list[WordTimestamp], max_per_chunk: int = 3) -> list[list[WordTimestamp]]:
    chunks: list[list[WordTimestamp]] = []
    cur: list[WordTimestamp] = []
    for w in words:
        if cur:
            gap = w.start - cur[-1].end
            if len(cur) >= max_per_chunk or gap >= 0.45 or cur[-1].word.endswith((".", "!", "?")):
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def generate_karaoke_ass(
    transcript: AnnotatedTranscript,
    edit_plan: EditPlan,
    out_ass: Path,
    captions_cfg: dict[str, Any] | None = None,
) -> Path:
    """Build PlayRes 1080x1920 Segoe UI Black karaoke subs inside Red Safe Box (MarginR>=200, MarginV>=720)."""
    cfg = captions_cfg or {}
    font_name = str(cfg.get("font_name", "Segoe UI Black"))
    font_size = int(cfg.get("font_size", 68))
    max_words = max(2, min(4, int(cfg.get("max_words_per_chunk", 3))))
    skip_npc = bool(cfg.get("skip_npc_dialogue", True))

    active_color = _parse_ass_color(str(cfg.get("highlight_color", "&H0000E5FF")), pysubs2.Color(255, 229, 0, 0))
    base_color = _parse_ass_color(str(cfg.get("primary_color", "&H00FFFFFF")), pysubs2.Color(255, 255, 255, 0))
    outline_color = _parse_ass_color(str(cfg.get("outline_color", "&H00000000")), pysubs2.Color(0, 0, 0, 0))
    outline_w = float(cfg.get("outline_width", 4))

    subs = pysubs2.SSAFile()
    subs.info["PlayResX"] = "1080"
    subs.info["PlayResY"] = "1920"
    subs.info["WrapStyle"] = "2"
    subs.info["ScaledBorderAndShadow"] = "yes"

    # In ASS karaoke (\kf), words sweep from SecondaryColour (base_color) -> PrimaryColour (active_color).
    style = pysubs2.SSAStyle(
        fontname=font_name,
        fontsize=font_size,
        primarycolor=active_color,
        secondarycolor=base_color,
        outlinecolor=outline_color,
        backcolor=pysubs2.Color(0, 0, 0, 140),
        bold=False,
        outline=outline_w,
        shadow=1.5,
        alignment=pysubs2.Alignment.BOTTOM_CENTER,
        marginl=60,   # >= 48px Red left safe margin
        marginr=204,  # >= 200px (>= 192px Red right safe margin)
        marginv=760,  # Bottom baseline at y=1160 inside [288..1248] universal box
    )
    subs.styles["Default"] = style

    clip_start = edit_plan.start_trim
    clip_end = edit_plan.end_trim
    clip_dur = max(0.5, clip_end - clip_start)

    filtered: list[WordTimestamp] = []
    for w in transcript.words:
        if skip_npc and w.is_npc_or_other:
            continue
        if w.end <= clip_start + 0.02 or w.start >= clip_end - 0.02:
            continue
        norm = re.sub(r"[^\w']", "", w.word.lower())
        if not norm or norm in _FILLERS:
            continue
        rel_s = max(0.0, round(w.start - clip_start, 3))
        rel_e = min(clip_dur, max(rel_s + 0.06, round(w.end - clip_start, 3)))
        filtered.append(WordTimestamp(word=w.word, start=rel_s, end=rel_e, confidence=w.confidence, is_npc_or_other=False))

    chunks = _chunk_words(filtered, max_per_chunk=max_words)
    for chunk in chunks:
        c_start_ms = round(chunk[0].start * 1000)
        c_end_ms = max(c_start_ms + 200, round(chunk[-1].end * 1000))
        parts: list[str] = []
        cursor = chunk[0].start
        for w in chunk:
            dur_s = max(0.08, (w.end - max(cursor, w.start)))
            kf_cs = max(8, round(dur_s * 100))
            safe_word = w.word.replace("{", "").replace("}", "")
            parts.append(f"{{\\kf{kf_cs}}}{safe_word}")
            cursor = max(cursor + dur_s, w.end)
        text = " ".join(parts)
        subs.events.append(pysubs2.SSAEvent(start=c_start_ms, end=c_end_ms, text=text, style="Default"))

    out_ass.parent.mkdir(parents=True, exist_ok=True)
    subs.save(str(out_ass), encoding="utf-8")
    return out_ass
