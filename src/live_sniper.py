"""Pure-Python Live-Stream Sniper (Twitch IRC + Kick REST/HLS + Hawkes/ChatWave + 90s HLS Ring-Buffer).

Zero Node sidecar, zero Playwright, strictly bounded RAM (<120 MB/stream, <350 MB for 2 streams).
Triggers JIT 90s 1080p60 slices (`t-65s .. t+25s`) on verified chat reaction spikes and feeds them
into the deterministic Clipping Studio pipeline (`transcribe_run -> curate_run -> _render_and_verify`).
"""

from __future__ import annotations

import asyncio
import html
import math
import random
import re
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import websockets
from curl_cffi import requests as curl_requests
from tenacity import retry, stop_after_attempt, wait_exponential

from src.db import insert_telemetry, upsert_slice, upsert_vod
from src.miner import RUNS_DIR, _ensure_media_path, probe_kick_channel
from src.notifier import send_telegram_message
from src.resource_guard import heavy_local_allowed
from src.runtime_paths import resolve_binary_str
from src.schemas import CandidateSlice, RunState

_CHANNEL_LOGIN_RE = re.compile(r"^[a-z0-9_]{2,32}$")

_REACTION_RE = re.compile(
    r"\b(clip\s*it|lulw?|omegalul|kekw|no\s*way|wtf|bro+|lmao+|lmfao|holy|insane|crazy|cooked|rip|w+|l+|ooo+h?|ahaha+|jaja+)\b",
    re.IGNORECASE,
)
_SUBGIFT_RE = re.compile(
    r"(gifted a tier|is gifting \d+|subscribed at tier|months in a row|raiders from|prime gaming)",
    re.IGNORECASE,
)
def validate_channel_login(channel: str) -> str:
    """Enforce strict `^[a-z0-9_]{2,32}$` channel login (blocks IRC command/CRLF injection)."""
    clean = (channel or "").strip().lower()
    if not _CHANNEL_LOGIN_RE.fullmatch(clean):
        raise ValueError(f"Invalid channel login {channel!r}: must match ^[a-z0-9_]{{2,32}}$")
    return clean


_IRC_PRIVMSG_RE = re.compile(r":([^!\s]+)![^\s]+ PRIVMSG #[^\s]+ :(.+)$")


@dataclass
class HawkesIntensityEngine:
    """Self-exciting exponential Hawkes process lambda(t) = mu + sum(alpha * exp(-beta * dt))."""

    mu: float = 0.25
    alpha: float = 1.0
    beta: float = 0.22
    intensity: float = 0.25
    last_t: float = 0.0

    def update(self, now: float, weight: float = 1.0) -> float:
        if self.last_t <= 0.0:
            self.last_t = now
            self.intensity = self.mu + self.alpha * weight
            return self.intensity
        dt = max(0.0, now - self.last_t)
        decayed = self.mu + (self.intensity - self.mu) * math.exp(-self.beta * dt)
        self.intensity = decayed + self.alpha * weight
        self.last_t = now
        return self.intensity

    def current(self, now: float) -> float:
        if self.last_t <= 0.0:
            return self.mu
        dt = max(0.0, now - self.last_t)
        return self.mu + (self.intensity - self.mu) * math.exp(-self.beta * dt)


@dataclass
class SniperTrigger:
    channel_key: str
    platform: str
    channel: str
    triggered_at: float
    intensity: float
    z_score: float
    reaction_ratio: float
    sample_messages: list[str] = field(default_factory=list)


class ChatWaveDetector:
    """Rolling 60s baseline + 5s burst Z-score + Hawkes intensity detector with anti-subgift filter."""

    def __init__(
        self,
        channel_key: str,
        platform: str,
        channel: str,
        *,
        z_threshold: float = 2.6,
        min_burst_msgs: int = 8,
        min_intensity: float = 5.5,
        cooldown_sec: float = 180.0,
    ) -> None:
        self.channel_key = channel_key
        self.platform = platform
        self.channel = channel
        self.z_threshold = z_threshold
        self.min_burst_msgs = min_burst_msgs
        self.min_intensity = min_intensity
        self.cooldown_sec = cooldown_sec
        self.hawkes = HawkesIntensityEngine()
        self._events: deque[tuple[float, bool, str]] = deque()
        self._bucket_counts: deque[int] = deque(maxlen=12)  # 12 x 5s = 60s baseline
        self._last_bucket_ts: float = 0.0
        self._current_bucket_count: int = 0
        self._last_trigger_ts: float = -1e9
        self.total_messages: int = 0
        self.ignored_subgifts: int = 0

    def ingest(self, text: str, now: float | None = None) -> SniperTrigger | None:
        ts = time.monotonic() if now is None else now
        clean = " ".join(text.strip().split())
        if not clean:
            return None
        if _SUBGIFT_RE.search(clean):
            self.ignored_subgifts += 1
            return None

        self.total_messages += 1
        is_reaction = bool(_REACTION_RE.search(clean))
        weight = 1.65 if is_reaction else 0.75
        intensity = self.hawkes.update(ts, weight=weight)
        self._events.append((ts, is_reaction, clean[:80]))

        # Advance 5-second baseline buckets
        if self._last_bucket_ts <= 0.0:
            self._last_bucket_ts = ts
        while ts - self._last_bucket_ts >= 5.0:
            self._bucket_counts.append(self._current_bucket_count)
            self._current_bucket_count = 0
            self._last_bucket_ts += 5.0
        self._current_bucket_count += 1

        # Trim rolling 5s window
        cutoff = ts - 5.0
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

        burst_n = len(self._events)
        if burst_n < self.min_burst_msgs:
            return None
        if (ts - self._last_trigger_ts) < self.cooldown_sec:
            return None

        if len(self._bucket_counts) >= 3:
            mean_b = statistics.mean(self._bucket_counts)
            std_b = statistics.pstdev(self._bucket_counts) or 1.0
            z_score = (burst_n - mean_b) / max(1.0, std_b)
        else:
            z_score = float(burst_n) / max(1.0, float(self.min_burst_msgs) * 0.4)

        react_count = sum(1 for _, r, _ in self._events if r)
        react_ratio = react_count / max(1, burst_n)

        if z_score >= self.z_threshold and intensity >= self.min_intensity and react_ratio >= 0.25:
            self._last_trigger_ts = ts
            samples = [m for _, _, m in list(self._events)[-6:]]
            return SniperTrigger(
                channel_key=self.channel_key,
                platform=self.platform,
                channel=self.channel,
                triggered_at=ts,
                intensity=round(intensity, 2),
                z_score=round(z_score, 2),
                reaction_ratio=round(react_ratio, 2),
                sample_messages=samples,
            )
        return None


@dataclass
class HLSSegment:
    seq_url: str
    duration: float
    data: bytes
    fetched_at: float


class HLSRingBuffer:
    """Thread-safe memory-bounded circular ring-buffer holding up to `max_seconds` of raw `.ts` segments in RAM."""

    def __init__(self, max_seconds: float = 95.0, max_ram_mb: float = 120.0) -> None:
        self.max_seconds = max_seconds
        self.max_bytes = int(max_ram_mb * 1024 * 1024)
        self._lock = threading.RLock()
        self._segments: deque[HLSSegment] = deque()
        self._seen: deque[str] = deque(maxlen=400)
        self._total_bytes: int = 0
        self._total_duration: float = 0.0

    def has_seen(self, seq_url: str) -> bool:
        with self._lock:
            return seq_url in self._seen

    def append(self, seq_url: str, duration: float, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            if seq_url in self._seen:
                return
            seg = HLSSegment(seq_url=seq_url, duration=max(0.5, duration), data=data, fetched_at=time.monotonic())
            self._segments.append(seg)
            self._seen.append(seq_url)
            self._total_bytes += len(data)
            self._total_duration += seg.duration
            while (self._total_duration > self.max_seconds or self._total_bytes > self.max_bytes) and len(self._segments) > 2:
                old = self._segments.popleft()
                self._total_bytes -= len(old.data)
                self._total_duration -= old.duration

    @property
    def ram_mb(self) -> float:
        with self._lock:
            return round(self._total_bytes / (1024 * 1024), 2)

    @property
    def duration_sec(self) -> float:
        with self._lock:
            return round(self._total_duration, 2)

    def snapshot_bytes(self) -> tuple[bytes, float]:
        """Return concatenated MPEG-TS payload + duration for safe concurrent async flush."""
        with self._lock:
            if not self._segments:
                return b"", 0.0
            payload = b"".join(s.data for s in self._segments)
            return payload, round(self._total_duration, 2)

    def flush_to_mp4(self, out_mp4: Path) -> tuple[bool, float]:
        """Remux/normalize the buffered MPEG-TS ring-buffer into a clean CFR MP4 slice."""
        payload, dur = self.snapshot_bytes()
        if not payload or dur < 5.0:
            return False, 0.0
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="hls_flush_") as tmp:
            ts_path = Path(tmp) / "ring.ts"
            ts_path.write_bytes(payload)
            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(ts_path),
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                "-movflags",
                "+faststart",
                str(out_mp4),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=90)
            if res.returncode == 0 and out_mp4.exists() and out_mp4.stat().st_size > 10_000:
                return True, dur
        return False, 0.0


def _select_best_hls_variant(master_url: str, playlist_text: str) -> str:
    """Pick the best <=1080p HLS media playlist URL from a master M3U8."""
    lines = [ln.strip() for ln in playlist_text.splitlines() if ln.strip()]
    if "#EXT-X-STREAM-INF" not in playlist_text:
        return master_url
    variants: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines) and not lines[i + 1].startswith("#"):
            m_res = re.search(r"RESOLUTION=(\d+)x(\d+)", ln)
            h = int(m_res.group(2)) if m_res else 720
            if h <= 1080:
                variants.append((h, urljoin(master_url, lines[i + 1])))
    if not variants:
        return master_url
    variants.sort(key=lambda x: x[0], reverse=True)
    return variants[0][1]


def resolve_live_hls_url(platform: str, channel: str) -> tuple[str | None, int | None]:
    """Resolve live HLS playback URL (and Kick channel_id if applicable) for a channel."""
    if platform == "kick":
        probe = probe_kick_channel(channel)
        if not probe.get("ok"):
            return None, None
        cid = int(probe.get("id") or 0) or None
        if not probe.get("is_live"):
            return None, cid
        playback = str(probe.get("playback_url") or "").strip()
        return (playback or None), cid

    if platform == "twitch":
        _ensure_media_path()
        url = f"https://www.twitch.tv/{channel}"
        sl = shutil.which("streamlink")
        if sl:
            res = subprocess.run(
                [sl, "--stream-url", "--twitch-disable-ads", url, "1080p60,1080p,best"],
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            if res.returncode == 0 and res.stdout.strip().startswith("http"):
                return res.stdout.strip().splitlines()[0], None
        ytdlp = resolve_binary_str("yt-dlp")
        res = subprocess.run(
            [ytdlp, "--no-warnings", "-f", "best[height<=1080]/best", "-g", "--", url],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        if res.returncode == 0 and res.stdout.strip().startswith("http"):
            return res.stdout.strip().splitlines()[0], None
    return None, None


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.4, min=0.4, max=3.0),
)
def _http_get_chrome(url: str, timeout: int = 10) -> Any:
    eff_timeout = min(20, max(2, int(timeout)))
    return curl_requests.get(url, impersonate="chrome", timeout=eff_timeout)


async def _poll_hls_ring_buffer(
    master_url: str,
    ring: HLSRingBuffer,
    stop_event: asyncio.Event,
    *,
    platform: str = "",
    channel: str = "",
) -> None:
    """Background asyncio task polling live HLS media segments into `HLSRingBuffer` with token auto-refresh."""
    media_url = master_url
    consecutive_expired = 0
    try:
        r0 = await asyncio.to_thread(_http_get_chrome, master_url, 10)
        if r0.status_code == 200:
            media_url = _select_best_hls_variant(master_url, r0.text)
    except Exception:
        pass

    while not stop_event.is_set():
        try:
            resp = await asyncio.to_thread(_http_get_chrome, media_url, 10)
            if resp.status_code == 200:
                consecutive_expired = 0
                lines = [ln.strip() for ln in resp.text.splitlines() if ln.strip()]
                seg_dur = 2.0
                for ln in lines:
                    if ln.startswith("#EXTINF:"):
                        try:
                            seg_dur = float(ln.split(":", 1)[1].split(",", 1)[0])
                        except ValueError:
                            seg_dur = 2.0
                    elif not ln.startswith("#"):
                        seg_url = urljoin(media_url, ln)
                        if not ring.has_seen(seg_url):
                            sr = await asyncio.to_thread(_http_get_chrome, seg_url, 10)
                            if sr.status_code == 200 and sr.content:
                                ring.append(seg_url, seg_dur, sr.content)
                            elif sr.status_code in {403, 404, 410}:
                                consecutive_expired += 1
            elif resp.status_code in {403, 404, 410}:
                consecutive_expired += 1

            if consecutive_expired >= 3 and platform and channel:
                consecutive_expired = 0
                refreshed_master, _ = await asyncio.to_thread(resolve_live_hls_url, platform, channel)
                if refreshed_master:
                    media_url = refreshed_master
                    r_new = await asyncio.to_thread(_http_get_chrome, refreshed_master, 10)
                    if r_new.status_code == 200:
                        media_url = _select_best_hls_variant(refreshed_master, r_new.text)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=2.0)
        except TimeoutError:
            continue


async def _listen_twitch_irc(
    channel: str,
    detector: ChatWaveDetector,
    on_trigger: asyncio.Queue[SniperTrigger],
    stop_event: asyncio.Event,
) -> None:
    """Connect anonymously (`justinfan`) to Twitch IRC WebSocket and feed `ChatWaveDetector`."""
    safe_channel = validate_channel_login(channel)
    nick = f"justinfan{random.randint(10000, 99999)}"
    uri = "wss://irc-ws.chat.twitch.tv:443"
    while not stop_event.is_set():
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10, open_timeout=12) as ws:
                await ws.send("PASS SCHMOOPIIE")
                await ws.send(f"NICK {nick}")
                await ws.send(f"JOIN #{safe_channel}")
                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except TimeoutError:
                        continue
                    for line in str(raw).splitlines():
                        if line.startswith("PING"):
                            await ws.send(line.replace("PING", "PONG", 1))
                            continue
                        m = _IRC_PRIVMSG_RE.search(line)
                        if m:
                            trig = detector.ingest(m.group(2))
                            if trig is not None:
                                await on_trigger.put(trig)
        except Exception:
            if stop_event.is_set():
                break
            await asyncio.sleep(2.0)


async def _listen_kick_chat_rest(
    channel: str,
    channel_id: int | None,
    detector: ChatWaveDetector,
    on_trigger: asyncio.Queue[SniperTrigger],
    stop_event: asyncio.Event,
) -> None:
    """Poll Kick live chat REST v2 (`curl_cffi` impersonate chrome) as resilient Centrifugo fallback."""
    cid = channel_id
    if cid is None:
        probe = await asyncio.to_thread(probe_kick_channel, channel)
        cid = int(probe.get("id") or 0) or None
    if cid is None:
        return

    seen_ids: deque[str] = deque(maxlen=400)
    url = f"https://kick.com/api/v2/channels/{cid}/messages"
    primed = False
    while not stop_event.is_set():
        try:
            resp = await asyncio.to_thread(_http_get_chrome, url, 10)
            if resp.status_code == 200:
                msgs = ((resp.json() or {}).get("data") or {}).get("messages") or []
                for msg in reversed(msgs):
                    mid = str(msg.get("id") or "")
                    if not mid or mid in seen_ids:
                        continue
                    seen_ids.append(mid)
                    if not primed:
                        continue
                    content = str(msg.get("content") or "")
                    trig = detector.ingest(content)
                    if trig is not None:
                        await on_trigger.put(trig)
                primed = True
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=2.5)
        except TimeoutError:
            continue


def _materialize_sniper_run(
    trig: SniperTrigger,
    ring: HLSRingBuffer,
    auto_render: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Flush ring-buffer to runs/live_<channel>_<ts>/slice_01.mp4 and optionally run full pipeline."""
    ts_tag = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    run_id = f"live_{trig.channel}_{ts_tag}"
    run_dir = RUNS_DIR / run_id
    source_url = (
        f"https://www.twitch.tv/{trig.channel}"
        if trig.platform == "twitch"
        else f"https://kick.com/{trig.channel}"
    )

    if dry_run:
        return {
            "run_id": run_id,
            "channel": trig.channel_key,
            "dry_run": True,
            "intensity": trig.intensity,
            "z_score": trig.z_score,
            "ring_sec": ring.duration_sec,
            "ring_ram_mb": ring.ram_mb,
        }

    out_slice = run_dir / "slice_01.mp4"
    ok, dur = ring.flush_to_mp4(out_slice)
    if not ok:
        return {"run_id": run_id, "channel": trig.channel_key, "flushed": False, "ring_sec": ring.duration_sec}

    sig = "twitch_chat_zscore" if trig.platform == "twitch" else "kick_hls_window"
    cand = CandidateSlice(
        slice_id="slice_01",
        source_url=source_url,
        platform="twitch" if trig.platform == "twitch" else "kick",
        start_sec=0.0,
        end_sec=dur,
        duration_sec=dur,
        signal_source=sig,  # type: ignore[arg-type]
        score=round(trig.z_score, 2),
        slice_video_path=str(out_slice),
        source_width=1920,
        source_height=1080,
        source_fps=60.0,
    )
    state = RunState(
        run_id=run_id,
        source_url=source_url,
        streamer_id=trig.channel,
        stage="mined",
        candidates=[cand],
        updated_at=datetime.now(UTC).isoformat(),
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(state.model_dump_json(indent=2), encoding="utf-8")
    upsert_vod(source_url, cand.platform, trig.channel, title=f"LIVE {trig.channel}", duration_sec=dur, run_id=run_id)
    upsert_slice(run_id, "slice_01", source_url, cand.platform, 0.0, dur, sig, cand.score, str(out_slice))

    result: dict[str, Any] = {"run_id": run_id, "flushed": True, "slice_mp4": str(out_slice), "duration_sec": dur}
    if auto_render:
        allowed, reason, _ = heavy_local_allowed()
        if not allowed:
            result["auto_render"] = f"skipped:{reason}"
            return result
        from src.cli import _render_and_verify
        from src.curator import curate_run
        from src.transcriber import transcribe_run

        transcribe_run(run_dir)
        state = curate_run(run_dir)
        if state.edit_plans:
            state = _render_and_verify(run_dir, state)
        result["rendered_clips"] = state.rendered_clips
        if state.rendered_clips:
            safe_key = html.escape(trig.channel_key, quote=True)
            safe_rid = html.escape(run_id, quote=True)
            send_telegram_message(
                f"<b>Live Sniper Captured</b> {safe_key}\nrun_id=<code>{safe_rid}</code> clips={len(state.rendered_clips)}"
            )
    return result


async def run_live_sniper(
    channels: list[str],
    *,
    duration_sec: float = 120.0,
    post_roll_sec: float = 20.0,
    enable_hls: bool = True,
    auto_render: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Monitor 1..2 live channels (`twitch:<login>`, `kick:<slug>`) for `duration_sec` seconds."""
    parsed: list[tuple[str, str]] = []
    for raw in channels[:2]:
        plat, _, ch = raw.partition(":")
        if not ch:
            plat, ch = "twitch", plat
        safe_ch = validate_channel_login(ch)
        parsed.append((plat.strip().lower(), safe_ch))

    stop_event = asyncio.Event()
    trigger_q: asyncio.Queue[SniperTrigger] = asyncio.Queue()
    detectors: dict[str, ChatWaveDetector] = {}
    rings: dict[str, HLSRingBuffer] = {}
    tasks: list[asyncio.Task[Any]] = []
    hls_status: dict[str, str] = {}

    for plat, ch in parsed:
        key = f"{plat}:{ch}"
        det = ChatWaveDetector(key, plat, ch)
        ring = HLSRingBuffer(max_seconds=95.0, max_ram_mb=120.0)
        detectors[key] = det
        rings[key] = ring

        cid: int | None = None
        if enable_hls:
            hls_url, cid = await asyncio.to_thread(resolve_live_hls_url, plat, ch)
            if hls_url:
                hls_status[key] = "streaming_hls"
                tasks.append(asyncio.create_task(_poll_hls_ring_buffer(hls_url, ring, stop_event, platform=plat, channel=ch)))
            else:
                hls_status[key] = "offline_or_no_hls"
        else:
            hls_status[key] = "chat_only"

        if plat == "twitch":
            tasks.append(asyncio.create_task(_listen_twitch_irc(ch, det, trigger_q, stop_event)))
        elif plat == "kick":
            tasks.append(asyncio.create_task(_listen_kick_chat_rest(ch, cid, det, trigger_q, stop_event)))

    captures: list[dict[str, Any]] = []
    started = time.monotonic()

    async def _consumer() -> None:
        while not stop_event.is_set():
            try:
                trig = await asyncio.wait_for(trigger_q.get(), timeout=1.0)
            except TimeoutError:
                continue
            print(
                f"[SNIPER_SPIKE] {trig.channel_key} z={trig.z_score} I={trig.intensity} "
                f"react={trig.reaction_ratio} samples={trig.sample_messages[:3]}"
            )
            if not dry_run and post_roll_sec > 0:
                await asyncio.sleep(min(post_roll_sec, max(1.0, duration_sec - (time.monotonic() - started))))
            cap = await asyncio.to_thread(
                _materialize_sniper_run,
                trig,
                rings[trig.channel_key],
                auto_render,
                dry_run,
            )
            captures.append(cap)
            insert_telemetry("live_sniper", "sniper_trigger", cap)

    consumer_task = asyncio.create_task(_consumer())
    try:
        await asyncio.sleep(max(1.0, duration_sec))
    finally:
        stop_event.set()
        for t in tasks:
            t.cancel()
        consumer_task.cancel()
        await asyncio.gather(*tasks, consumer_task, return_exceptions=True)

    summary = {
        "duration_sec": round(time.monotonic() - started, 2),
        "channels": {
            k: {
                "hls_status": hls_status.get(k, "unknown"),
                "messages_seen": det.total_messages,
                "ignored_subgifts": det.ignored_subgifts,
                "ring_sec": rings[k].duration_sec,
                "ring_ram_mb": rings[k].ram_mb,
            }
            for k, det in detectors.items()
        },
        "captures": captures,
    }
    insert_telemetry("live_sniper", "sniper_session_summary", summary)
    return summary
