"""Small structural MP4 for transport mocks; real codec tests use FFmpeg fixtures."""


def box(kind, data):
    return (len(data) + 8).to_bytes(4, "big") + kind + data


def mp4_header(width=32, height=32, duration=1, codec=b"avc1"):
    tkhd = bytearray(84)
    tkhd[40:44] = (65536).to_bytes(4, "big")
    tkhd[56:60] = (65536).to_bytes(4, "big")
    tkhd[-8:-4] = (width << 16).to_bytes(4, "big")
    tkhd[-4:] = (height << 16).to_bytes(4, "big")
    mdhd = bytearray(24)
    mdhd[12:16] = (1000).to_bytes(4, "big")
    mdhd[16:20] = int(duration * 1000).to_bytes(4, "big")
    stsd = box(b"stsd", b"\0" * 4 + (1).to_bytes(4, "big") + box(codec, b""))
    mdia = box(
        b"mdia",
        box(b"hdlr", b"\0" * 8 + b"vide") + box(b"mdhd", mdhd) + box(b"minf", box(b"stbl", stsd)),
    )
    moov = box(b"moov", box(b"trak", box(b"tkhd", tkhd) + mdia))
    return box(b"ftyp", b"isom\0\0\0\0isommp42") + moov
