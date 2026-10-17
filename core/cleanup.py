"""Independent cleanup choices and compact, strictly validated filter snapshots."""

from dataclasses import dataclass

from schemas.media import DownloadError

ALPHABET = "0123456789abcdefghijklmnopqrstuv"


@dataclass(frozen=True)
class Attribute:
    code: str
    key: str
    values: tuple[str, ...]

    @property
    def n(self):
        return len(self.values)

    @property
    def full(self):
        return (1 << self.n) - 1


@dataclass(frozen=True)
class Entity:
    code: str
    name: str
    attributes: tuple[Attribute, ...]
    default: tuple[int, ...]


ENTITIES = (
    Entity(
        "u",
        "users",
        (
            Attribute("st", "started", ("yes", "no")),
            Attribute("mb", "membership", ("yes", "no")),
            Attribute("ak", "kind", ("real", "unknown", "bot", "deleted")),
        ),
        (2, 1, 15),
    ),
    Entity(
        "g",
        "groups",
        (
            Attribute("gad", "admin", ("yes", "no")),
            Attribute("gmb", "members", ("yes", "no")),
            Attribute("gow", "owner", ("yes", "no")),
        ),
        (3, 3, 3),
    ),
)


def encode(ent, masks):
    return ent.code + "_" + "".join(ALPHABET[mask] for mask in masks)


def decode(raw):
    parts = raw.split("_")
    ent = next((item for item in ENTITIES if item.code == parts[0]), None)
    if ent is None or len(parts) != 2 or len(parts[1]) != len(ent.attributes):
        raise DownloadError("فیلتر معتبر نیست.")
    masks = tuple(ALPHABET.find(char) for char in parts[1])
    if any(mask < 0 or mask > attr.full for attr, mask in zip(ent.attributes, masks, strict=True)):
        raise DownloadError("فیلتر معتبر نیست.")
    return ent, masks


def filters(ent, masks):
    result = {}
    for attr, mask in zip(ent.attributes, masks, strict=True):
        values = [value for index, value in enumerate(attr.values) if mask & (1 << index)]
        result[attr.key] = (
            "all"
            if mask == attr.full
            else "none"
            if not values
            else values[0]
            if len(values) == 1
            else values
        )
    return result
