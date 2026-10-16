import os
import wave
import shutil
import pytest
import cv2
import json
import subprocess
import numpy as np
from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import docx
from app import create_app
from extensions import db, limiter
import extensions
from models import User, Job
from unittest.mock import patch

has_ffmpeg = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
skip_no_ffmpeg = pytest.mark.skipif(not has_ffmpeg, reason="ffmpeg and ffprobe required for video generation and processing")

@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["RATELIMIT_ENABLED"] = False
    limiter.enabled = False
    app.config["CELERY_ALWAYS_EAGER"] = True
    app.config["CELERY_TASK_ALWAYS_EAGER"] = True
    app.config["CELERY_RESULT_BACKEND"] = "rpc://"

    if extensions.celery:
        extensions.celery.conf.update(
            task_always_eager=True,
            task_eager_propagates=True,
            result_backend="rpc://",
        )

    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()

@pytest.fixture
def client(app):
    return app.test_client()

@pytest.fixture
def auth_client(client, app):
    with app.app_context():
        user = User.query.filter_by(username="e2e_user").first()
        if not user:
            user = User(username="e2e_user", email="e2e@example.com")
            user.set_password("password123")
            db.session.add(user)
            db.session.commit()
        user_id = user.id

    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
    return client

@pytest.fixture
def tiny_fixtures(app):
    storage = app.config["STORAGE_ROOT"]
    os.makedirs(storage, exist_ok=True)
    
    # 1. WAV audio (1s)
    wav_path = os.path.join(storage, "test.wav")
    with wave.open(wav_path, "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(8000)
        f.writeframes(b"\x00" * 8000 * 2)

    # 2. PNG images (mixed RGBA and JPG: 2 PNGs with alpha, 1 JPG)
    png_paths = []
    for i in range(3):
        p = os.path.join(storage, f"test_{i}.png" if i < 2 else f"test_{i}.jpg")
        mode = "RGBA" if i < 2 else "RGB"
        color = (i * 80, 100, 200, 150) if i < 2 else (200, i * 60, 50)
        img = Image.new(mode, (64, 64), color=color)
        img.save(p)
        png_paths.append(p)

    # 3. PDF document (1-page real PDF with ReportLab containing real text)
    pdf_path = os.path.join(storage, "test.pdf")
    c = canvas.Canvas(pdf_path, pagesize=letter)
    c.drawString(100, 750, "Hello World from real PDF test fixture for SNPro System.")
    c.save()

    # 4. Text file
    txt_path = os.path.join(storage, "test.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("Hello PDF combination test text content.")

    # 5. MP4 video (2-second 128x128 with audio using ffmpeg lavfi)
    mp4_path = os.path.join(storage, "test.mp4")
    if has_ffmpeg:
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=128x128:rate=24",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p",
            mp4_path
        ], capture_output=True, check=True)
    else:
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(mp4_path, fourcc, 24, (128, 128))
        for _ in range(48):
            out.write(np.zeros((128, 128, 3), dtype=np.uint8))
        out.release()

    return {"wav": wav_path, "pngs": png_paths, "pdf": pdf_path, "txt": txt_path, "mp4": mp4_path}

def verify_job_success(client, app, validator=None):
    with app.app_context():
        job = Job.query.order_by(Job.id.desc()).first()
        assert job is not None, "No job created"
        assert job.status == "success", f"Job status is {job.status}, message: {job.message}"
        if job.result_path:
            full_result_path = os.path.join(app.config["STORAGE_ROOT"], job.result_path)
            assert os.path.exists(full_result_path), f"Result file {full_result_path} does not exist"
            assert os.path.getsize(full_result_path) > 0, f"Result file {full_result_path} is empty"
            if validator:
                validator(full_result_path)
        
        res = client.get(f"/tools/job/{job.id}/download")
        assert res.status_code == 200, f"Download route for job {job.id} failed with {res.status_code}"

def test_qr_code(auth_client, app):
    res = auth_client.post("/tools/qr-code", data={"text": "https://example.com"}, follow_redirects=True)
    assert res.status_code == 200
    verify_job_success(auth_client, app, lambda p: assert_image_valid(p))

def assert_image_valid(path):
    img = Image.open(path)
    assert img.size[0] > 0 and img.size[1] > 0

@patch("gtts.gTTS.save")
def test_text_to_speech(mock_gtts_save, auth_client, app):
    def fake_save(out_path):
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with wave.open(out_path, "w") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(8000)
            f.writeframes(b"\x00" * 8000)
    mock_gtts_save.side_effect = fake_save

    res = auth_client.post("/tools/text-to-speech", data={"text": "Hello test", "lang": "en"}, follow_redirects=True)
    assert res.status_code == 200
    verify_job_success(auth_client, app)

@patch("app.tools.tasks.transcribe_with_whisper")
def test_speech_to_text(mock_transcribe, auth_client, tiny_fixtures, app):
    mock_transcribe.return_value = {"text": "Hello world", "segments": [{"start": 0, "end": 1, "text": "Hello world"}]}
    with patch("speech_recognition.Recognizer.recognize_google", return_value="Hello world"):
        with open(tiny_fixtures["wav"], "rb") as f:
            res = auth_client.post("/tools/speech-to-text", data={"audio": (f, "test.wav")}, content_type="multipart/form-data", follow_redirects=True)
    assert res.status_code == 200
    verify_job_success(auth_client, app)

@skip_no_ffmpeg
def test_video_to_frames(auth_client, tiny_fixtures, app):
    with open(tiny_fixtures["mp4"], "rb") as f:
        res = auth_client.post("/tools/video-to-frames", data={"video": (f, "test.mp4"), "fps": "1"}, content_type="multipart/form-data", follow_redirects=True)
    assert res.status_code == 200
    verify_job_success(auth_client, app, lambda p: assert_zip_valid(p))

def assert_zip_valid(path):
    import zipfile
    assert zipfile.is_zipfile(path)
    with zipfile.ZipFile(path, 'r') as z:
        namelist = z.namelist()
        assert len(namelist) > 0

@pytest.mark.slow
def test_pdf_to_word(auth_client, tiny_fixtures, app):
    with open(tiny_fixtures["pdf"], "rb") as f:
        res = auth_client.post("/tools/pdf-to-word", data={"pdf": (f, "test.pdf")}, content_type="multipart/form-data", follow_redirects=True)
    assert res.status_code == 200
    def validate_docx(path):
        doc = docx.Document(path)
        text = "".join([p.text for p in doc.paragraphs])
        assert "Hello World from real PDF test fixture" in text, f"Expected text not found in docx: {text}"
    verify_job_success(auth_client, app, validate_docx)

def test_photo_repair(auth_client, tiny_fixtures, app):
    with open(tiny_fixtures["pngs"][0], "rb") as f:
        res = auth_client.post("/tools/photo-repair", data={"image": (f, "test_0.png"), "denoise": "on", "sharpen": "light"}, content_type="multipart/form-data", follow_redirects=True)
    assert res.status_code == 200
    verify_job_success(auth_client, app, lambda p: assert_image_valid(p))

@pytest.mark.slow
@skip_no_ffmpeg
def test_audio_remover(auth_client, tiny_fixtures, app):
    # Remove mode: assert NO audio stream in output
    with open(tiny_fixtures["mp4"], "rb") as f:
        res = auth_client.post("/tools/audio-remover", data={"video": (f, "test.mp4"), "mode": "remove"}, content_type="multipart/form-data", follow_redirects=True)
    assert res.status_code == 200
    def validate_no_audio(path):
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", path], capture_output=True, text=True, check=True)
        data = json.loads(probe.stdout)
        audio_streams = [s for s in data.get("streams", []) if s["codec_type"] == "audio"]
        assert len(audio_streams) == 0, "Audio stream found in remove mode output"
    verify_job_success(auth_client, app, validate_no_audio)

    # Replace mode: assert audio stream exists in output
    with open(tiny_fixtures["mp4"], "rb") as f, open(tiny_fixtures["wav"], "rb") as af:
        res = auth_client.post("/tools/audio-remover", data={"video": (f, "test.mp4"), "audio": (af, "test.wav"), "mode": "replace"}, content_type="multipart/form-data", follow_redirects=True)
    assert res.status_code == 200
    def validate_has_audio(path):
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", path], capture_output=True, text=True, check=True)
        data = json.loads(probe.stdout)
        audio_streams = [s for s in data.get("streams", []) if s["codec_type"] == "audio"]
        assert len(audio_streams) > 0, "No audio stream found in replace mode output"
    verify_job_success(auth_client, app, validate_has_audio)

@pytest.mark.slow
@skip_no_ffmpeg
@pytest.mark.parametrize("fps", [1, 12, 24, 30])
def test_images_to_video(auth_client, tiny_fixtures, app, fps):
    # Regression test parametrized over fps and mixed RGBA PNG + JPG inputs
    with open(tiny_fixtures["pngs"][0], "rb") as f1, open(tiny_fixtures["pngs"][1], "rb") as f2, open(tiny_fixtures["pngs"][2], "rb") as f3:
        res = auth_client.post("/tools/images-to-video", data={"images": [(f1, "img0.png"), (f2, "img1.png"), (f3, "img2.jpg")], "fps": str(fps)}, content_type="multipart/form-data", follow_redirects=True)
    assert res.status_code == 200
    def validate_mp4(path):
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,duration,nb_frames", "-of", "json", path], capture_output=True, text=True, check=True)
        data = json.loads(probe.stdout)
        stream = data["streams"][0]
        assert int(stream["width"]) > 0
        assert int(stream["height"]) > 0
        assert float(stream.get("duration", 1)) > 0
    verify_job_success(auth_client, app, validate_mp4)

@pytest.mark.slow
@skip_no_ffmpeg
def test_video_enhancer(auth_client, tiny_fixtures, app):
    with open(tiny_fixtures["mp4"], "rb") as f:
        res = auth_client.post("/tools/video-enhancer", data={"video": (f, "test.mp4"), "target_resolution": "original", "denoise": "off", "sharpen": "light"}, content_type="multipart/form-data", follow_redirects=True)
    assert res.status_code == 200
    def validate_mp4(path):
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,duration", "-of", "json", path], capture_output=True, text=True, check=True)
        data = json.loads(probe.stdout)
        stream = data["streams"][0]
        assert int(stream["width"]) > 0
        assert int(stream["height"]) > 0
        assert float(stream.get("duration", 1)) > 0
    verify_job_success(auth_client, app, validate_mp4)

def test_combine_pdf(auth_client, tiny_fixtures, app):
    with open(tiny_fixtures["pdf"], "rb") as f1, open(tiny_fixtures["pngs"][0], "rb") as f2, open(tiny_fixtures["txt"], "rb") as f3:
        res = auth_client.post(
            "/tools/combine-pdf",
            data={
                "files": [(f1, "test.pdf"), (f2, "test.png"), (f3, "test.txt")],
                "page_size": "A4",
                "orientation": "portrait"
            },
            content_type="multipart/form-data",
            follow_redirects=True
        )
    assert res.status_code == 200
    verify_job_success(auth_client, app)

@pytest.mark.slow
@skip_no_ffmpeg
def test_auto_edit_video(auth_client, tiny_fixtures, app):
    with open(tiny_fixtures["mp4"], "rb") as f:
        res = auth_client.post("/tools/auto-edit-video", data={"clips": (f, "test.mp4"), "template": "social_short"}, content_type="multipart/form-data", follow_redirects=True)
    assert res.status_code == 200
    def validate_mp4(path):
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,duration", "-of", "json", path], capture_output=True, text=True, check=True)
        data = json.loads(probe.stdout)
        stream = data["streams"][0]
        assert int(stream["width"]) > 0
        assert int(stream["height"]) > 0
        assert float(stream.get("duration", 1)) > 0
    verify_job_success(auth_client, app, validate_mp4)

# Real local test for background remover (rembg)
def test_background_remover_real(auth_client, tiny_fixtures, app):
    try:
        from rembg import new_session
        new_session("u2netp")
    except Exception:
        pytest.skip("model not downloaded")

    with open(tiny_fixtures["pngs"][0], "rb") as f:
        res = auth_client.post("/tools/background-remover", data={"image": (f, "test_0.png")}, content_type="multipart/form-data", follow_redirects=True)
    assert res.status_code == 200
    verify_job_success(auth_client, app, lambda p: assert_image_valid(p))

# Real local test for background replacer (rembg)
def test_background_replacer_real(auth_client, tiny_fixtures, app):
    try:
        from rembg import new_session
        new_session("u2netp")
    except Exception:
        pytest.skip("model not downloaded")

    with open(tiny_fixtures["pngs"][0], "rb") as f1, open(tiny_fixtures["pngs"][1], "rb") as f2:
        res = auth_client.post("/tools/background-replacer", data={"subject": (f1, "subj.png"), "background": (f2, "bg.png"), "light_direction": "left"}, content_type="multipart/form-data", follow_redirects=True)
    assert res.status_code == 200
    verify_job_success(auth_client, app, lambda p: assert_image_valid(p))

# Negative tests
def test_negative_wrong_extension(auth_client, tiny_fixtures):
    with open(tiny_fixtures["mp4"], "rb") as f:
        res = auth_client.post("/tools/pdf-to-word", data={"pdf": (f, "test.mp4")}, content_type="multipart/form-data", follow_redirects=True)
    assert res.status_code == 200

def test_negative_empty_upload(auth_client):
    res = auth_client.post("/tools/pdf-to-word", data={}, content_type="multipart/form-data", follow_redirects=True)
    assert res.status_code == 200

def test_exceeding_active_jobs(auth_client, tiny_fixtures, app):
    with app.app_context():
        app.config["MAX_ACTIVE_JOBS_PER_USER"] = 1
        user = User.query.filter_by(username="e2e_user").first()
        job = Job(user_id=user.id, tool="qr_code", status="pending")
        db.session.add(job)
        db.session.commit()

    res = auth_client.post("/tools/qr-code", data={"text": "exceed"}, follow_redirects=True)
    assert res.status_code == 200

# Mocked network/API tools testing (patching apply_async on task)
@patch("app.tools.routes.tasks.task_download_video.apply_async")
def test_video_downloader_mock(mock_apply_async, auth_client):
    mock_apply_async.return_value.id = "mock-task-id"
    res = auth_client.post("/tools/video-downloader", data={"url": "https://youtube.com/watch?v=xyz", "mode": "mp4"}, follow_redirects=True)
    assert res.status_code == 200
    assert mock_apply_async.called

@patch("app.tools.routes.tasks.task_replace_background.apply_async")
def test_background_replacer_mock(mock_apply_async, auth_client, tiny_fixtures):
    mock_apply_async.return_value.id = "mock-task-id"
    with open(tiny_fixtures["pngs"][0], "rb") as f1, open(tiny_fixtures["pngs"][1], "rb") as f2:
        res = auth_client.post("/tools/background-replacer", data={"subject": (f1, "subj.png"), "background": (f2, "bg.png")}, content_type="multipart/form-data", follow_redirects=True)
    assert res.status_code == 200
    assert mock_apply_async.called

@patch("app.tools.tasks.translate_speech_task.apply_async")
def test_speech_translator_mock(mock_apply_async, auth_client, tiny_fixtures):
    mock_apply_async.return_value.id = "mock-task-id"
    with open(tiny_fixtures["wav"], "rb") as f:
        res = auth_client.post("/tools/speech-translator", data={"audio_file": (f, "test.wav"), "target_lang": "es"}, content_type="multipart/form-data", follow_redirects=True)
    assert res.status_code == 200
    assert mock_apply_async.called

@patch("app.tools.routes.tasks.task_translate_video_speech.apply_async")
def test_video_speech_translator_mock(mock_apply_async, auth_client, tiny_fixtures):
    mock_apply_async.return_value.id = "mock-task-id"
    with open(tiny_fixtures["mp4"], "rb") as f:
        res = auth_client.post("/tools/video-speech-translator", data={"video": (f, "test.mp4"), "target_lang": "es"}, content_type="multipart/form-data", follow_redirects=True)
    assert res.status_code == 200


def test_job_events_streaming_without_persistent_client_context(client, auth_client, app):
    with app.app_context():
        user = User.query.filter_by(username="e2e_user").first()
        job = Job(user_id=user.id, tool="qr_code", status="pending", progress=0)
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    res = auth_client.get(f"/tools/job/{job_id}/events", buffered=False)
    assert res.status_code == 200
    data = next(res.iter_encoded(), None)
    assert data is not None


def test_sse_slot_release_on_early_close(client, auth_client, app):
    from app.tools.routes import _ACTIVE_SSE_STREAMS
    initial_streams = _ACTIVE_SSE_STREAMS

    with app.app_context():
        user = User.query.filter_by(username="e2e_user").first()
        job = Job(user_id=user.id, tool="qr_code", status="pending", progress=0)
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    res = auth_client.get(f"/tools/job/{job_id}/events", buffered=False)
    assert res.status_code == 200
    res.close()

    from app.tools.routes import _ACTIVE_SSE_STREAMS
    assert _ACTIVE_SSE_STREAMS == initial_streams




