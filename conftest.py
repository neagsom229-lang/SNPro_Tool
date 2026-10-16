import pytest
import shutil
import os

def pytest_addoption(parser):
    parser.addoption(
        "--require-ffmpeg",
        action="store_true",
        default=False,
        help="Fail tests instead of skipping if ffmpeg/ffprobe is missing"
    )

def pytest_configure(config):
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )

def pytest_runtest_setup(item):
    has_ffmpeg = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
    require_ffmpeg = item.config.getoption("--require-ffmpeg") or os.environ.get("REQUIRE_FFMPEG") == "1"
    
    for mark in item.iter_markers():
        if mark.name == "skipif" and any("ffmpeg" in str(arg).lower() for arg in mark.args):
            if not has_ffmpeg:
                if require_ffmpeg:
                    pytest.fail("FFmpeg / ffprobe is missing but required by --require-ffmpeg / REQUIRE_FFMPEG=1")
