import base64
import logging
from pathlib import Path

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, PictureType
from mutagen.mp4 import MP4, MP4Cover

log = logging.getLogger(__name__)


def read_artwork(path: Path) -> tuple[bytes | None, str | None]:
    """Read embedded artwork, preferring a front cover when one is identified."""
    audio = mutagen.File(path)
    if audio is None:
        return None, None
    tags = audio.tags or {}

    if isinstance(audio.tags, ID3):
        pictures = audio.tags.getall("APIC")
    elif isinstance(audio, FLAC):
        pictures = audio.pictures
    elif isinstance(audio, MP4):
        for cover in tags.get("covr", []):
            if cover:
                mime = {
                    MP4Cover.FORMAT_JPEG: "image/jpeg",
                    MP4Cover.FORMAT_PNG: "image/png",
                }.get(cover.imageformat, "application/octet-stream")
                return bytes(cover), mime
        return None, None
    else:
        # Ogg Vorbis and Opus store base64-encoded FLAC picture blocks.
        pictures = []
        for value in tags.get("metadata_block_picture", []):
            try:
                pictures.append(Picture(base64.b64decode(value, validate=True)))
            except (ValueError, mutagen.MutagenError) as exc:
                log.warning("ignoring invalid artwork in %s: %s", path, exc)

    for picture in sorted(pictures, key=lambda p: p.type != PictureType.COVER_FRONT):
        if picture.data and picture.mime != "-->":  # Skip linked artwork.
            return bytes(picture.data), picture.mime or "application/octet-stream"
    return None, None
