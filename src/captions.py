"""SOTA discrete word-highlight ASS subtitles (Montserrat Black, zero \\kf, Streamer-Only, centered inside Red Safe Box, Hito 2.0)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pysubs2

from src.schemas import AnnotatedTranscript, EditPlan, WordTimestamp

_FILLERS = {"um", "uh", "uhh", "umm", "er", "erm"}
_ALLOWED_SINGLE_LETTERS = {"i", "a", "w", "l"}
_PROFANITY_RE = re.compile(r"^(fuck\w*|shit\w*|bitch\w*|cunt\w*|nigg\w*|whore\w*|slut\w*|motherfuck\w*)$", re.IGNORECASE)
_IMPACT_WORDS = {
    "WTF",
    "BRO",
    "NO",
    "WAY",
    "INSANE",
    "WASTED",
    "DEAD",
    "BOOM",
    "SHIT",
    "FUCK",
    "HOLY",
    "WHAT",
    "CRAZY",
    "CRASH",
    "EXPLODE",
    "GATOR",
    "UFO",
    "DEXTER",
}


def _clean_ass_bgr(hex_str: str, fallback: str) -> str:
    cleaned = hex_str.strip().upper().replace("&H", "").replace("&", "")
    if len(cleaned) == 8 and all(c in "0123456789ABCDEF" for c in cleaned):
        return f"&H{cleaned[2:]}&"  # ASS inline \c uses &HBBGGRR&
    if len(cleaned) == 6 and all(c in "0123456789ABCDEF" for c in cleaned):
        return f"&H{cleaned}&"
    return fallback


def _chunk_words_strict(
    words: list[WordTimestamp],
    max_words: int = 3,
    max_chars: int = 16,
) -> list[list[WordTimestamp]]:
    """Group streamer words into punchy 2-3 word chunks <= 16 chars for strict centering inside [48..888]."""
    chunks: list[list[WordTimestamp]] = []
    cur: list[WordTimestamp] = []
    for w in words:
        if cur:
            gap = w.start - cur[-1].end
            cand_len = len(" ".join([x.word for x in cur] + [w.word]))
            if (
                len(cur) >= max_words
                or cand_len > max_chars
                or gap >= 0.45
                or cur[-1].word.endswith((".", "!", "?"))
            ):
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def _is_impact_word(word_txt: str, abs_start: float, abs_end: float, shout_ranges: list[tuple[float, float]]) -> bool:
    norm = re.sub(r"[^A-Z0-9]", "", word_txt.upper())
    if word_txt.endswith("!") or norm in _IMPACT_WORDS:
        return True
    return any(not (abs_end <= s or abs_start >= e) for s, e in shout_ranges)


def remap_time_through_segments(rel_t: float, kept_segments: list[tuple[float, float]] | None) -> float | None:
    """Map a clip-relative timestamp through selective jump-cut segments into final output timeline."""
    if not kept_segments:
        return rel_t
    offset = 0.0
    for seg_s, seg_e in kept_segments:
        if rel_t < seg_s:
            return round(offset, 3)
        if seg_s <= rel_t <= seg_e:
            return round(offset + (rel_t - seg_s), 3)
        offset += max(0.0, seg_e - seg_s)
    return None


def _mask_opening_profanity(word: str, rel_start: float, shield_sec: float = 7.5) -> str:
    if rel_start > shield_sec:
        return word
    m = re.match(r"^([^A-Za-z]*)([A-Za-z']+)([^A-Za-z]*)$", word)
    if not m:
        return word
    pre, core, post = m.groups()
    if _PROFANITY_RE.match(core):
        return f"{pre}{core[0]}{'*' * max(2, len(core) - 1)}{post}"
    return word


def generate_karaoke_ass(
    transcript: AnnotatedTranscript,
    edit_plan: EditPlan,
    out_ass: Path,
    captions_cfg: dict[str, Any] | None = None,
    kept_segments: list[tuple[float, float]] | None = None,
    censor_opening_sec: float = 7.5,
) -> Path:
    """Build discrete word-by-word Montserrat Black subs (zero \\kf) centered at (540, pos_y) inside Red Safe Box."""
    cfg = captions_cfg or {}
    font_name = str(cfg.get("font_name", "Montserrat Black"))
    font_size = int(cfg.get("font_size", 68))
    uppercase = bool(cfg.get("uppercase", True))
    max_words = max(2, min(3, int(cfg.get("max_words_per_chunk", 3))))
    max_chars = max(12, min(18, int(cfg.get("max_chars_per_chunk", 16))))
    skip_npc = bool(cfg.get("skip_npc_dialogue", True))
    outline_w = float(cfg.get("outline_width", 6))
    shadow_d = float(cfg.get("shadow", 3))
    pos_x = int(cfg.get("pos_x", 540))
    pos_y = int(cfg.get("pos_y", 1120))

    base_bgr = _clean_ass_bgr(str(cfg.get("primary_color", "&H00FFFFFF")), "&HFFFFFF&")
    active_bgr = _clean_ass_bgr(str(cfg.get("highlight_color", "&H0000FFFF")), "&H00FFFF&")
    impact_bgr = _clean_ass_bgr(str(cfg.get("impact_color", "&H0055FF00")), "&H55FF00&")

    subs = pysubs2.SSAFile()
    subs.info["PlayResX"] = "1080"
    subs.info["PlayResY"] = "1920"
    subs.info["WrapStyle"] = "2"
    subs.info["ScaledBorderAndShadow"] = "yes"

    style = pysubs2.SSAStyle(
        fontname=font_name,
        fontsize=font_size,
        primarycolor=pysubs2.Color(255, 255, 255, 0),
        secondarycolor=pysubs2.Color(255, 255, 255, 0),
        outlinecolor=pysubs2.Color(0, 0, 0, 0),
        backcolor=pysubs2.Color(0, 0, 0, 128),
        bold=True,
        borderstyle=1,
        outline=outline_w,
        shadow=shadow_d,
        alignment=pysubs2.Alignment.MIDDLE_CENTER,
        marginl=48,
        marginr=48,
        marginv=48,
    )
    subs.styles["Default"] = style

    clip_start = edit_plan.start_trim
    clip_end = edit_plan.end_trim
    total_out_dur = (
        round(sum(max(0.0, e - s) for s, e in kept_segments), 3)
        if kept_segments
        else max(0.5, clip_end - clip_start)
    )
    shout_ranges = [(e.start, e.end) for e in transcript.non_verbal_events if e.kind == "shouting"]

    filtered: list[tuple[WordTimestamp, float, float]] = []
    for w in transcript.words:
        if skip_npc and w.is_npc_or_other:
            continue
        if w.end <= clip_start + 0.02 or w.start >= clip_end - 0.02:
            continue
        norm = re.sub(r"[^\w']", "", w.word.lower())
        if not norm or norm in _FILLERS:
            continue
        if len(norm) == 1 and norm not in _ALLOWED_SINGLE_LETTERS:
            continue
        raw_s = max(0.0, round(w.start - clip_start, 3))
        raw_e = max(raw_s + 0.06, round(w.end - clip_start, 3))
        mapped_s = remap_time_through_segments(raw_s, kept_segments)
        mapped_e = remap_time_through_segments(raw_e, kept_segments)
        if mapped_s is None or mapped_e is None:
            continue
        rel_s = max(0.0, min(total_out_dur - 0.05, mapped_s))
        rel_e = min(total_out_dur, max(rel_s + 0.06, mapped_e))
        clean_word = _mask_opening_profanity(w.word, rel_s, shield_sec=censor_opening_sec)
        shifted = WordTimestamp(
            word=clean_word,
            start=rel_s,
            end=rel_e,
            confidence=w.confidence,
            is_npc_or_other=False,
            streamer_asd_score=w.streamer_asd_score,
        )
        filtered.append((shifted, w.start, w.end))

    words_only = [f[0] for f in filtered]
    abs_lookup = {(f[0].start, f[0].word): (f[1], f[2]) for f in filtered}
    chunks = _chunk_words_strict(words_only, max_words=max_words, max_chars=max_chars)

    for chunk in chunks:
        display_words = [
            (w.word.upper() if uppercase else w.word).replace("{", "").replace("}", "") for w in chunk
        ]
        for idx, w in enumerate(chunk):
            ev_start_ms = round(w.start * 1000)
            if idx + 1 < len(chunk):
                next_ms = round(chunk[idx + 1].start * 1000)
                ev_end_ms = max(ev_start_ms + 40, next_ms)
            else:
                ev_end_ms = max(ev_start_ms + 120, round(w.end * 1000))

            abs_s, abs_e = abs_lookup.get((w.start, w.word), (clip_start + w.start, clip_start + w.end))
            hi_bgr = impact_bgr if _is_impact_word(display_words[idx], abs_s, abs_e, shout_ranges) else active_bgr

            pop_tag = r"\t(0,70,\fscx112\fscy112)\t(70,140,\fscx100\fscy100)" if idx == 0 else ""
            prefix = f"{{\\an5\\pos({pos_x},{pos_y}){pop_tag}}}"

            tokens_fmt: list[str] = []
            for j, tok in enumerate(display_words):
                color = hi_bgr if j == idx else base_bgr
                tokens_fmt.append(f"{{\\c{color}}}{tok}")
            line_text = prefix + " ".join(tokens_fmt)
            subs.events.append(pysubs2.SSAEvent(start=ev_start_ms, end=ev_end_ms, text=line_text, style="Default"))

    out_ass.parent.mkdir(parents=True, exist_ok=True)
    subs.save(str(out_ass), encoding="utf-8")
    return out_ass
