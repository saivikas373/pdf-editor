# Putting the editor on a server

The same code runs locally and on a server; what changes is a handful of environment
variables. Nothing here is specific to one host — it is a container and a reverse
proxy, so it runs on a VM you own, or on any platform that takes a Dockerfile.

## What you need

- A machine with Docker, reachable from the internet
- A hostname pointing at it (`pdf.example.com`)
- Ports 80 and 443 open

## Deploy

```bash
git clone <your repo> pdf-editor && cd pdf-editor
printf 'DOMAIN=pdf.example.com\nEMAIL=you@example.com\nSOURCE_URL=https://github.com/you/pdf-editor\n' > .env
docker compose up -d --build
```

Caddy gets the TLS certificate by itself. Visit the hostname; there is nothing else
to configure.

## The settings that matter

| Variable | Default | What it does |
| --- | --- | --- |
| `PDF_EDITOR_BIND` | `127.0.0.1` | `0.0.0.0` to accept connections from outside the machine |
| `PDF_EDITOR_ALLOWED_HOSTS` | *(none)* | Hostnames to serve. A request for any other `Host` is refused — this is what stops a hostile page from driving the server through DNS rebinding. Setting it is what puts the editor in public mode. |
| `PDF_EDITOR_BEHIND_PROXY` | off | Marks the visitor cookie `Secure`, for when TLS is terminated in front |
| `PDF_EDITOR_MAX_UPLOAD_MB` | `1024` | Refused above this, and the page says so before uploading |
| `PDF_EDITOR_SESSIONS_PER_VISITOR` | `6` | Open documents one visitor may hold |
| `PDF_EDITOR_SESSIONS_TOTAL` | `200` | Open documents across everyone |
| `PDF_EDITOR_SESSION_IDLE_MINUTES` | `30` | A document nobody has touched for this long is dropped |
| `PDF_EDITOR_MAX_OCR_PAGES` | `0` (no limit) | Pages one OCR request may do |
| `PDF_EDITOR_SOURCE_URL` | *(none)* | Linked in the footer — see the licence note below |

In public mode each visitor gets a cookie, documents belong to whoever opened them,
and nobody else can reach them or push them out. `tests/test_public.py` checks all of
that; run it after any change here.

## What this will and will not carry

Documents live in the server's memory — about 1.5 to 2.5 times the size of the file,
including its undo history. That part is cheap: a few hundred MB holds a hundred
ordinary documents.

The real limit is CPU, and there are two things to know:

- **The server handles one request at a time.** That is deliberate and safe for
  a local tool, and it is the ceiling here. A long job blocks everything else, which
  is why `PDF_EDITOR_MAX_OCR_PAGES` exists.
- **OCR and matching the font of scanned text are the expensive jobs.** Merging,
  splitting, compressing and rotating are cheap in comparison.

Measured on a fast laptop core: merging three small files ~0.04s, opening a 242-page
book ~0.6s, compressing it ~0.85s, rendering one page ~0.03s, OCR seconds per page.
Divide by whatever fraction of a core your host gives you. A tenth of a core — the
usual free-tier allowance — turns that book into ten seconds of opening.

**You cannot scale this by adding instances.** Sessions live in one process's memory,
so a second container would not see the first one's documents. Growth means a bigger
machine, until sessions move to shared storage.

## On Oracle Cloud's free tier

It is the only free option with real CPU, and two things catch everyone:

1. **There are two firewalls.** Open the port in the OCI security list *and* in the
   instance: `sudo iptables -I INPUT 5 -p tcp --dport 443 -j ACCEPT` (and 80), then
   `sudo netfilter-persistent save`. Ubuntu images on OCI drop everything but SSH.
2. **Idle instances are reclaimed.** An Always Free ARM instance can be taken back if,
   over 7 days, its 95th-percentile CPU, network *and* memory are all under 20%. It is
   an AND, so keeping any one above the line is enough — the simplest insurance is to
   ask for a smaller shape, which lowers the bar.

## Licence

PyMuPDF is AGPL, and section 13 covers use over a network: people using this on your
server are entitled to its source. Set `PDF_EDITOR_SOURCE_URL` to your repository and
it appears in the footer. The alternative is a commercial licence from Artifex.
