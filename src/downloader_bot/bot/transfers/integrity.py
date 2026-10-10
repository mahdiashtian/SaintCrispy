"""Validate received media before finalizing a Telegram upload."""

import math
from contextlib import aclosing
from dataclasses import dataclass

from downloader_bot.bot.transfers.video import MAX_HEADER_BYTES, boxes, child
from downloader_bot.schemas.media import DownloadError


def require_duration(actual: float | None, expected: float | None) -> None:
    if expected and (
        actual is None or not math.isfinite(actual) or actual < expected - max(1, expected * 0.005)
    ):
        raise DownloadError(
            "دریافت جریان رسانه کامل نشد؛ فایل ناقص ارسال نمی‌شود.",
            code="media_stream_incomplete",
        )


@dataclass
class Track:
    kind: bytes
    timescale: int
    duration: float
    default_duration: int = 0
    default_size: int = 0
    fragment_ticks: int = 0


class MP4Integrity:
    """Read only moov/moof metadata; discard mdat bytes as they pass through.

    Fragment durations are summed per track. A long audio track cannot conceal a
    video ending after its first segment, and a large timestamp cannot conceal gaps.
    """

    def __init__(self):
        self.tracks: dict[int, Track] = {}
        self._header = bytearray()
        self._body = bytearray()
        self._kind = b""
        self._remaining = 0
        self._open_ended = False
        self._sample_bytes = 0
        self._mdat_bytes = 0
        self._fragmented = False

    @staticmethod
    def _invalid():
        raise DownloadError(
            "ساختار فایل رسانه کامل نیست؛ فایل ناقص ارسال نمی‌شود.",
            code="media_stream_incomplete",
        )

    def feed(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            if not self._kind:
                needed = 16 if len(self._header) >= 8 and self._header[:4] == b"\0\0\0\1" else 8
                count = min(needed - len(self._header), len(data) - offset)
                self._header.extend(data[offset : offset + count])
                offset += count
                if len(self._header) < needed:
                    continue
                size = int.from_bytes(self._header[:4], "big")
                if size == 1 and needed == 8:
                    continue
                self._kind = bytes(self._header[4:8])
                if size == 1:
                    size = int.from_bytes(self._header[8:16], "big")
                self._open_ended = size == 0
                if size and size < needed:
                    self._invalid()
                self._remaining = size - needed if size else 0
                if self._kind in {b"moov", b"moof"} and (
                    self._open_ended or self._remaining > MAX_HEADER_BYTES
                ):
                    self._invalid()
                self._header.clear()
            count = len(data) - offset
            if not self._open_ended:
                count = min(count, self._remaining)
            if self._kind in {b"moov", b"moof"}:
                self._body.extend(data[offset : offset + count])
            elif self._kind == b"mdat":
                self._mdat_bytes += count
                self._sample_bytes = max(0, self._sample_bytes - count)
            offset += count
            self._remaining -= count
            if not self._open_ended and self._remaining == 0:
                self._box_complete()

    def _box_complete(self):
        if self._kind == b"moov":
            self._moov(bytes(self._body))
        elif self._kind == b"moof":
            if self._sample_bytes:
                self._invalid()
            self._fragmented = True
            for kind, body, _ in boxes(self._body):
                if kind == b"traf":
                    self._traf(body)
        self._body.clear()
        self._kind = b""

    def _moov(self, data):
        for kind, track, _ in boxes(data):
            if kind != b"trak":
                continue
            tkhd = child(track, b"tkhd")
            mdia = child(track, b"mdia")
            mdhd = child(mdia, b"mdhd")
            if len(tkhd) < 24 or len(mdhd) < 24:
                continue
            identity_start = 20 if tkhd[0] == 1 else 12
            identity = int.from_bytes(tkhd[identity_start : identity_start + 4], "big")
            start, length = (20, 8) if mdhd[0] == 1 else (12, 4)
            scale = int.from_bytes(mdhd[start : start + 4], "big")
            ticks = int.from_bytes(mdhd[start + 4 : start + 4 + length], "big")
            if not scale:
                continue
            stts = child(child(child(mdia, b"minf"), b"stbl"), b"stts")
            if len(stts) >= 8:
                entries = int.from_bytes(stts[4:8], "big")
                if 8 + entries * 8 > len(stts):
                    self._invalid()
                ticks = sum(
                    int.from_bytes(stts[i : i + 4], "big")
                    * int.from_bytes(stts[i + 4 : i + 8], "big")
                    for i in range(8, 8 + entries * 8, 8)
                )
            if ticks == (1 << (length * 8)) - 1:
                ticks = 0
            self.tracks[identity] = Track(child(mdia, b"hdlr")[8:12], scale, ticks / scale)
        for kind, body, _ in boxes(child(data, b"mvex")):
            if kind == b"trex" and len(body) >= 24:
                track = self.tracks.get(int.from_bytes(body[4:8], "big"))
                if track:
                    track.default_duration = int.from_bytes(body[12:16], "big")
                    track.default_size = int.from_bytes(body[16:20], "big")

    def _traf(self, data):
        tfhd = child(data, b"tfhd")
        if len(tfhd) < 8:
            self._invalid()
        track = self.tracks.get(int.from_bytes(tfhd[4:8], "big"))
        if track is None:
            self._invalid()
        flags = int.from_bytes(tfhd[1:4], "big")
        offset = 8 + (8 if flags & 1 else 0) + (4 if flags & 2 else 0)
        duration, size = track.default_duration, track.default_size
        for flag in (8, 16, 32):
            if flags & flag:
                if offset + 4 > len(tfhd):
                    self._invalid()
                value = int.from_bytes(tfhd[offset : offset + 4], "big")
                if flag == 8:
                    duration = value
                elif flag == 16:
                    size = value
                offset += 4
        for kind, run, _ in boxes(data):
            if kind != b"trun":
                continue
            if len(run) < 8:
                self._invalid()
            flags = int.from_bytes(run[1:4], "big")
            count = int.from_bytes(run[4:8], "big")
            offset = 8 + (4 if flags & 1 else 0) + (4 if flags & 4 else 0)
            fields = [flag for flag in (256, 512, 1024, 2048) if flags & flag]
            if offset + count * len(fields) * 4 > len(run):
                self._invalid()
            if not fields:
                track.fragment_ticks += count * duration
                self._sample_bytes += count * size
                continue
            if count > MAX_HEADER_BYTES // 4:
                self._invalid()
            for _ in range(count):
                sample_duration, sample_size = duration, size
                for flag in fields:
                    value = int.from_bytes(run[offset : offset + 4], "big")
                    if flag == 256:
                        sample_duration = value
                    elif flag == 512:
                        sample_size = value
                    offset += 4
                track.fragment_ticks += sample_duration
                self._sample_bytes += sample_size

    def finish(
        self, expected: float | None, require_audio: bool = False, *, audio_only: bool = False
    ) -> float:
        if (
            self._header
            or (self._kind and not self._open_ended)
            or self._sample_bytes
            or not self._mdat_bytes
        ):
            self._invalid()
        durations = {
            track.kind: track.duration + track.fragment_ticks / track.timescale
            if self._fragmented
            else track.duration
            for track in self.tracks.values()
            if track.kind in {b"vide", b"soun"}
        }
        primary = b"soun" if audio_only else b"vide"
        if primary not in durations or (require_audio and b"soun" not in durations):
            self._invalid()
        for kind, duration in durations.items():
            if kind == primary or require_audio:
                require_duration(duration, expected)
        return durations[primary]


async def validated_mp4(chunks, progress, expected, require_audio=False, *, audio_only=False):
    validator = MP4Integrity()
    async with aclosing(chunks):
        async for chunk in chunks:
            validator.feed(chunk)
            yield chunk
        progress.seconds = validator.finish(expected, require_audio, audio_only=audio_only)
