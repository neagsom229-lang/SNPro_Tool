"""
app/tools/speech_helpers.py

Helper functions for the Speech-to-Speech Translator tool.

Pipeline:
    audio -> transcribe_audio (Whisper) -> text
    text -> translate_text() -> translated text
    translated text -> synthesize_speech() -> mp3
"""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

GOOGLE_TRANSLATE_API_KEY = os.environ.get("GOOGLE_TRANSLATE_API_KEY")
DEEPL_API_KEY = os.environ.get("DEEPL_API_KEY")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_DEFAULT_VOICE_ID = os.environ.get("ELEVENLABS_DEFAULT_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

# Full language menu exposed in the UI. code -> display name.
SUPPORTED_LANGUAGES = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "km": "Khmer",
    "zh": "Chinese (Mandarin)",
    "ja": "Japanese",
    "ar": "Arabic",
    "ru": "Russian",
    "hi": "Hindi",
    "pt": "Portuguese",
    "it": "Italian",
    "ko": "Korean",
    "vi": "Vietnamese",
    "th": "Thai",
    "id": "Indonesian",
}

ALLOWED_AUDIO_EXTENSIONS = {"mp3", "wav", "m4a", "ogg", "flac"}
MAX_UPLOAD_MB = 50
MAX_TRANSLATE_CHARS = 5000  # guard against accidentally billing/blocking on runaway input

@dataclass
class TranslationResult:
    source_text: str
    translated_text: str
    detected_source_lang: Optional[str]
    engine_used: str

# --------------------------------------------------------------------------
# Translation cache
# --------------------------------------------------------------------------
# Repeated translation requests are common in practice (a user re-running a
# job after tweaking an unrelated option, or a batch containing duplicate
# clips) and every engine here is a paid/rate-limited API call. A small
# bounded cache avoids paying for - and waiting on - the same request twice
# within a worker process's lifetime, with no persistence/eviction policy
# needed beyond a simple size cap (this is a latency/cost optimization, not
# a correctness-critical cache, so "good enough" beats "perfectly precise").
_TRANSLATION_CACHE = {}
_TRANSLATION_CACHE_MAX_SIZE = 256


def _translation_cache_key(text, target_lang, source_lang):
    return (text, target_lang, source_lang or "auto")


def _translation_cache_get(text, target_lang, source_lang):
    return _TRANSLATION_CACHE.get(_translation_cache_key(text, target_lang, source_lang))


def _translation_cache_set(text, target_lang, source_lang, result):
    if len(_TRANSLATION_CACHE) >= _TRANSLATION_CACHE_MAX_SIZE:
        # Evict an arbitrary (oldest-inserted, in practice, on CPython)
        # entry rather than tracking real LRU order - simplicity over
        # precision for a cache whose only job is cutting duplicate calls.
        _TRANSLATION_CACHE.pop(next(iter(_TRANSLATION_CACHE)))
    _TRANSLATION_CACHE[_translation_cache_key(text, target_lang, source_lang)] = result

# --------------------------------------------------------------------------
# 1. Transcription – Whisper (Consolidated with shared ai_helpers cache)
# --------------------------------------------------------------------------

def transcribe_audio(audio_path: str, lang_code: Optional[str] = None) -> dict:
    """
    Transcribe an audio file using Whisper (open-source, 99 languages).
    Consolidated to use ai_helpers.transcribe_with_whisper and the shared model cache.
    Returns dict with keys: text, detected_lang, duration_seconds.
    """
    import subprocess

    duration_seconds = 0.0
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            audio_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
        duration_seconds = float(res.stdout.strip())
    except Exception:
        logger.warning("ffprobe failed to get duration for %s, falling back to librosa", audio_path, exc_info=True)
        try:
            import librosa
            audio, sr = librosa.load(audio_path, sr=16000, mono=True)
            duration_seconds = len(audio) / sr
        except Exception:
            logger.exception("librosa fallback also failed for %s", audio_path)
            duration_seconds = 0.0

    from app.tools.ai_helpers import transcribe_with_whisper
    res = transcribe_with_whisper(audio_path, language=lang_code, duration_hint=duration_seconds)
    text = res.get("text", "").strip()
    detected_lang = res.get("detected_lang", lang_code or "unknown")

    logger.info("Transcribed %.1fs of audio -> %d chars (lang=%s)", duration_seconds, len(text), detected_lang)

    return {
        "text": text,
        "detected_lang": detected_lang,
        "duration_seconds": duration_seconds,
    }

# --------------------------------------------------------------------------
# 2. Translation (Google → DeepL → NLLB-200)
# --------------------------------------------------------------------------

def _translate_with_google(text: str, target_lang: str, source_lang: Optional[str]) -> str:
    """
    Free translation via deep-translator.
    Google first (4000-char chunks); falls to MyMemory on rate-limit,
    re-chunked to 450 chars to respect MyMemory's 500-char limit.
    """
    import time
    from deep_translator import GoogleTranslator, MyMemoryTranslator
    from deep_translator.exceptions import TooManyRequests

    tgt = (target_lang or "en").lower()
    src = (source_lang or "auto").lower()

    mymemory_map = {
        "en": "en-GB", "km": "km-KH", "zh": "zh-CN", "ja": "ja-JP",
        "ko": "ko-KR", "fr": "fr-FR", "es": "es-ES", "de": "de-DE",
        "th": "th-TH", "vi": "vi-VN", "ru": "ru-RU", "hi": "hi-IN",
        "ar": "ar-SA", "pt": "pt-PT", "it": "it-IT", "id": "id-ID",
    }
    mm_src = mymemory_map.get(src, "en-GB")
    mm_tgt = mymemory_map.get(tgt, "en-GB")

    # Track whether Google is dead for this task — one failure = skip for all
    # remaining chunks, saves ~30s per chunk when we're IP-blocked.
    google_dead = [False]

    def translate_google(chunk: str):
        """Return translated chunk via Google, or None on failure."""
        if google_dead[0]:
            return None
        for attempt in range(2):  # 2 tries, then mark dead
            try:
                return GoogleTranslator(source=src, target=tgt).translate(chunk)
            except TooManyRequests:
                if attempt == 0:
                    logger.warning("Google rate limit — one short retry")
                    time.sleep(5)
                else:
                    logger.warning("Google dead for this task — using MyMemory only")
                    google_dead[0] = True
            except Exception:
                logger.exception("Google chunk failed — using MyMemory only")
                google_dead[0] = True
                return None
        return None

    def translate_mymemory(chunk: str) -> str:
        """Translate via MyMemory. Chunk must be ≤ 500 chars."""
        # MyMemory caps at 500 chars — split further if needed.
        parts = [chunk[i:i + 450] for i in range(0, len(chunk), 450)]
        out = []
        for part in parts:
            try:
                out.append(MyMemoryTranslator(source=mm_src, target=mm_tgt).translate(part))
                time.sleep(0.3)
            except Exception:
                logger.exception("MyMemory chunk failed — keeping original text")
                out.append(part)
        return " ".join(out)

    # Google chunks are 4000 chars; MyMemory sub-chunks internally.
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]

    out_parts = []
    for i, chunk in enumerate(chunks):
        result = translate_google(chunk)
        if result is None:
            result = translate_mymemory(chunk)
        out_parts.append(result)
        logger.info("Translated chunk %d/%d", i + 1, len(chunks))
        if i < len(chunks) - 1:
            time.sleep(0.5)

    return " ".join(out_parts)

def _translate_with_deepl(text: str, target_lang: str, source_lang: Optional[str]) -> str:
    import deepl  # type: ignore

    # DeepL doesn't support Khmer. Raise early so translate_text() skips it.
    if (target_lang or "").lower() in ("km", "khmer"):
        raise ValueError("DeepL does not support Khmer")

    translator = deepl.Translator(DEEPL_API_KEY)
    result = translator.translate_text(
        text,
        target_lang=target_lang.upper(),
        source_lang=source_lang.upper() if source_lang else None,
    )
    return result.text

_nllb_model = None
_nllb_tokenizer = None
_NLLB_LANG_MAP = {
    "en": "eng_Latn", "es": "spa_Latn", "fr": "fra_Latn", "de": "deu_Latn",
    "km": "khm_Khmr", "zh": "zho_Hans", "ja": "jpn_Jpan", "ar": "arb_Arab",
    "ru": "rus_Cyrl", "hi": "hin_Deva", "pt": "por_Latn", "it": "ita_Latn",
    "ko": "kor_Hang", "vi": "vie_Latn", "th": "tha_Thai", "id": "ind_Latn",
}

def _translate_with_nllb(text: str, target_lang: str, source_lang: Optional[str]) -> str:
    global _nllb_model, _nllb_tokenizer
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer # type: ignore
    import torch

    if _nllb_model is None:
        model_name = "facebook/nllb-200-distilled-600M"
        logger.info("Loading NLLB-200 fallback translation model (%s)", model_name)
        _nllb_tokenizer = AutoTokenizer.from_pretrained(model_name)
        _nllb_model = AutoModelForSeq2SeqLM.from_pretrained(model_name)

    src_code = _NLLB_LANG_MAP.get(source_lang or "en", "eng_Latn")
    tgt_code = _NLLB_LANG_MAP.get(target_lang, "eng_Latn")

    _nllb_tokenizer.src_lang = src_code
    encoded = _nllb_tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
    forced_bos_token_id = _nllb_tokenizer.convert_tokens_to_ids(tgt_code)

    generated = _nllb_model.generate(**encoded, forced_bos_token_id=forced_bos_token_id, max_new_tokens=512)
    return _nllb_tokenizer.batch_decode(generated, skip_special_tokens=True)[0]

def translate_text(text: str, target_lang: str, source_lang: Optional[str] = None) -> TranslationResult:
    if not text.strip():
        return TranslationResult(text, "", source_lang, "none")

    if len(text) > MAX_TRANSLATE_CHARS:
        raise ValueError(
            f"Text is {len(text)} characters, which exceeds the {MAX_TRANSLATE_CHARS}-character "
            f"limit for a single translation request. Split it into smaller chunks."
        )

    cached = _translation_cache_get(text, target_lang, source_lang)
    if cached is not None:
        logger.info("Translation cache hit (%d chars -> %s)", len(text), target_lang)
        return cached

    if GOOGLE_TRANSLATE_API_KEY:
        try:
            translated = _translate_with_google(text, target_lang, source_lang)
            result = TranslationResult(text, translated, source_lang, "google")
            _translation_cache_set(text, target_lang, source_lang, result)
            return result
        except Exception:
            logger.exception("Google Translate failed, trying next engine")

    if DEEPL_API_KEY:
        try:
            translated = _translate_with_deepl(text, target_lang, source_lang)
            result = TranslationResult(text, translated, source_lang, "deepl")
            _translation_cache_set(text, target_lang, source_lang, result)
            return result
        except Exception:
            logger.exception("DeepL failed, falling back to NLLB-200")

    translated = _translate_with_nllb(text, target_lang, source_lang)
    result = TranslationResult(text, translated, source_lang, "nllb-200")
    _translation_cache_set(text, target_lang, source_lang, result)
    return result

# --------------------------------------------------------------------------
# 3. Speech synthesis (ElevenLabs → gTTS)
# --------------------------------------------------------------------------

def synthesize_with_elevenlabs(text: str, output_path: str, voice_id: Optional[str] = None) -> str:
    import requests
    voice_id = voice_id or ELEVENLABS_DEFAULT_VOICE_ID
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    response = requests.post(url, json=payload, headers=headers, timeout=120)
    response.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(response.content)
    return output_path

def _synthesize_with_gtts(text: str, lang_code: str, output_path: str) -> str:
    """gTTS with retries — its underlying HTTP calls have no timeout by default."""
    from gtts import gTTS
    import time

    last_err = None
    for attempt in range(3):
        try:
            tts = gTTS(text=text, lang=lang_code, slow=False, timeout=30)
            tts.save(output_path)
            return output_path
        except Exception as e:
            last_err = e
            logger.warning("gTTS attempt %d failed: %s", attempt + 1, e)
            time.sleep(3)
    raise RuntimeError(f"gTTS failed after 3 attempts: {last_err}")

def synthesize_speech(
    text: str,
    lang_code: str,
    output_path: str,
    elevenlabs_voice_id: Optional[str] = None,
) -> dict:
    """Top-level synthesis: ElevenLabs if key present, else gTTS."""
    engine_used = None
    try:
        if ELEVENLABS_API_KEY:
            synthesize_with_elevenlabs(text, output_path, voice_id=elevenlabs_voice_id)
            engine_used = "elevenlabs"
        else:
            _synthesize_with_gtts(text, lang_code, output_path)
            engine_used = "gtts"
    except Exception as e:
        logger.exception("Primary TTS failed, falling back to gTTS: %s", e)
        _synthesize_with_gtts(text, lang_code, output_path)
        engine_used = "gtts-fallback"

    return {"output_path": output_path, "engine_used": engine_used}

def allowed_audio_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_AUDIO_EXTENSIONS