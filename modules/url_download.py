# These functions are for general-purpose URL archiving of video and video-like media, allowing audio-only

import uuid
from pathlib import Path

import yt_dlp

OUTPUT_ROOT = Path("output")

# format selectors per target extension
_FORMATS = {
    "mp4": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
    "mp3": "bestaudio/best",   # extracted to mp3 via postprocessor below
    "m4a": "bestaudio[ext=m4a]/bestaudio/best",
    "webm": "bestvideo[ext=webm]+bestaudio/best[ext=webm]/best",
}


def _make_output_dir(name: str) -> Path:
    """Create and return a per-function subfolder under /output. Reusable."""
    path = OUTPUT_ROOT / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def download(url: str, ext: str = "mp4") -> dict:
    """Download a video/audio from `url` in the given `ext` (default mp4)."""
    ext = ext.lower().lstrip(".")
    if ext not in _FORMATS:
        raise ValueError(f"Unsupported ext '{ext}'. Choose from {list(_FORMATS)}")

    out_dir = _make_output_dir("url_download")

    options = {
        "format": _FORMATS[ext],
        "outtmpl": str(out_dir / f"{uuid.uuid4().hex}_%(title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
    }

    # audio-only targets need extraction/conversion (requires ffmpeg installed)
    if ext in {"mp3"}:
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": ext,
        }]

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

    return {
        "title": info.get("title"),
        "output_dir": str(out_dir),
        "ext": ext,
    }