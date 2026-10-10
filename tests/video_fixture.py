"""Small structural MP4 for transport mocks; real codec tests use FFmpeg fixtures."""


def box(kind, data):
    return (len(data) + 8).to_bytes(4, "big") + kind + data


def mp4_header(width=32, height=32, duration=1, codec=b"avc1", *, handler=b"vide"):
    tkhd = bytearray(84)
    tkhd[40:44] = (65536).to_bytes(4, "big")
    tkhd[56:60] = (65536).to_bytes(4, "big")
    tkhd[-8:-4] = (width << 16).to_bytes(4, "big")
    tkhd[-4:] = (height << 16).to_bytes(4, "big")
    mdhd = bytearray(24)
    mdhd[12:16] = (1000).to_bytes(4, "big")
    mdhd[16:20] = int(duration * 1000).to_bytes(4, "big")
    stsd = box(b"stsd", b"\0" * 4 + (1).to_bytes(4, "big") + box(codec, b""))
    one = (1).to_bytes(4, "big")
    tables = (
        box(b"stts", b"\0" * 4 + one + one + int(duration * 1000).to_bytes(4, "big"))
        + box(b"stsz", b"\0" * 4 + one + one)
        + box(b"stsc", b"\0" * 4 + one + one * 3)
        + box(b"stco", b"\0" * 4 + one + (512).to_bytes(4, "big"))
    )
    mdia = box(
        b"mdia",
        box(b"hdlr", b"\0" * 8 + handler)
        + box(b"mdhd", mdhd)
        + box(b"minf", box(b"stbl", stsd + tables)),
    )
    moov = box(b"moov", box(b"trak", box(b"tkhd", tkhd) + mdia))
    return box(b"ftyp", b"isom\0\0\0\0isommp42") + moov
