import re

PINTEREST_DOMAINS = (
    "com",
    "fr",
    "de",
    "ch",
    "jp",
    "cl",
    "ca",
    "it",
    "co.uk",
    "nz",
    "ru",
    "com.au",
    "at",
    "pt",
    "co.kr",
    "es",
    "com.mx",
    "dk",
    "ph",
    "th",
    "com.uy",
    "co",
    "nl",
    "info",
    "kr",
    "ie",
    "vn",
    "com.vn",
    "ec",
    "mx",
    "in",
    "pe",
    "co.at",
    "hu",
    "co.in",
    "co.nz",
    "id",
    "com.ec",
    "com.py",
    "tw",
    "be",
    "uk",
    "com.bo",
    "com.pe",
)
PINTEREST_HOSTS = {
    prefix + "pinterest." + domain for domain in PINTEREST_DOMAINS for prefix in ("", "www.", "m.")
} | {locale + ".pinterest.com" for locale in ("co", "id", "uk", "de", "fr", "es", "it", "jp", "br")}
PAGE_HOSTS = PINTEREST_HOSTS | {"pin.it"}


LINK_PATTERN = re.compile(
    r"(?<![\w.@/:-])(?:https?://)?(?:"
    + "|".join(re.escape(host) for host in sorted(PAGE_HOSTS))
    + r")(?=$|[/\s?#])(?:[/?#][^\s<>\"]*)?",
    re.IGNORECASE,
)


def extract_url(text: str) -> str | None:
    match = LINK_PATTERN.search(text)
    if not match:
        return None
    url = match[0].rstrip(".,;!?)]}»")
    if not re.match(r"https?://", url, re.IGNORECASE):
        url = "https://" + url
    return re.sub(r"^http://", "https://", url, flags=re.IGNORECASE)
