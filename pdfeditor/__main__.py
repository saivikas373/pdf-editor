"""Entry point:  python -m pdfeditor [file.pdf] [--port 8765] [--no-browser] [--app]"""

from __future__ import annotations

import argparse
import errno
import os
import sys
import threading
import webbrowser

from . import __version__
from .document import LOCK, DocSession, NeedsPassword
from .fonts import SYSTEM_FONTS
from . import config
from .server import APP, Handler, Server, watchdog
from .textedit import EditError
from .tools import ensure_tessdata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pdfeditor", description="Local PDF editor")
    parser.add_argument("file", nargs="?", help="PDF to open (Save writes back to this file)")
    # hosting platforms hand the port over in PORT; on your own machine it is 8765
    default_port = int(os.environ.get("PORT") or os.environ.get("PDF_EDITOR_PORT") or 8765)
    parser.add_argument("--port", type=int, default=default_port)
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    parser.add_argument("--app", action="store_true", help="quit automatically when the editor tab is closed")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    SYSTEM_FONTS.start_background_scan()
    ensure_tessdata()

    if args.file:
        path = os.path.abspath(os.path.expanduser(args.file))
        try:
            with open(path, "rb") as f:
                data = f.read()
            with LOCK:
                session = DocSession(data, os.path.basename(path), path)
            APP.add_session(session)
            APP.preload_id = session.id
        except NeedsPassword:
            print("This PDF is password protected - open it from the editor window instead.", file=sys.stderr)
        except (OSError, EditError) as exc:
            print(f"Could not open {path}: {exc}", file=sys.stderr)

    server = None
    for port in [args.port] + list(range(args.port + 1, args.port + 30)) + [0]:
        try:
            server = Server((config.BIND, port), Handler, app_mode=args.app, verbose=args.verbose)
            break
        except OSError as exc:
            if exc.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
    assert server is not None
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"PDF Editor {__version__} running at {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    if args.app:
        threading.Thread(target=watchdog, args=(server,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
