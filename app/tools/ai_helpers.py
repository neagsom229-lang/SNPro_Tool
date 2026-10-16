"""
app/tools/ai_helpers.py

Shared helpers for the "modern AI" upgrades to app/tools/tasks.py.

These wrap third-party AI services (Whisper, Replicate, ElevenLabs) behind
small functions with consistent signatures and graceful fallbacks, so the
Celery tasks stay readable and every external call can fail without
crashing a job.

New dependencies (add to requirements.txt):
    openai-whisper            # or faster-whisper for CPU speed
    replicate                 # Replicate API client
    ffmpeg-python             # thin wrapper around the ffmpeg binary

New environment variables:
    REPLICATE_API_TOKEN       # enables Replicate-backed features:
                              #   - background removal (fallback for rembg)
                              #   - Real-ESRGAN video/image upscaling
                              #   - Stable Video Diffusion image animation
    WHISPER_MODEL             # tiny|base|small|medium|large (default: "small")
    ELEVENLABS_API_KEY        # already referenced in your existing code
    DEEPL_API_KEY             # preferred translation engine
"""

import os
import traceback

# ---------------------------------------------------------------------------
# Whisper (speech-to-text)
# ---------------------------------------------------------------------------

_WHISPER_MODEL_CACHE = {}

# Duration breakpoints (seconds) for automatic model-size selection when the
# caller doesn't force a specific size. Tuned for a reasonable
# speed/accuracy trade-off on CPU workers: short clips get a bigger model
# since the absolute time cost is still small, long clips get a smaller
# model so a 20-minute upload doesn't take an hour to transcribe.
_AUTO_MODEL_BREAKPOINTS = (
    (120, "small"),    # up to 2 min -> small (good accuracy, still fast)
    (600, "base"),     # up to 10 min -> base
    (float("inf"), "tiny"),  # anything longer -> tiny, prioritize turnaround
)


def recommend_whisper_model(duration_seconds):
    """
    Pick a Whisper model size automatically based on audio duration, so
    "AI Magic" / smart-default flows don't have to hardcode one size for
    every input. Pure function - no I/O, easy to unit test and to call
    from a route to preview the choice before a job even starts.
    """
    if duration_seconds is None or duration_seconds <= 0:
        return os.environ.get("WHISPER_MODEL", "small")
    for max_seconds, model_name in _AUTO_MODEL_BREAKPOINTS:
        if duration_seconds <= max_seconds:
            return model_name
    return "tiny"


def _get_whisper_model(model_name=None):
    """Lazily load (and cache) a Whisper model. Loading is slow, so we keep
    one instance per worker process, keyed by size so mixing auto-selected
    sizes across jobs doesn't reload the model every time."""
    model_name = model_name or os.environ.get("WHISPER_MODEL", "small")
    if model_name not in _WHISPER_MODEL_CACHE:
        import whisper  # openai-whisper
        _WHISPER_MODEL_CACHE[model_name] = whisper.load_model(model_name)
    return _WHISPER_MODEL_CACHE[model_name]


def _compute_transcript_confidence(segments):
    """
    Derive a single 0-1 confidence score from Whisper's per-segment
    metadata, so callers can flag "this transcript may be unreliable"
    instead of silently trusting garbage output (e.g. music, heavy
    background noise, or a language Whisper mis-detected).

    Whisper segments carry `avg_logprob` (higher/less-negative = more
    confident) and `no_speech_prob` (probability the segment is actually
    silence/non-speech). We fold both into one score, weighted by each
    segment's duration so a handful of short garbled words don't tank the
    score for an otherwise-clean long transcript.
    """
    if not segments:
        return 0.0

    total_weight = 0.0
    weighted_score = 0.0
    for seg in segments:
        duration = max(0.01, seg.get("end", 0) - seg.get("start", 0))
        # avg_logprob is typically in roughly [-1.0, 0.0]; clamp and rescale
        # to [0, 1] rather than assuming an exact range Whisper doesn't
        # actually guarantee.
        logprob = seg.get("avg_logprob", -0.5)
        logprob_score = max(0.0, min(1.0, 1.0 + logprob))
        no_speech = seg.get("no_speech_prob", 0.0)
        segment_score = logprob_score * (1.0 - no_speech)

        weighted_score += segment_score * duration
        total_weight += duration

    return round(weighted_score / total_weight, 3) if total_weight else 0.0


def transcribe_with_whisper(audio_path, language=None, model_name=None,
                             duration_hint=None, progress_cb=None):
    """
    Transcribe an audio file with Whisper.

    Args:
        audio_path: path to a wav/mp3/mp4/etc. Whisper uses ffmpeg internally
                    so most common formats work without pre-conversion.
        language:   ISO 639-1 code, or None to auto-detect.
        model_name: force a specific Whisper model size. If None, and
                    duration_hint is given, the size is chosen automatically
                    via recommend_whisper_model() - this is the "AI Magic"
                    smart-default path. If both are None, falls back to
                    WHISPER_MODEL / "small" as before.
        duration_hint: audio duration in seconds, used only for automatic
                    model selection above - has no effect if model_name is set.
        progress_cb: optional callable(str) for status messages.

    Returns:
        dict(text=str, detected_lang=str, segments=list, confidence=float)
        `confidence` is a 0-1 score - see _compute_transcript_confidence.

    Raises:
        Whatever whisper/ffmpeg raises - caller is expected to catch and
        fall back to Google Speech Recognition.
    """
    resolved_model = model_name or recommend_whisper_model(duration_hint)

    if progress_cb:
        progress_cb(f"Loading Whisper model ({resolved_model})...")
    model = _get_whisper_model(resolved_model)

    if progress_cb:
        progress_cb("Transcribing with Whisper...")

    # language=None triggers Whisper's built-in language auto-detection.
    result = model.transcribe(
        audio_path,
        language=language if language else None,
        task="transcribe",
        fp16=False,  # safe default for CPU-only workers; set True if on GPU
    )

    segments = result.get("segments", [])
    return {
        "text": (result.get("text") or "").strip(),
        "detected_lang": result.get("language", language or "en"),
        "segments": segments,
        "confidence": _compute_transcript_confidence(segments),
        "model_used": resolved_model,
    }


def whisper_segments_to_srt(segments, out_path):
    """Write Whisper's segment list out as an .srt subtitle file."""
    def _fmt(t):
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int((t - int(t)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(out_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, start=1):
            f.write(f"{i}\n")
            f.write(f"{_fmt(seg['start'])} --> {_fmt(seg['end'])}\n")
            f.write(f"{seg['text'].strip()}\n\n")
    return out_path


# ---------------------------------------------------------------------------
# Replicate (Real-ESRGAN upscaling, Stable Video Diffusion, bg removal)
# ---------------------------------------------------------------------------

def _retry_with_backoff(fn, *args, retries=2, base_delay=1.5, retryable=(Exception,), **kwargs):
    """
    Call fn(*args, **kwargs), retrying on transient failures with
    exponential backoff (base_delay, base_delay*2, base_delay*4, ...).
    Used for the Replicate network calls below, which otherwise fail an
    entire AI-upgrade path (upscale/interpolate/animate) on a single
    dropped connection or momentary rate limit - the kind of blip that a
    two-attempt retry resolves the vast majority of the time in practice.
    Re-raises the last exception if every attempt fails, so callers
    (typically wrapped in safe_call) still see a real failure to log/fall
    back on rather than a silently-swallowed one.
    """
    import time
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except retryable as e:
            last_exc = e
            if attempt < retries:
                time.sleep(base_delay * (2 ** attempt))
    raise last_exc


def _replicate_enabled():
    return bool(os.environ.get("REPLICATE_API_TOKEN"))


def _replicate_client():
    import replicate
    return replicate.Client(api_token=os.environ["REPLICATE_API_TOKEN"])


# Resolving a model to its latest version costs one extra API call, so we
# cache the resolved "owner/model:version" string per worker process for a
# short time. This avoids hardcoding version hashes, which go stale the
# moment the model owner pushes an update and then fail with a 404/422 that
# looks like an unrelated bug.
_MODEL_VERSION_CACHE = {}
_MODEL_VERSION_TTL = 60 * 30  # 30 minutes


def _resolve_latest_version(model_slug):
    """Return "owner/model:version" pinned to the model's current latest
    version, resolved fresh (subject to the cache above) rather than
    hardcoded. Raises if the model can't be found - callers should let
    safe_call() turn that into a graceful fallback."""
    import time
    now = time.time()
    cached = _MODEL_VERSION_CACHE.get(model_slug)
    if cached and (now - cached[1]) < _MODEL_VERSION_TTL:
        return cached[0]

    client = _replicate_client()
    model = client.models.get(model_slug)
    latest = model.latest_version
    if latest is None:
        raise RuntimeError(f"Replicate model '{model_slug}' has no published version.")
    pinned = f"{model_slug}:{latest.id}"
    _MODEL_VERSION_CACHE[model_slug] = (pinned, now)
    return pinned


def _replicate_run_and_download(model_slug, file_path, file_field, extra_input,
                                 out_path=None, timeout=120):
    """
    Shared implementation for every replicate_* helper below: resolve the
    model's latest version, run it (retried), download the result
    (retried), and either return the raw bytes (out_path=None) or write
    them to out_path and return that path.

    file_path/file_field: the local file to upload and the input key
        Replicate expects it under (e.g. "image", "video", "input_image").
        The file is re-opened fresh on every retry attempt rather than
        reusing one handle across attempts - reusing a handle would risk
        silently resending a partially-consumed (truncated) upload if the
        first attempt failed partway through, since the handle's read
        position doesn't reset itself.
    extra_input: dict of any additional input fields besides the file.
    """
    import requests

    client = _replicate_client()
    version = _resolve_latest_version(model_slug)

    def _run_attempt():
        with open(file_path, "rb") as f:
            return client.run(version, input={file_field: f, **extra_input})

    output = _retry_with_backoff(_run_attempt, retries=2, base_delay=1.5)

    def _download():
        resp = requests.get(output, timeout=timeout)
        resp.raise_for_status()
        return resp.content

    content = _retry_with_backoff(_download, retries=2, base_delay=1.5)

    if out_path is None:
        return content
    with open(out_path, "wb") as out:
        out.write(content)
    return out_path


def replicate_remove_background(image_path):
    """Higher-quality/faster background removal via Replicate.
    Returns bytes of a PNG with alpha, or raises on failure."""
    return _replicate_run_and_download(
        "lucataco/remove-bg", image_path, "image", {}, out_path=None, timeout=60,
    )


def replicate_restore_image(image_path, out_path, scale=2, face_enhance=True):
    """Real-ESRGAN still-image upscale/restore via Replicate - good for
    old/damaged photos: cleans noise and sharpens detail while upscaling.
    Downloads the result to out_path (PNG or JPG matching out_path's ext)."""
    return _replicate_run_and_download(
        "nightmareai/real-esrgan", image_path, "image",
        {"scale": scale, "face_enhance": face_enhance},
        out_path=out_path, timeout=120,
    )


def replicate_upscale_video(video_path, out_path, scale=2):
    """Real-ESRGAN video super-resolution via Replicate.
    Downloads the processed file to out_path."""
    return _replicate_run_and_download(
        "lucataco/real-esrgan-video", video_path, "video", {"scale": scale},
        out_path=out_path, timeout=600,
    )


def replicate_interpolate_frames(video_path, out_path, target_fps=60):
    """RIFE frame interpolation via Replicate - smooths motion and raises
    frame rate. Downloads the processed file to out_path."""
    return _replicate_run_and_download(
        "pollinations/rife", video_path, "video", {"fps": target_fps},
        out_path=out_path, timeout=600,
    )


def replicate_animate_image(image_path, out_path, motion_bucket_id=127, fps=6):
    """Stable Video Diffusion (image -> short clip) via Replicate.
    Downloads the resulting mp4 to out_path."""
    return _replicate_run_and_download(
        "stability-ai/stable-video-diffusion", image_path, "input_image",
        {"motion_bucket_id": motion_bucket_id, "fps": fps, "cond_aug": 0.02},
        out_path=out_path, timeout=300,
    )


def safe_call(fn, *args, on_error_return=None, log_prefix="", **kwargs):
    """Run fn(*args, **kwargs); on any exception, log and return on_error_return
    instead of propagating. Used for optional AI-upgrade paths that must not
    take down a whole job if an external API is flaky."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"{log_prefix}{fn.__name__} failed, falling back: {e}")
        traceback.print_exc()
        return on_error_return