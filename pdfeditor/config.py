"""Settings, read from the environment once at start.

The defaults are the ones that suit a tool running on your own machine: bound to
localhost, generous limits, no ceremony.  Everything that has to change when the
editor is put on a server is an environment variable, so the code that runs there is
the same code that runs here.
"""

from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _is_address(name: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(name.strip("[]"))
        return True
    except ValueError:
        return False


def _list(name: str) -> list[str]:
    return [part.strip().lower() for part in (os.environ.get(name) or "").split(",") if part.strip()]


# where to listen.  0.0.0.0 only makes sense behind something that terminates TLS.
BIND = os.environ.get("PDF_EDITOR_BIND", "127.0.0.1")

# Host headers to accept besides localhost, e.g. "pdf.example.com".  A request whose
# Host is not on the list is refused, which is what stops a hostile page in someone
# else's browser from driving this server through DNS rebinding.
ALLOWED_HOSTS = _list("PDF_EDITOR_ALLOWED_HOSTS")


def host_allowed(name: str) -> bool:
    """Is this Host header one we serve?

    An entry may be a hostname, or ".example.com" for it and all its subdomains.
    An address or a bare name with no dots is always accepted: the attack this check
    exists to stop needs a *name* the attacker controls, so it cannot be an address,
    and platforms health-check their containers by address or container name.
    """
    if not name or "." not in name.strip("[]") or _is_address(name):
        return True
    for entry in ALLOWED_HOSTS:
        if entry.startswith("."):
            if name == entry[1:] or name.endswith(entry):
                return True
        elif name == entry:
            return True
    return False


# behind a reverse proxy the Host header is the public name, so trust it for the check
BEHIND_PROXY = bool(os.environ.get("PDF_EDITOR_BEHIND_PROXY"))

MAX_UPLOAD = _int("PDF_EDITOR_MAX_UPLOAD_MB", 1024) * 1024 * 1024

# documents are held in memory, so both of these are really memory limits
MAX_SESSIONS_PER_VISITOR = _int("PDF_EDITOR_SESSIONS_PER_VISITOR", 6)
MAX_SESSIONS_TOTAL = _int("PDF_EDITOR_SESSIONS_TOTAL", 200)

# a document nobody has touched for this long is dropped
SESSION_IDLE_MINUTES = _int("PDF_EDITOR_SESSION_IDLE_MINUTES", 30)

# the server works on one request at a time, so a long OCR run holds everyone else up
MAX_OCR_PAGES = _int("PDF_EDITOR_MAX_OCR_PAGES", 0)  # 0 = no limit

PUBLIC = bool(ALLOWED_HOSTS)  # a hostname was configured: this is not a local tool

SOURCE_URL = os.environ.get("PDF_EDITOR_SOURCE_URL", "")  # AGPL: where to get the source
