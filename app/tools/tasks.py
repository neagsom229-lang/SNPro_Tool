import os
import re
import shutil
import zipfile
import traceback
import json
import subprocess
import uuid
from celery import shared_task
import celery
from werkzeug.utils import secure_filename

from extensions import db

from models import Job
from flask import current_app
from config import user_dir

# ----- Helper functions (defined once) -----
def _set(job_id, **fields):
    job = Job.query.get(job_id)
    if not job:
        return None
    for k, v in fields.items():
        setattr(job, k, v)
    db.session.commit()
    return job

def _rel(path):
    root = current_app.config["STORAGE_ROOT"]
    return os.path.relpath(path, root)

def _sorted_alnum(items):
    convert = lambda t: int(t) if t.isdigit() else t.lower()
    key = lambda s: [convert(c) for c in re.split(r"([0-9]+)", s)]
    return sorted(items, key=key)

def _friendly_error(prefix, exc):
    traceback.print_exc()
    msg = str(exc).strip() or exc.__class__.__name__
    if len(msg) > 300:
        msg = msg[:300] + "..."
    return f"{prefix}: {msg}"

# ----- Import helpers from other modules -----
from app.tools.speech_helpers import (
    transcribe_audio, translate_text, synthesize_speech,
    synthesize_with_elevenlabs, _synthesize_with_gtts, SUPPORTED_LANGUAGES,
)
from app.tools.ai_helpers import (
    transcribe_with_whisper, whisper_segments_to_srt,
    replicate_remove_background, replicate_restore_image,
    replicate_upscale_video, replicate_interpolate_frames,
    replicate_animate_image, safe_call,
)
from app.tools import editing_helpers as eh
from app.tools.pdf_helpers import combine_files_to_pdf
from app.tools.video_helpers import extract_audio_from_video, replace_audio_in_video

# ----- All Celery tasks follow (no routes!) -----
# e.g. task_download_video, task_speech_to_text, etc.


# ---------------------------------------------------------------------------
# 1. Video / audio downloader (yt-dlp) - smarter retry, playlist support
# ---------------------------------------------------------------------------
@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def task_download_video(self, job_id, url, mode, out_dir, download_playlist=False):
    import yt_dlp
    import time
    from yt_dlp.utils import DownloadError

    _set(job_id, status="running", progress=1, message="Starting download...")

    last_update_time = [0.0]
    last_pct = [-1]

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes") or 0
            pct = int(downloaded / total * 100) if total else 0
            now = time.time()
            if pct != last_pct[0] and (now - last_update_time[0] >= 1.0 or pct >= last_pct[0] + 2 or pct == 99):
                last_update_time[0] = now
                last_pct[0] = pct
                _set(job_id, progress=max(1, min(99, pct)), message=f"Downloading... {pct}%")
        elif d.get("status") == "finished":
            _set(job_id, progress=99, message="Processing / merging...")

    os.makedirs(out_dir, exist_ok=True)

    if "ted.com/talks/" in url:
        url = url.split("?")[0]

    base_opts = {
        "outtmpl": os.path.join(out_dir, "%(playlist_index|)s%(playlist_index& - |)s%(title)s.%(ext)s"),
        "progress_hooks": [hook],
        "windowsfilenames": True,
        "quiet": False,
        "no_warnings": False,
        "ignoreerrors": "only_download",  # skip bad items in a playlist rather than aborting
        "noplaylist": not download_playlist,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "http_headers": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-us,en;q=0.5",
            "Sec-Fetch-Mode": "navigate",
            "Origin": "https://www.ted.com",
            "Referer": "https://www.ted.com/",
        },
        "extractor_retries": 3,
        "retries": 5,
        "fragment_retries": 5,
    }

    cookie_file = os.environ.get("YDL_COOKIE_FILE")
    if cookie_file and os.path.isfile(cookie_file):
        base_opts["cookiefile"] = cookie_file

    if mode == "mp3":
        base_opts.update({
            "format": "bestaudio/best",
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ],
        })
    else:
        base_opts.update({
            "format": "bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
        })

    # ---- Intelligent retry: try a few extractor strategies in order ----
    attempts = [
        {},  # 1. plain defaults
        {"extractor_args": {"ted": {"format": "best", "prefer_ffmpeg": True, "use_webpage": True}}},  # 2. TED-specific
        {"force_generic_extractor": True},  # 3. last resort: generic extractor
    ]

    last_error = None
    for i, extra_opts in enumerate(attempts, start=1):
        opts = {**base_opts, **extra_opts}
        try:
            if i > 1:
                _set(job_id, progress=5, message=f"Retrying with strategy {i}/{len(attempts)}...")
            with yt_dlp.YoutubeDL(opts) as ydl:
                result = ydl.download([url])
                if result != 0:
                    raise RuntimeError(f"yt-dlp returned exit code {result}")

            files = [f for f in os.listdir(out_dir) if not f.startswith(".")]
            if not files:
                raise RuntimeError("No file was produced by the downloader.")

            if download_playlist and len(files) > 1:
                zip_path = os.path.join(out_dir, "playlist.zip")
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for fname in files:
                        zf.write(os.path.join(out_dir, fname), arcname=fname)
                _set(job_id, status="success", progress=100,
                     message=f"Done - {len(files)} files zipped", result_path=_rel(zip_path))
                return

            result_file = os.path.join(out_dir, files[0])
            _set(job_id, status="success", progress=100, message="Done", result_path=_rel(result_file))
            return

        except DownloadError as e:
            last_error = e
            continue
        except Exception as e:
            last_error = e
            continue

    # ---- Final fallback: legacy youtube-dl ----
    try:
        _set(job_id, progress=50, message="All yt-dlp strategies failed. Trying youtube-dl fallback...")
        import youtube_dl
        legacy_opts = {
            "outtmpl": os.path.join(out_dir, "%(title)s.%(ext)s"),
            "progress_hooks": [hook],
            "quiet": False,
        }
        if "cookiefile" in base_opts:
            legacy_opts["cookiefile"] = base_opts["cookiefile"]
        with youtube_dl.YoutubeDL(legacy_opts) as ydl_legacy:
            result = ydl_legacy.download([url])
            if result != 0:
                raise RuntimeError(f"youtube-dl returned exit code {result}")
        files = [f for f in os.listdir(out_dir) if not f.startswith(".")]
        if not files:
            raise RuntimeError("No file produced by youtube-dl.")
        result_file = os.path.join(out_dir, files[0])
        _set(job_id, status="success", progress=100, message="Done (via youtube-dl)", result_path=_rel(result_file))
        return
    except ImportError:
        error_msg = (
            "All download strategies failed and youtube-dl is not installed as a fallback. "
            "Try: pip install youtube-dl, or update yt-dlp."
        )
    except Exception as e_legacy:
        error_msg = f"All strategies failed. Last yt-dlp error: {last_error}. youtube-dl error: {e_legacy}"

    _set(job_id, status="failure", message=_friendly_error("Download failed", RuntimeError(error_msg)))


# ---------------------------------------------------------------------------
# 2. Speech to text - now Whisper-first, Google fallback, auto language ID
# ---------------------------------------------------------------------------

@shared_task(bind=True)
def task_speech_to_text(self, job_id, input_path, language, out_dir):
    """
    language: ISO 639-1 code (e.g. "en", "km", "fr") or None/"auto" to
    let Whisper auto-detect. Falls back to Google Speech Recognition
    (60s/short-form, limited language set) only if Whisper is unavailable
    or errors out.
    """
    from pydub import AudioSegment

    _set(job_id, status="running", progress=10, message="Preparing audio...")

    lang_code = None if not language or language.lower() in ("auto", "detect") else language

    try:
        work_path = input_path
        if not work_path.lower().endswith((".wav", ".mp3", ".m4a", ".flac")):
            audio = AudioSegment.from_file(work_path)
            work_path = os.path.join(out_dir, "converted.wav")
            audio.export(work_path, format="wav")

        # ---- Primary: Whisper ----
        try:
            def _progress(msg):
                _set(job_id, progress=40, message=msg)

            result = transcribe_with_whisper(work_path, language=lang_code, progress_cb=_progress)
            final_text = result["text"]
            if not final_text:
                raise ValueError("Whisper returned empty transcript.")

            out_file = os.path.join(out_dir, "transcript.txt")
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(final_text)

            srt_file = None
            if result.get("segments"):
                srt_file = whisper_segments_to_srt(result["segments"], os.path.join(out_dir, "transcript.srt"))

            _set(
                job_id, status="success", progress=100,
                message=f"Done (Whisper, detected language: {result['detected_lang']})",
                result_path=_rel(out_file),
            )
            return

        except Exception as whisper_error:
            _set(job_id, progress=35, message=f"Whisper unavailable ({whisper_error}). Falling back to Google...")

        # ---- Fallback: Google Speech Recognition (legacy path) ----
        import speech_recognition as sr
        import math

        audio = AudioSegment.from_wav(work_path) if work_path.lower().endswith(".wav") else AudioSegment.from_file(work_path)
        if not work_path.lower().endswith(".wav"):
            work_path = os.path.join(out_dir, "converted_fallback.wav")
            audio.export(work_path, format="wav")
        duration_seconds = len(audio) / 1000.0

        google_lang = lang_code
        supported_languages = {
            "en-US", "en-GB", "es-ES", "fr-FR", "de-DE", "it-IT",
            "ja-JP", "ko-KR", "pt-PT", "ru-RU", "zh-CN", "zh-TW",
            "nl-NL", "pl-PL", "tr-TR",
        }
        if not google_lang or google_lang not in supported_languages:
            google_lang = "en-US"

        recognizer = sr.Recognizer()

        if duration_seconds <= 60:
            _set(job_id, progress=50, message="Transcribing (Google fallback)...")
            with sr.AudioFile(work_path) as source:
                audio_data = recognizer.record(source)
            try:
                text = recognizer.recognize_google(audio_data, language=google_lang)
            except sr.UnknownValueError:
                _set(job_id, status="failure", message="Could not understand the audio.")
                return
            except sr.RequestError as e:
                _set(job_id, status="failure", message=_friendly_error("Recognition service error", e))
                return
            out_file = os.path.join(out_dir, "transcript.txt")
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(text)
            _set(job_id, status="success", progress=100, message="Done (Google fallback)", result_path=_rel(out_file))
            return

        chunk_length_ms = 55 * 1000
        overlap_ms = 5 * 1000
        step_ms = chunk_length_ms - overlap_ms
        total_chunks = max(1, math.ceil((len(audio) - chunk_length_ms) / step_ms) + 1)

        full_transcript = []
        chunk_dir = os.path.join(out_dir, "chunks")
        os.makedirs(chunk_dir, exist_ok=True)

        for i in range(total_chunks):
            start_ms = i * step_ms
            end_ms = min(start_ms + chunk_length_ms, len(audio))
            chunk = audio[start_ms:end_ms]
            chunk_path = os.path.join(chunk_dir, f"chunk_{i+1:03d}.wav")
            chunk.export(chunk_path, format="wav")

            pct = 50 + int((i + 1) / total_chunks * 45)
            _set(job_id, progress=pct, message=f"Transcribing chunk {i+1}/{total_chunks} (Google fallback)...")

            with sr.AudioFile(chunk_path) as source:
                audio_data = recognizer.record(source)
            try:
                text = recognizer.recognize_google(audio_data, language=google_lang)
                if text.strip():
                    full_transcript.append(text.strip())
            except sr.UnknownValueError:
                continue
            except sr.RequestError as e:
                _set(job_id, status="failure", message=_friendly_error(f"Recognition error on chunk {i+1}", e))
                return

        if not full_transcript:
            _set(job_id, status="failure", message="No speech detected in any chunk.")
            return

        final_text = " ".join(full_transcript)
        out_file = os.path.join(out_dir, "transcript.txt")
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(final_text)

        _set(job_id, status="success", progress=100,
             message=f"Done (Google fallback, {len(full_transcript)} chunks)", result_path=_rel(out_file))

    except Exception as e:
        _set(job_id, status="failure", message=_friendly_error("Transcription failed", e))


# ---------------------------------------------------------------------------
# 2b. Text to speech - now a Celery task, ElevenLabs-first with gTTS fallback
# ---------------------------------------------------------------------------

@shared_task(bind=True)
def task_text_to_speech(self, job_id, text, lang, out_dir, voice_id=None):
    _set(job_id, status="running", progress=10, message="Preparing text...")

    if not text or not text.strip():
        _set(job_id, status="failure", message="No text provided.")
        return

    out_file = os.path.join(out_dir, "speech.mp3")

    try:
        engine_used = None
        if os.environ.get("ELEVENLABS_API_KEY"):
            _set(job_id, progress=40, message="Generating speech (ElevenLabs)...")
            ok = safe_call(
                synthesize_with_elevenlabs, text, out_file,
                voice_id=voice_id, on_error_return=None, log_prefix="TTS: ",
            )
            if ok is not None or os.path.exists(out_file):
                engine_used = "elevenlabs"

        if engine_used is None:
            _set(job_id, progress=60, message="Generating speech (gTTS fallback)...")
            _synthesize_with_gtts(text, lang or "en", out_file)
            engine_used = "gtts"

        if not os.path.exists(out_file):
            raise RuntimeError("No audio file was produced.")

        _set(job_id, status="success", progress=100, message=f"Done ({engine_used})", result_path=_rel(out_file))

    except Exception as e:
        _set(job_id, status="failure", message=_friendly_error("Speech generation failed", e))


# ---------------------------------------------------------------------------
# 3. Video to frames (zipped) - FFmpeg-based extraction (fast), OpenCV
#    fallback if ffmpeg is unavailable or errors
# ---------------------------------------------------------------------------

def _ffmpeg_extract_frames(video_path, frames_dir, frame_rate, progress_cb=None):
    import subprocess
    os.makedirs(frames_dir, exist_ok=True)
    if progress_cb:
        progress_cb("Extracting frames (FFmpeg)...")
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"fps={frame_rate}",
        "-qscale:v", "2",
        os.path.join(frames_dir, "frame_%05d.jpg"),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    saved = [f for f in os.listdir(frames_dir) if f.lower().endswith(".jpg")]
    if not saved:
        raise RuntimeError("FFmpeg produced no frames.")
    return len(saved)


def _opencv_extract_frames(video_path, frames_dir, frame_rate, progress_cb=None):
    import cv2
    os.makedirs(frames_dir, exist_ok=True)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError("Could not open video file.")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    step = max(1, int(fps / max(frame_rate, 0.1)))
    count, saved = 0, 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if count % step == 0:
            saved += 1
            cv2.imwrite(os.path.join(frames_dir, f"frame_{saved:05d}.jpg"), frame)
        count += 1
        if count % 20 == 0 and progress_cb:
            pct = min(95, int(count / total_frames * 100)) if total_frames else 50
            progress_cb(f"Extracted {saved} frames...", pct)
    cap.release()
    if saved == 0:
        raise RuntimeError("No frames were extracted.")
    return saved


@shared_task(bind=True)
def task_video_to_frames(self, job_id, video_path, frame_rate, out_dir):
    _set(job_id, status="running", progress=1, message="Reading video...")

    try:
        frames_dir = os.path.join(out_dir, "frames")

        try:
            def _progress(msg):
                _set(job_id, progress=50, message=msg)
            saved = _ffmpeg_extract_frames(video_path, frames_dir, frame_rate, progress_cb=_progress)
            engine = "ffmpeg"
        except Exception as ffmpeg_error:
            _set(job_id, progress=10, message=f"FFmpeg extraction unavailable ({ffmpeg_error}). Using OpenCV fallback...")
            def _progress2(msg, pct):
                _set(job_id, progress=pct, message=msg)
            saved = _opencv_extract_frames(video_path, frames_dir, frame_rate, progress_cb=_progress2)
            engine = "opencv"

        _set(job_id, progress=95, message="Zipping frames...")
        zip_path = os.path.join(out_dir, "frames.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in os.listdir(frames_dir):
                zf.write(os.path.join(frames_dir, fname), arcname=fname)

        _set(job_id, status="success", progress=100,
             message=f"Done ({engine}) - {saved} frames extracted.", result_path=_rel(zip_path))
    except Exception as e:
        _set(job_id, status="failure", message=_friendly_error("Frame extraction failed", e))


# ---------------------------------------------------------------------------
# 4. PDF to Word - unchanged
# ---------------------------------------------------------------------------

@shared_task(bind=True)
def task_pdf_to_word(self, job_id, pdf_path, out_dir):
    from pdf2docx import Converter

    _set(job_id, status="running", progress=10, message="Converting PDF...")

    try:
        base = os.path.splitext(os.path.basename(pdf_path))[0]
        docx_path = os.path.join(out_dir, f"{base}.docx")

        cv = Converter(pdf_path)
        cv.convert(docx_path, start=0, end=None)
        cv.close()

        _set(job_id, status="success", progress=100, message="Done", result_path=_rel(docx_path))
    except Exception as e:
        _set(job_id, status="failure", message=_friendly_error("PDF conversion failed", e))


# ---------------------------------------------------------------------------
# 5. Images to video - cleaned-up Ken Burns, optional AI motion, captions
# ---------------------------------------------------------------------------

def _make_ken_burns_clip(img_path, duration, target_size, zoom=0.15):
    """Simple, working zoom-in Ken Burns clip (no pan) - replaces the
    previous tangle of half-finished attempts with one clean implementation."""
    import moviepy.editor as mp

    base_clip = mp.ImageClip(img_path).set_duration(duration)
    tw, th = target_size

    def make_frame(t):
        z = 1.0 + zoom * (t / duration)
        frame = base_clip.get_frame(t)
        h, w = frame.shape[:2]
        # Resize is handled by moviepy's own resize before crop for correctness
        import numpy as np
        from PIL import Image
        im = Image.fromarray(frame).resize((int(w * z), int(h * z)), Image.LANCZOS)
        w2, h2 = im.size
        x = (w2 - tw) // 2
        y = (h2 - th) // 2
        cropped = im.crop((x, y, x + tw, y + th))
        return np.array(cropped)

    return mp.VideoClip(make_frame, duration=duration)


@shared_task(bind=True)
def task_images_to_video(self, job_id, images_dir, fps, audio_path, out_dir,
                          use_ken_burns=False, use_ai_motion=False,
                          script=None, voice_id=None, voice_lang="en",
                          generate_captions=False, out_dir_captions=None):
    """
    use_ai_motion: if True and REPLICATE_API_TOKEN is set, each image is
    animated with Stable Video Diffusion instead of (or in addition to)
    Ken Burns. Falls back to Ken Burns / static frames automatically if
    the API call fails or the token is missing.

    generate_captions: if True, burns/exports subtitles derived from the
    voiceover script (via Whisper alignment on the generated voiceover, so
    timing matches the actual audio rather than guessing from text length).
    """
    import moviepy.editor as mp
    from PIL import Image

    _set(job_id, status="running", progress=10, message="Assembling slideshow...")

    try:
        image_files = [
            os.path.join(images_dir, f)
            for f in _sorted_alnum(os.listdir(images_dir))
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))
        ]
        if not image_files:
            raise RuntimeError("No images found to build the video.")

        _set(job_id, progress=20, message="Preparing images...")

        norm_dir = os.path.join(images_dir, "_normalized")
        os.makedirs(norm_dir, exist_ok=True)
        target_size = None
        normalized_files = []
        for i, path in enumerate(image_files):
            with Image.open(path) as im:
                im = im.convert("RGB")
                if target_size is None:
                    target_size = im.size
                elif im.size != target_size:
                    im = im.resize(target_size, Image.LANCZOS)
                norm_path = os.path.join(norm_dir, f"frame_{i:05d}.jpg")
                im.save(norm_path, quality=95)
                normalized_files.append(norm_path)

        duration_per_image = max(2.0, 10.0 / len(image_files))

        _set(job_id, progress=30, message="Creating clips...")
        clips = []
        for idx, img_path in enumerate(normalized_files):
            clip = None
            if use_ai_motion and os.environ.get("REPLICATE_API_TOKEN"):
                _set(job_id, message=f"Animating image {idx+1}/{len(normalized_files)} with AI...")
                animated_path = os.path.join(out_dir, f"_ai_clip_{idx:03d}.mp4")
                ok_path = safe_call(
                    replicate_animate_image, img_path, animated_path,
                    on_error_return=None, log_prefix="Images-to-video: ",
                )
                if ok_path and os.path.exists(ok_path):
                    ai_clip = mp.VideoFileClip(ok_path)
                    clip = ai_clip.resize(newsize=target_size)

            if clip is None and use_ken_burns:
                clip = _make_ken_burns_clip(img_path, duration_per_image, target_size)

            if clip is None:
                clip = mp.ImageClip(img_path, duration=duration_per_image).resize(newsize=target_size)

            clips.append(clip)

        _set(job_id, progress=50, message="Concatenating clips...")
        final_clip = mp.concatenate_videoclips(clips, method="compose")

        voiceover_path = os.path.join(out_dir, "voiceover.mp3")
        have_voiceover = False
        if script:
            _set(job_id, progress=60, message="Generating voiceover...")
            engine_used = None
            if os.environ.get("ELEVENLABS_API_KEY"):
                ok = safe_call(
                    synthesize_with_elevenlabs, script, voiceover_path,
                    voice_id=voice_id, on_error_return=None, log_prefix="Voiceover: ",
                )
                if ok is not None or os.path.exists(voiceover_path):
                    engine_used = "elevenlabs"
            if engine_used is None:
                _synthesize_with_gtts(script, voice_lang, voiceover_path)

            have_voiceover = os.path.exists(voiceover_path)
            if have_voiceover:
                audio_clip = mp.AudioFileClip(voiceover_path)
                if audio_clip.duration > final_clip.duration:
                    audio_clip = audio_clip.subclip(0, final_clip.duration)
                final_clip = final_clip.set_audio(audio_clip)

        if audio_path and os.path.exists(audio_path):
            bg_audio = mp.AudioFileClip(audio_path)
            if bg_audio.duration > final_clip.duration:
                bg_audio = bg_audio.subclip(0, final_clip.duration)
            else:
                bg_audio = mp.afx.audio_loop(bg_audio, duration=final_clip.duration)

            if have_voiceover:
                voice_clip = mp.AudioFileClip(voiceover_path)
                if voice_clip.duration > final_clip.duration:
                    voice_clip = voice_clip.subclip(0, final_clip.duration)
                final_audio = mp.CompositeAudioClip([voice_clip.volumex(1.0), bg_audio.volumex(0.3)])
                final_clip = final_clip.set_audio(final_audio)
            else:
                final_clip = final_clip.set_audio(bg_audio)

        # ---- Captions/subtitles from the voiceover, timed via Whisper ----
        srt_path = None
        if generate_captions and have_voiceover:
            _set(job_id, progress=75, message="Generating captions...")
            whisper_result = safe_call(
                transcribe_with_whisper, voiceover_path, language=voice_lang,
                on_error_return=None, log_prefix="Captions: ",
            )
            if whisper_result and whisper_result.get("segments"):
                srt_path = whisper_segments_to_srt(
                    whisper_result["segments"],
                    os.path.join(out_dir_captions or out_dir, "captions.srt"),
                )

        _set(job_id, progress=85, message="Rendering final video...")
        out_file = os.path.join(out_dir, "slideshow.mp4")
        final_clip.write_videofile(out_file, codec="libx264", audio_codec="aac", logger=None, threads=4)

        message = "Done" + (" (captions.srt generated)" if srt_path else "")
        _set(job_id, status="success", progress=100, message=message, result_path=_rel(out_file))

    except Exception as e:
        _set(job_id, status="failure", message=_friendly_error("Video generation failed", e))


# ---------------------------------------------------------------------------
# 6. AI Background Remover - rembg default, Replicate opt-in, batch support
# ---------------------------------------------------------------------------

_REMBG_SESSION = None

def _get_rembg_session():
    global _REMBG_SESSION
    if _REMBG_SESSION is None:
        from rembg import new_session
        _REMBG_SESSION = new_session("u2netp")
    return _REMBG_SESSION


def _remove_background_one(image_path, out_file, use_replicate):
    from PIL import Image
    if use_replicate and os.environ.get("REPLICATE_API_TOKEN"):
        png_bytes = safe_call(replicate_remove_background, image_path, on_error_return=None, log_prefix="BG removal: ")
        if png_bytes:
            with open(out_file, "wb") as f:
                f.write(png_bytes)
            return "replicate"

    from rembg import remove
    session = _get_rembg_session()
    img = Image.open(image_path).convert("RGB")
    cutout = remove(img, session=session)
    cutout.save(out_file)
    return "rembg"


@shared_task(bind=True)
def task_remove_background(self, job_id, image_path, out_dir, use_replicate=False):
    _set(job_id, status="running", progress=10, message="Loading model...")
    try:
        out_file = os.path.join(out_dir, "no-background.png")
        engine = _remove_background_one(image_path, out_file, use_replicate)
        _set(job_id, status="success", progress=100, message=f"Done ({engine})", result_path=_rel(out_file))
    except Exception as e:
        _set(job_id, status="failure",
             message=_friendly_error("Background removal failed (first run needs internet to fetch the AI model)", e))


@shared_task(bind=True)
def task_remove_background_batch(self, job_id, image_paths, out_dir, use_replicate=False):
    """Batch version: processes a list of image paths, zips the results."""
    _set(job_id, status="running", progress=5, message=f"Processing {len(image_paths)} images...")
    try:
        results_dir = os.path.join(out_dir, "results")
        os.makedirs(results_dir, exist_ok=True)

        for i, image_path in enumerate(image_paths):
            base = os.path.splitext(os.path.basename(image_path))[0]
            out_file = os.path.join(results_dir, f"{base}_no_bg.png")
            try:
                _remove_background_one(image_path, out_file, use_replicate)
            except Exception as e:
                print(f"Batch bg removal: {image_path} failed: {e}")
            pct = int((i + 1) / len(image_paths) * 90)
            _set(job_id, progress=pct, message=f"Processed {i+1}/{len(image_paths)}...")

        zip_path = os.path.join(out_dir, "no-background-batch.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in os.listdir(results_dir):
                zf.write(os.path.join(results_dir, fname), arcname=fname)

        _set(job_id, status="success", progress=100, message="Done", result_path=_rel(zip_path))
    except Exception as e:
        _set(job_id, status="failure", message=_friendly_error("Batch background removal failed", e))


# ---------------------------------------------------------------------------
# 7. AI Background Replacer - unchanged logic, friendlier errors
# ---------------------------------------------------------------------------

def _lighting_gradient(size, direction, intensity=0.25):
    import numpy as np
    w, h = size
    grad = np.ones((h, w), dtype=np.float32)
    if direction == "left":
        grad = 1 - (np.linspace(0, 1, w) * intensity)[None, :].repeat(h, axis=0)
    elif direction == "right":
        grad = 1 - (np.linspace(1, 0, w) * intensity)[None, :].repeat(h, axis=0)
    elif direction == "top":
        grad = 1 - (np.linspace(0, 1, h) * intensity)[:, None].repeat(w, axis=1)
    elif direction == "bottom":
        grad = 1 - (np.linspace(1, 0, h) * intensity)[:, None].repeat(w, axis=1)
    return grad


@shared_task(bind=True)
def task_replace_background(self, job_id, subject_path, background_path, light_direction, out_dir, use_replicate=False):
    from PIL import Image, ImageFilter
    import numpy as np

    _set(job_id, status="running", progress=10, message="Extracting subject...")

    try:
        cutout_path = os.path.join(out_dir, "_subject_cutout.png")
        _remove_background_one(subject_path, cutout_path, use_replicate)
        subject = Image.open(cutout_path).convert("RGBA")

        _set(job_id, progress=40, message="Preparing background...")
        background = Image.open(background_path).convert("RGB")
        bg_w, bg_h = background.size

        sw, sh = subject.size
        max_h = int(bg_h * 0.85)
        max_w = int(bg_w * 0.85)
        scale = min(max_w / sw, max_h / sh, 1.0) if sw and sh else 1.0
        if scale < 1.0:
            subject = subject.resize((max(1, int(sw * scale)), max(1, int(sh * scale))), Image.LANCZOS)

        if light_direction and light_direction != "none":
            _set(job_id, progress=60, message="Matching lighting...")
            arr = np.array(subject).astype(np.float32)
            gradient = _lighting_gradient(subject.size, light_direction)
            for c in range(3):
                arr[:, :, c] = np.clip(arr[:, :, c] * gradient, 0, 255)
            subject = Image.fromarray(arr.astype(np.uint8), mode="RGBA")

        sw, sh = subject.size
        x = (bg_w - sw) // 2
        y = max(0, bg_h - sh - int(bg_h * 0.04))

        _set(job_id, progress=75, message="Compositing...")

        canvas = background.convert("RGBA")
        alpha = subject.split()[-1]
        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        shadow_shape = Image.new("L", subject.size, 0)
        shadow_shape.paste(alpha, (0, 0))
        shadow_shape = shadow_shape.filter(ImageFilter.GaussianBlur(radius=max(4, sw // 40)))
        shadow_layer = Image.new("RGBA", subject.size, (0, 0, 0, 120))
        shadow_layer.putalpha(shadow_shape.point(lambda p: int(p * 0.5)))
        shadow.paste(shadow_layer, (x, y + int(sh * 0.03)), shadow_layer)
        canvas = Image.alpha_composite(canvas, shadow)
        canvas.paste(subject, (x, y), subject)

        out_file = os.path.join(out_dir, "composite.png")
        canvas.convert("RGB").save(out_file, quality=95)

        _set(job_id, status="success", progress=100, message="Done", result_path=_rel(out_file))
    except Exception as e:
        _set(job_id, status="failure",
             message=_friendly_error("Background replacement failed (first run needs internet to fetch the AI model)", e))


# ---------------------------------------------------------------------------
# 8. Photo Repair - OpenCV pipeline by default, optional Real-ESRGAN AI
#    restore pass (denoise + detail + optional face enhance + upscale)
# ---------------------------------------------------------------------------

@shared_task(bind=True)
def task_repair_photo(self, job_id, image_path, out_dir, use_ai=None, ai_scale=2, face_enhance=True):
    """
    use_ai: True/False to force the Replicate Real-ESRGAN restore pass on/off.
        None = auto-detect from REPLICATE_API_TOKEN. If AI is requested but
        fails, falls back to the OpenCV pipeline automatically.
    """
    import cv2

    _set(job_id, status="running", progress=10, message="Loading image...")

    if use_ai is None:
        use_ai = bool(os.environ.get("REPLICATE_API_TOKEN"))

    try:
        ext = os.path.splitext(image_path)[1].lower()
        ext = ext if ext in (".jpg", ".jpeg", ".png") else ".jpg"
        out_file = os.path.join(out_dir, f"repaired{ext}")

        if use_ai:
            _set(job_id, progress=30, message="Restoring with Real-ESRGAN...")
            ok_path = safe_call(
                replicate_restore_image, image_path, out_file, ai_scale, face_enhance,
                on_error_return=None, log_prefix="Photo repair: ",
            )
            if ok_path and os.path.exists(ok_path):
                _set(job_id, status="success", progress=100, message="Done (Real-ESRGAN)", result_path=_rel(out_file))
                return
            _set(job_id, progress=30, message="AI restore unavailable, using local pipeline...")

        img = cv2.imread(image_path, cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("Could not read this image file.")

        _set(job_id, progress=45, message="Denoising (color preserved)...")
        denoised = cv2.fastNlMeansDenoisingColored(img, None, 6, 6, 7, 21)

        _set(job_id, progress=65, message="Restoring contrast...")
        lab = cv2.cvtColor(denoised, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        l = clahe.apply(l)
        contrast_boosted = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)

        _set(job_id, progress=85, message="Sharpening...")
        blurred = cv2.GaussianBlur(contrast_boosted, (0, 0), 2.0)
        sharpened = cv2.addWeighted(contrast_boosted, 1.5, blurred, -0.5, 0)

        cv2.imwrite(out_file, sharpened)

        _set(job_id, status="success", progress=100, message="Done (local pipeline)", result_path=_rel(out_file))
    except Exception as e:
        _set(job_id, status="failure", message=_friendly_error("Photo repair failed", e))


# ---------------------------------------------------------------------------
# 9. Remove / Replace Video Audio - FFmpeg stream-copy (fast, no
#    re-encoding of video), moviepy fallback if ffmpeg call fails
# ---------------------------------------------------------------------------

def _ffmpeg_remove_or_replace_audio(video_path, replacement_audio_path, out_file):
    import subprocess
    if replacement_audio_path and os.path.exists(replacement_audio_path):
        cmd = [
            "ffmpeg", "-y", "-i", video_path, "-i", replacement_audio_path,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-shortest", out_file,
        ]
    else:
        cmd = ["ffmpeg", "-y", "-i", video_path, "-map", "0:v:0", "-an", "-c:v", "copy", out_file]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out_file


@shared_task(bind=True)
def task_remove_audio(self, job_id, video_path, replacement_audio_path, out_dir):
    _set(job_id, status="running", progress=10, message="Loading video...")

    out_file = os.path.join(
        out_dir, "video_no_audio.mp4" if not replacement_audio_path else "video_new_audio.mp4"
    )

    try:
        _set(job_id, progress=30,
             message="Attaching new audio track (FFmpeg)..." if replacement_audio_path else "Stripping audio track (FFmpeg)...")
        _ffmpeg_remove_or_replace_audio(video_path, replacement_audio_path, out_file)
        _set(job_id, status="success", progress=100, message="Done", result_path=_rel(out_file))
        return
    except Exception as ffmpeg_error:
        _set(job_id, progress=40, message=f"FFmpeg path unavailable ({ffmpeg_error}). Falling back to moviepy...")

    try:
        import moviepy.editor as mp

        clip = mp.VideoFileClip(video_path)
        silent = clip.without_audio()

        if replacement_audio_path and os.path.exists(replacement_audio_path):
            audio = mp.AudioFileClip(replacement_audio_path)
            if audio.duration > silent.duration:
                audio = audio.subclip(0, silent.duration)
            final = silent.set_audio(audio)
            audio_codec = "aac"
        else:
            final = silent
            audio_codec = None

        _set(job_id, progress=70, message="Rendering video (this can take a while)...")
        if audio_codec:
            final.write_videofile(out_file, codec="libx264", audio_codec=audio_codec, logger=None)
        else:
            final.write_videofile(out_file, codec="libx264", audio=False, logger=None)

        _set(job_id, status="success", progress=100, message="Done (moviepy fallback)", result_path=_rel(out_file))
    except Exception as e:
        _set(job_id, status="failure", message=_friendly_error("Audio operation failed", e))


# ---------------------------------------------------------------------------
# 10. Video Enhancer - FFmpeg-first pipeline (fast, scales to 4K/120fps),
#     with optional Replicate AI passes (Real-ESRGAN upscale, RIFE
#     interpolation) layered on top. Falls back to pure FFmpeg if AI is
#     disabled, unavailable, or fails partway through.
# ---------------------------------------------------------------------------

_ENHANCE_RESOLUTIONS = {
    "original": None,
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "4k": (3840, 2160),
}
_ALLOWED_FPS = {24, 25, 30, 48, 60, 120}
_MAX_ENHANCE_SECONDS = 15 * 60  # refuse videos longer than 15 minutes on a shared server


def _probe_video(path):
    """Read basic stream info with ffprobe (already ships with ffmpeg)."""
    import subprocess, json
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    vstream = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if not vstream:
        raise RuntimeError("No video stream found in file.")
    num, den = (vstream.get("avg_frame_rate") or "25/1").split("/")
    fps = float(num) / float(den) if float(den) else 25.0
    duration = float(data.get("format", {}).get("duration") or 0)
    has_audio = any(s["codec_type"] == "audio" for s in data["streams"])
    return {
        "width": int(vstream["width"]),
        "height": int(vstream["height"]),
        "fps": fps,
        "duration": duration,
        "has_audio": has_audio,
    }


def _hwaccel_args():
    """Optional GPU decode/encode flags, off by default. Set FFMPEG_HWACCEL
    to 'cuda' or 'vulkan' only if your deployment actually has that hardware
    and matching ffmpeg build - otherwise leave unset."""
    hwaccel = os.environ.get("FFMPEG_HWACCEL")
    if hwaccel == "cuda":
        return ["-hwaccel", "cuda"], ["-c:v", "h264_nvenc"]
    if hwaccel == "vulkan":
        return ["-hwaccel", "vulkan"], ["-c:v", "libx264"]
    return [], ["-c:v", "libx264"]


def _build_filter_chain(target_size, denoise, sharpen, color_enhance, src_size):
    """Compose a single -vf filter graph so ffmpeg does one encode pass
    instead of several (much faster, avoids repeated generation loss)."""
    filters = []

    denoise_strengths = {"light": "4:7:5:7", "medium": "8:7:5:7", "strong": "12:7:5:7"}
    if denoise and denoise != "off":
        filters.append(f"hqdn3d" if denoise == "light" else f"nlmeans={denoise_strengths.get(denoise, denoise_strengths['medium'])}")

    if target_size and target_size != src_size:
        tw, th = target_size
        # scale then pad/crop is unnecessary here since we preserve aspect via -2
        filters.append(f"scale={tw}:-2:flags=lanczos")

    if sharpen and sharpen != "off":
        amount = "1.0:1.0:0.0" if sharpen == "light" else "1.5:1.5:0.0"
        filters.append(f"unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount={amount.split(':')[0]}")

    if color_enhance:
        # gentle auto contrast/saturation lift - subtle, not a heavy "HDR" look
        filters.append("eq=contrast=1.06:saturation=1.08:brightness=0.01")

    return ",".join(filters) if filters else None


def _ffmpeg_encode(in_path, out_path, vf_filter, out_fps, hwaccel_in, hwaccel_out, crf=18):
    import subprocess
    cmd = ["ffmpeg", "-y", *hwaccel_in, "-i", in_path]
    if vf_filter:
        cmd += ["-vf", vf_filter]
    if out_fps:
        cmd += ["-r", str(out_fps)]
    cmd += [*hwaccel_out, "-preset", "medium", "-crf", str(crf), "-c:a", "copy", out_path]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out_path


def _ffmpeg_interpolate(in_path, out_path, target_fps):
    """Fast, always-available fallback for frame interpolation using
    ffmpeg's motion-compensated minterpolate filter. Slower per-frame than
    a simple duplicate/blend, but far better quality, and needs no model."""
    import subprocess
    vf = f"minterpolate=fps={target_fps}:mi_mode=mci:mc_mode=aobmc:vsbmc=1"
    cmd = ["ffmpeg", "-y", "-i", in_path, "-vf", vf, "-c:a", "copy", out_path]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out_path


@shared_task(bind=True)
def task_enhance_video(self, job_id, video_path, out_dir,
                        target="original", target_resolution=None, target_fps=None,
                        denoise="off", sharpen="light", smooth_fps=False,
                        color_enhance=False, use_ai=None):
    """
    target / target_resolution: "original" | "720p" | "1080p" | "4k".
        target_resolution is the new name; target is kept for backward
        compatibility with the existing form field and is used if
        target_resolution is not passed.
    target_fps: desired output fps (24/25/30/48/60/120). If None, keeps
        the source fps unless smooth_fps is set (then doubles it, capped
        at 60, matching the previous behavior).
    use_ai: True/False to force AI passes on/off. None = auto-detect from
        REPLICATE_API_TOKEN / ENABLE_AI_VIDEO_ENHANCE env vars.
    """
    _set(job_id, status="running", progress=1, message="Reading video...")

    try:
        info = _probe_video(video_path)
        if info["duration"] > _MAX_ENHANCE_SECONDS:
            raise RuntimeError(
                f"Video is {info['duration']/60:.1f} min long - this tool is capped at "
                f"{_MAX_ENHANCE_SECONDS//60} min on a shared server. Trim it first."
            )

        res_key = target_resolution or target or "original"
        target_size = _ENHANCE_RESOLUTIONS.get(res_key)
        src_size = (info["width"], info["height"])

        desired_fps = target_fps or (min(info["fps"] * 2, 60) if smooth_fps else info["fps"])
        desired_fps = min(desired_fps, 120)
        # snap to nearest allowed value so we don't ask ffmpeg for odd fps
        desired_fps = min(_ALLOWED_FPS, key=lambda x: abs(x - desired_fps))

        if use_ai is None:
            use_ai = bool(os.environ.get("REPLICATE_API_TOKEN")) and \
                      os.environ.get("ENABLE_AI_VIDEO_ENHANCE", "true").lower() != "false"

        # ---- Phase 1: FFmpeg pass - denoise, sharpen, color, base scale ----
        _set(job_id, progress=10, message="Denoising and sharpening (FFmpeg)...")
        hwaccel_in, hwaccel_out = _hwaccel_args()

        # If AI upscaling will run later, don't also scale here past a
        # reasonable pre-upscale size - AI models expect a smaller input.
        pre_ai_upscale = use_ai and target_size and target_size[1] > src_size[1]
        vf_target_size = None if pre_ai_upscale else target_size

        vf = _build_filter_chain(vf_target_size, denoise, sharpen, color_enhance, src_size)
        stage1_path = os.path.join(out_dir, "_stage1.mp4")
        _ffmpeg_encode(video_path, stage1_path, vf, None, hwaccel_in, hwaccel_out)
        working_path = stage1_path

        # ---- Phase 2: AI upscale (Real-ESRGAN via Replicate), if needed ----
        if pre_ai_upscale:
            _set(job_id, progress=45, message=f"Upscaling to {res_key} with Real-ESRGAN...")
            scale_factor = max(2, round(target_size[1] / src_size[1]))
            upscaled_path = os.path.join(out_dir, "_ai_upscaled.mp4")
            ok_path = safe_call(
                replicate_upscale_video, working_path, upscaled_path, scale_factor,
                on_error_return=None, log_prefix="Video enhance: ",
            )
            if ok_path and os.path.exists(ok_path):
                working_path = ok_path
            else:
                # AI failed - fall back to plain FFmpeg scale to hit the target anyway
                _set(job_id, progress=50, message="AI upscale unavailable, using FFmpeg scale fallback...")
                fallback_path = os.path.join(out_dir, "_ffmpeg_scaled.mp4")
                fallback_vf = _build_filter_chain(target_size, None, None, False, src_size)
                _ffmpeg_encode(working_path, fallback_path, fallback_vf, None, hwaccel_in, hwaccel_out)
                working_path = fallback_path

        # ---- Phase 3: frame interpolation to reach target_fps ----
        needs_interpolation = desired_fps > round(info["fps"]) + 1
        if needs_interpolation:
            interpolated_path = os.path.join(out_dir, "_interpolated.mp4")
            done = False
            if use_ai:
                _set(job_id, progress=70, message=f"Interpolating motion to {int(desired_fps)}fps with RIFE...")
                ok_path = safe_call(
                    replicate_interpolate_frames, working_path, interpolated_path, int(desired_fps),
                    on_error_return=None, log_prefix="Video enhance: ",
                )
                if ok_path and os.path.exists(ok_path):
                    working_path = ok_path
                    done = True
            if not done:
                _set(job_id, progress=75, message=f"Interpolating motion to {int(desired_fps)}fps (FFmpeg minterpolate)...")
                _ffmpeg_interpolate(working_path, interpolated_path, int(desired_fps))
                working_path = interpolated_path

        # ---- Phase 4: re-attach original audio ----
        _set(job_id, progress=92, message="Restoring audio track...")
        out_file = os.path.join(out_dir, "enhanced.mp4")
        if info["has_audio"]:
            import subprocess
            cmd = [
                "ffmpeg", "-y", "-i", working_path, "-i", video_path,
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-shortest", out_file,
            ]
            try:
                subprocess.run(cmd, check=True, capture_output=True, text=True)
            except Exception:
                shutil.copy(working_path, out_file)
        else:
            shutil.copy(working_path, out_file)

        # ---- Cleanup intermediates ----
        for tmp in ("_stage1.mp4", "_ai_upscaled.mp4", "_ffmpeg_scaled.mp4", "_interpolated.mp4"):
            tmp_path = os.path.join(out_dir, tmp)
            if os.path.exists(tmp_path) and tmp_path != out_file:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        _set(job_id, status="success", progress=100,
             message=f"Done - {res_key}, {int(desired_fps)}fps" + (" (AI-assisted)" if use_ai else ""),
             result_path=_rel(out_file))

    except Exception as e:
        _set(job_id, status="failure", message=_friendly_error("Video enhancement failed", e))


# ---------------------------------------------------------------------------
# Speech Translator - Whisper transcription, DeepL/Google/NLLB translation,
# ElevenLabs/gTTS synthesis. Expanded language coverage lives in
# speech_helpers.SUPPORTED_LANGUAGES (100+ ISO 639-1 codes).
# ---------------------------------------------------------------------------

@shared_task(bind=True, name="tools.translate_speech_task")
def translate_speech_task(self, job_id: int, source_audio_path: str, target_lang: str,
                           source_lang: str = None, elevenlabs_voice_id: str = None):
    """
    Full speech-to-speech translation pipeline, run asynchronously.

      1. Transcribe source audio -> text (Whisper, via speech_helpers.transcribe_audio)
      2. Translate text -> target language (DeepL preferred, Google fallback, NLLB last resort)
      3. Synthesize translated text -> speech mp3 (ElevenLabs preferred, gTTS fallback)
      4. Update the Job row with the result path and status
    """
    job = Job.query.get(job_id)
    if job is None:
        return {"status": "error", "message": f"Job {job_id} not found"}

    try:
        job.status = "running"
        job.progress = 10
        job.stage = "transcribing"
        job.message = "Transcribing source audio..."
        db.session.commit()

        asr_result = transcribe_audio(source_audio_path, lang_code=source_lang)
        transcript = asr_result["text"]
        detected_lang = asr_result["detected_lang"]

        if not transcript.strip():
            raise ValueError("No speech detected in the uploaded audio file.")

        job.progress = 40
        job.stage = "translating"
        job.message = "Translating transcript..."
        job.result_meta = {"transcript": transcript, "detected_lang": detected_lang}
        db.session.commit()

        translation = translate_text(transcript, target_lang=target_lang, source_lang=detected_lang)

        job.progress = 65
        job.stage = "synthesizing"
        job.message = "Synthesizing translated speech..."
        job.result_meta = {
            **(job.result_meta or {}),
            "translated_text": translation.translated_text,
            "translation_engine": translation.engine_used,
        }
        db.session.commit()

        output_dir = os.path.join("app", "static", "generated", "speech_translator")
        os.makedirs(output_dir, exist_ok=True)
        output_filename = f"job_{job_id}_translated.mp3"
        output_path = os.path.join(output_dir, output_filename)

        tts_result = synthesize_speech(
            translation.translated_text,
            lang_code=target_lang,
            output_path=output_path,
            elevenlabs_voice_id=elevenlabs_voice_id,
        )

        job.status = "success"
        job.progress = 100
        job.stage = "done"
        job.message = "Done"
        job.result_path = _rel(output_path)
        job.result_meta = {
            **(job.result_meta or {}),
            "tts_engine": tts_result["engine_used"],
            "target_lang": target_lang,
            "target_lang_name": SUPPORTED_LANGUAGES.get(target_lang, target_lang),
        }
        db.session.commit()

        return {"status": "success", "job_id": job_id, "output_path": output_path}

    except Exception as exc:
        db.session.rollback()
        job = Job.query.get(job_id)
        if job:
            job.status = "failure"
            job.stage = "error"
            job.message = _friendly_error("Speech translation failed", exc)
            db.session.commit()
        else:
            traceback.print_exc()
        return {"status": "error", "job_id": job_id, "message": str(exc)}


@shared_task(bind=True)
def task_combine_to_pdf(self, job_id, file_paths, options, out_dir):
    """
    file_paths: list of absolute paths, already in the order the user wants
        them to appear in the final PDF (routes.py is responsible for
        applying any drag-and-drop reordering before calling .delay()).
    options: dict - see pdf_helpers.combine_files_to_pdf for the accepted
        keys (page_size, orientation, header_text, footer_text,
        page_numbers, date_text, custom_size).
    """
    _set(job_id, status="running", progress=1, message="Preparing files...")
 
    if not file_paths:
        _set(job_id, status="failure", message="No files were uploaded.")
        return
 
    try:
        work_dir = os.path.join(out_dir, "_work")
        os.makedirs(work_dir, exist_ok=True)
        out_file = os.path.join(out_dir, "combined.pdf")
 
        total = len(file_paths)
 
        def _progress(done, total_count):
            # Reserve the last ~10% for the merge + header/footer pass so
            # the bar doesn't sit at 100% while that work still runs.
            pct = 5 + int(done / total_count * 85)
            _set(job_id, progress=pct, message=f"Converting file {done}/{total_count}...")
 
        _set(job_id, progress=5, message=f"Converting {total} file(s)...")
        result_path, skipped = combine_files_to_pdf(
            file_paths, options, work_dir, out_file, progress_cb=_progress
        )
 
        _set(job_id, progress=95, message="Finalizing PDF...")
 
        # Clean up per-file intermediates and thumbnails; keep only the
        # final combined.pdf in out_dir.
        shutil.rmtree(work_dir, ignore_errors=True)
 
        message = "Done"
        if skipped:
            message += f" - skipped {len(skipped)} file(s): {', '.join(skipped)}"
 
        _set(job_id, status="success", progress=100, message=message, result_path=_rel(result_path))
 
    except Exception as e:
        _set(job_id, status="failure", message=_friendly_error("Combine to PDF failed", e))

 
@shared_task(bind=True)
def task_translate_video_speech(self, job_id, video_path, target_lang, out_dir,
                                 source_lang=None, elevenlabs_voice_id=None,
                                 generate_subtitles=False):
    """
    Pipeline: extract audio -> transcribe (Whisper) -> translate -> synthesize
    dubbed speech -> mux back onto the video -> (optional) subtitles.
 
    source_lang: ISO 639-1 code to force transcription language, or None to
        let Whisper auto-detect.
    """
    _set(job_id, status="running", progress=2, message="Extracting audio...")
 
    try:
        work_dir = os.path.join(out_dir, "_work")
        os.makedirs(work_dir, exist_ok=True)
 
        # ---- 1. Extract audio ----
        original_audio_path = os.path.join(work_dir, "original_audio.wav")
        extract_audio_from_video(video_path, original_audio_path)
 
        # ---- 2. Transcribe (Whisper) ----
        _set(job_id, progress=15, message="Transcribing speech...")
 
        def _whisper_progress(msg):
            _set(job_id, progress=20, message=msg)
 
        transcription = transcribe_with_whisper(
            original_audio_path, language=source_lang, progress_cb=_whisper_progress
        )
        transcript_text = transcription["text"]
        detected_lang = transcription["detected_lang"]
 
        if not transcript_text.strip():
            _set(job_id, status="failure",
                 message="No speech was detected in this video. Please try a different file.")
            return
 
        # ---- 3. Translate ----
        _set(job_id, progress=40, message=f"Translating from {detected_lang} to {target_lang}...")
        translation = translate_text(transcript_text, target_lang=target_lang, source_lang=detected_lang)
 
        if not translation.translated_text.strip():
            _set(job_id, status="failure", message="Translation returned no text.")
            return
 
        # ---- 4. Synthesize dubbed speech ----
        _set(job_id, progress=60, message="Synthesizing translated speech...")
        dubbed_audio_path = os.path.join(work_dir, "dubbed_audio.mp3")
        tts_result = synthesize_speech(
            translation.translated_text,
            lang_code=target_lang,
            output_path=dubbed_audio_path,
            elevenlabs_voice_id=elevenlabs_voice_id,
        )
 
        # ---- 5. Replace the video's audio track ----
        _set(job_id, progress=80, message="Replacing audio track in video...")
        out_video = os.path.join(out_dir, "translated_video.mp4")
        replace_audio_in_video(video_path, dubbed_audio_path, out_video)
 
        # ---- 6. Optional subtitles, timed to the DUBBED audio (not the
        #         original) so captions match what's actually being heard ----
        srt_rel_path = None
        if generate_subtitles:
            _set(job_id, progress=90, message="Generating subtitles...")
            caption_result = safe_call(
                transcribe_with_whisper, dubbed_audio_path, language=target_lang,
                on_error_return=None, log_prefix="Video translator captions: ",
            )
            if caption_result and caption_result.get("segments"):
                srt_path = os.path.join(out_dir, "subtitles.srt")
                whisper_segments_to_srt(caption_result["segments"], srt_path)
                srt_rel_path = _rel(srt_path)
 
        shutil.rmtree(work_dir, ignore_errors=True)
 
        job = _set(
            job_id, status="success", progress=100,
            message=f"Done - dubbed in {SUPPORTED_LANGUAGES.get(target_lang, target_lang)}"
                    f" (translation: {translation.engine_used}, speech: {tts_result['engine_used']})"
                    + (", subtitles ready" if srt_rel_path else ""),
            result_path=_rel(out_video),
        )
        if job is not None:
            job.result_meta = {
                "detected_source_lang": detected_lang,
                "target_lang": target_lang,
                "translation_engine": translation.engine_used,
                "tts_engine": tts_result["engine_used"],
                "subtitles_path": srt_rel_path,
            }
            db.session.commit()
 
    except Exception as e:
        _set(job_id, status="failure", message=_friendly_error("Video speech translation failed", e))


# ---------------------------------------------------------------------------
# 15. Automated Video Editing Pipeline (flagship tool)
# ---------------------------------------------------------------------------

from app.tools.editing_helpers import (
    load_template,
    trim_silence,
    apply_transitions,
    overlay_text,
    burn_captions,
    duck_background_music,
    apply_color_grade,
    export_multiple_formats,
    zip_outputs,
    concatenate_clips,
    detect_highlights,
    extract_highlight_reel,
)

_MIN_CLIP_SECONDS = 2.0


@shared_task(bind=True)
def task_auto_edit_video(self, job_id, clip_paths, out_dir, template_name="cinematic",
                          custom_template_path=None, trim_silence_enabled=True,
                          silence_threshold=1.0, generate_captions=True,
                          caption_language=None, title_text=None,
                          music_path=None, output_formats=None,
                          auto_highlights=False, highlight_duration=30.0):
    """
    clip_paths: list of uploaded video paths, in the order they should be
        concatenated.
    template_name: one of the built-in templates ("cinematic",
        "social_short", "slideshow", "podcast_highlights") or "custom".
    custom_template_path: path to a user-uploaded JSON config - used when
        template_name == "custom".
    output_formats: list from {"1080p", "4k", "vertical", "square"}.
        Defaults to ["1080p"] if empty/None.
    auto_highlights: if True, automatically select and keep only the most
        "interesting" ~highlight_duration seconds of the assembled footage
        (see editing_helpers.detect_highlights for the scoring approach)
        before proceeding to color grade / captions / music / export.
    highlight_duration: target length in seconds for the highlight reel,
        only used when auto_highlights is True.
    """
    _set(job_id, status="running", progress=1, message="Analysing input...")
 
    if not clip_paths:
        _set(job_id, status="failure", message="No video files were uploaded.")
        return
 
    output_formats = output_formats or ["1080p"]
    work_dir = os.path.join(out_dir, "_work")
    os.makedirs(work_dir, exist_ok=True)
 
    try:
        template = load_template(custom_template_path if template_name == "custom" else template_name)
 
        # ---- Step 1: validate + per-clip silence trimming ----
        processed_clips = []
        for idx, clip_path in enumerate(clip_paths):
            duration = None
            try:
                import subprocess as _sp, json as _json
                probe = _sp.run(
                    ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", clip_path],
                    capture_output=True, text=True, check=True,
                )
                duration = float(_json.loads(probe.stdout).get("format", {}).get("duration") or 0)
            except Exception:
                pass
            if duration is not None and duration < _MIN_CLIP_SECONDS:
                _set(job_id, status="failure",
                     message=f"Clip {idx + 1} is too short ({duration:.1f}s) - minimum is {_MIN_CLIP_SECONDS:.0f}s.")
                return
 
            current_path = clip_path
            if trim_silence_enabled:
                _set(job_id, progress=5 + int(idx / len(clip_paths) * 12),
                     message=f"Trimming silence in clip {idx + 1}/{len(clip_paths)}...")
                trimmed_path = os.path.join(work_dir, f"_trimmed_{idx:03d}.mp4")
                try:
                    current_path, _removed = trim_silence(current_path, trimmed_path, threshold=silence_threshold)
                except Exception as e:
                    _set(job_id, message=f"Silence trim skipped for clip {idx + 1} ({e}); using original.")
                    current_path = clip_path
            processed_clips.append(current_path)
 
        # ---- Step 2: transitions / concatenation ----
        _set(job_id, progress=22, message="Applying transitions...")
        transition = template.get("transition", {"type": "crossfade", "duration": 0.5})
        combined_path = os.path.join(work_dir, "_combined.mp4")
        combined_path = apply_transitions(
            processed_clips, combined_path,
            transition_type=transition.get("type", "crossfade"),
            duration=transition.get("duration", 0.5),
        )
 
        # ---- Step 2b: highlight reel (optional) ----
        # Transcribed here (once) if needed, both to score highlight
        # windows by speech density and to hand off to the caption step
        # below so it isn't transcribed a second time.
        pre_transcribed = None
        if auto_highlights:
            _set(job_id, progress=30, message="Finding the best moments...")
            try:
                highlight_audio_path = os.path.join(work_dir, "_highlight_audio.wav")
                from app.tools.video_helpers import extract_audio_from_video
                extract_audio_from_video(combined_path, highlight_audio_path)
 
                pre_transcribed = safe_call(
                    transcribe_with_whisper, highlight_audio_path, language=caption_language,
                    on_error_return=None, log_prefix="Highlight detection: ",
                )
                whisper_segments = pre_transcribed.get("segments") if pre_transcribed else None
 
                highlight_ranges = detect_highlights(
                    combined_path, target_duration=highlight_duration,
                    whisper_segments=whisper_segments,
                )
                if highlight_ranges:
                    highlighted_path = os.path.join(work_dir, "_highlighted.mp4")
                    combined_path = extract_highlight_reel(
                        combined_path, highlight_ranges, highlighted_path, work_dir
                    )
                    _set(job_id, message=f"Kept {len(highlight_ranges)} best moment(s), "
                                          f"~{sum(e - s for s, e in highlight_ranges):.0f}s total.")
            except Exception as e:
                _set(job_id, message=f"Highlight detection skipped ({e}); using full edit.")
 
        # ---- Step 3: color grade ----
        _set(job_id, progress=40, message="Applying colour grade...")
        graded_path = os.path.join(work_dir, "_graded.mp4")
        combined_path = apply_color_grade(combined_path, graded_path, template.get("color_grade", {}))
 
        # ---- Step 4: title overlay ----
        if title_text:
            _set(job_id, progress=48, message="Adding title overlay...")
            titled_path = os.path.join(work_dir, "_titled.mp4")
            combined_path = overlay_text(
                combined_path, titled_path, title_text,
                style=template.get("text_style"), start=0.0, duration=4.0,
            )
 
        # ---- Step 5: captions (Whisper) ----
        srt_rel_path = None
        if generate_captions:
            _set(job_id, progress=55, message="Transcribing for captions...")
            try:
                # Reuse the highlight-detection transcript if we already
                # have one AND no highlight cut happened after it (title
                # overlay/color-grade don't change timing, so the earlier
                # transcript's timestamps are still valid in that case).
                # If a highlight reel WAS extracted, the timeline changed,
                # so we must re-transcribe the (now-shorter) result instead
                # of reusing timestamps that no longer line up.
                if pre_transcribed and not auto_highlights:
                    caption_result = pre_transcribed
                else:
                    audio_path = os.path.join(work_dir, "_captions_audio.wav")
                    from app.tools.video_helpers import extract_audio_from_video
                    extract_audio_from_video(combined_path, audio_path)
 
                    def _progress(msg):
                        _set(job_id, progress=60, message=msg)
 
                    caption_result = safe_call(
                        transcribe_with_whisper, audio_path, language=caption_language,
                        on_error_return=None, log_prefix="Auto-edit captions: ", progress_cb=_progress,
                    )
 
                if caption_result and caption_result.get("text", "").strip() and caption_result.get("segments"):
                    srt_path = os.path.join(work_dir, "captions.srt")
                    whisper_segments_to_srt(caption_result["segments"], srt_path)
 
                    _set(job_id, progress=65, message="Burning in captions...")
                    captioned_path = os.path.join(work_dir, "_captioned.mp4")
                    caption_style = {**template.get("text_style", {}), "size": max(24, template.get("text_style", {}).get("size", 32) - 10)}
                    combined_path = burn_captions(combined_path, srt_path, captioned_path, style=caption_style)
 
                    final_srt = os.path.join(out_dir, "captions.srt")
                    import shutil as _shutil
                    _shutil.copy(srt_path, final_srt)
                    srt_rel_path = _rel(final_srt)
                else:
                    _set(job_id, message="No speech detected - skipping captions.")
            except Exception as e:
                _set(job_id, message=f"Caption generation skipped ({e}).")
 
        # ---- Step 6: background music (auto-ducking) ----
        if music_path and os.path.exists(music_path):
            _set(job_id, progress=75, message="Mixing background music...")
            ducked_path = os.path.join(work_dir, "_ducked.mp4")
            combined_path = duck_background_music(
                combined_path, music_path, ducked_path,
                ducking_level=template.get("audio_ducking_level", 0.25),
            )
 
        # ---- Step 7: multi-format export ----
        _set(job_id, progress=85, message=f"Exporting {len(output_formats)} format(s)...")
 
        def _export_progress(done, total, fmt):
            pct = 85 + int(done / total * 12)
            _set(job_id, progress=pct, message=f"Exporting {fmt} ({done}/{total})...")
 
        exported = export_multiple_formats(combined_path, output_formats, out_dir, progress_cb=_export_progress)
 
        if not exported:
            raise RuntimeError("No valid output formats were selected.")
 
        # ---- Step 8: package result ----
        _set(job_id, progress=98, message="Packaging result...")
        if len(exported) == 1:
            only_path = next(iter(exported.values()))
            final_path = os.path.join(out_dir, "auto_edited_video.mp4")
            os.replace(only_path, final_path)
            result_path = final_path
        else:
            zip_path = os.path.join(out_dir, "auto_edited_video_exports.zip")
            files_to_zip = list(exported.values())
            if srt_rel_path:
                files_to_zip.append(os.path.join(out_dir, "captions.srt"))
            zip_outputs(files_to_zip, zip_path)
            result_path = zip_path
 
        job = _set(
            job_id, status="success", progress=100,
            message=f"Done - {template.get('name', template_name)} template, "
                    f"{len(exported)} format(s)"
                    + (", highlight reel" if auto_highlights else "")
                    + (", captions burned in" if srt_rel_path else ""),
            result_path=_rel(result_path),
        )
        if job is not None:
            job.result_meta = {
                "template": template.get("name", template_name),
                "formats": list(exported.keys()),
                "subtitles_path": srt_rel_path,
                "auto_highlights": auto_highlights,
            }
            db.session.commit()
 
    except Exception as e:
        _set(job_id, status="failure", message=_friendly_error("Automated video editing failed", e))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@shared_task(name="tools.cleanup_old_jobs")
def cleanup_old_jobs(days: int = 7):
    """
    Periodic Celery beat task to clean up old job rows and files older than N days.
    Walks users/*/uploads/** and users/*/outputs/**, deletes files whose mtime is older than retention,
    removes empty directories, and deletes expired Job rows. Uses naive datetime.utcnow().
    """
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)
    storage_root = current_app.config["STORAGE_ROOT"]

    # Walk uploads and outputs and remove old files & empty dirs
    users_base = os.path.join(storage_root, "users")
    if os.path.exists(users_base):
        for user_folder in os.listdir(users_base):
            for sub in ["uploads", "outputs"]:
                sub_dir = os.path.join(users_base, user_folder, sub)
                if os.path.exists(sub_dir):
                    for root, dirs, files in os.walk(sub_dir, topdown=False):
                        for file in files:
                            fpath = os.path.join(root, file)
                            try:
                                mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
                                if mtime < cutoff:
                                    os.remove(fpath)
                            except Exception as e:
                                current_app.logger.warning(f"cleanup failed for {fpath}: {e}")
                        for d in dirs:
                            dpath = os.path.join(root, d)
                            try:
                                if not os.listdir(dpath):
                                    os.rmdir(dpath)
                            except Exception as e:
                                current_app.logger.warning(f"cleanup failed for {dpath}: {e}")

    # Delete expired Job rows in batches of 100 and only for status in (success, failure)
    deleted_count = 0
    while True:
        old_jobs = Job.query.filter(
            Job.created_at < cutoff,
            Job.status.in_(("success", "failure"))
        ).limit(100).all()
        if not old_jobs:
            break
        for job in old_jobs:
            try:
                db.session.delete(job)
                deleted_count += 1
            except Exception as e:
                current_app.logger.error(f"Failed to cleanup job {job.id}: {e}")
        db.session.commit()

    if deleted_count > 0:
        current_app.logger.info(f"Cleaned up {deleted_count} job rows and old files older than {days} days.")


@shared_task(name="tools.watchdog_stuck_jobs")
def watchdog_stuck_jobs():
    """
    Celery beat task to mark stuck jobs as failed:
    - pending jobs older than 2 hours (7200s) based on created_at.
    - running jobs older than task_time_limit + 300 seconds (2200s) based on updated_at.
    Uses naive datetime.utcnow().
    """
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    pending_cutoff = now - timedelta(hours=2)
    running_cutoff = now - timedelta(seconds=2200)

    stuck_pending = Job.query.filter(
        Job.status == "pending",
        Job.created_at < pending_cutoff
    ).all()

    stuck_running = Job.query.filter(
        Job.status == "running",
        Job.updated_at < running_cutoff
    ).all()

    count = 0
    for job in stuck_pending + stuck_running:
        job.status = "failure"
        job.message = "Job timed out or worker crashed"
        job.error_message = "Job timed out or worker crashed"
        count += 1

    if count > 0:
        db.session.commit()
        current_app.logger.warning(f"Watchdog marked {count} stuck job(s) as failed.")

 