"""The behaviour that only matters when the editor is on a server.

Run:  python tests/test_public.py

One visitor must never see, or be able to push out, another visitor's documents, and
a request for a hostname nobody configured must be refused.
"""

import os
import sys
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# read before the server module is imported: these are start-up settings
os.environ["PDF_EDITOR_ALLOWED_HOSTS"] = "pdf.example.com"
os.environ["PDF_EDITOR_SESSIONS_PER_VISITOR"] = "2"
os.environ["PDF_EDITOR_MAX_UPLOAD_MB"] = "1"

import pymupdf as fitz  # noqa: E402

from pdfeditor.server import Handler, Server  # noqa: E402

server = Server(("127.0.0.1", 0), Handler)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def call(method, path, body=None, cookie=None, host="pdf.example.com", ctype=None):
    headers = {"X-PDF-Editor": "1", "Host": host}
    if cookie:
        headers["Cookie"] = cookie
    if ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, res.read(), res.headers.get("Set-Cookie")
    except urllib.error.HTTPError as err:
        return err.code, err.read(), err.headers.get("Set-Cookie")


def a_pdf(name="x.pdf"):
    doc = fitz.open()
    doc.new_page()
    return doc.tobytes()


def open_doc(cookie=None):
    status, body, set_cookie = call("POST", "/api/open", a_pdf(), cookie=cookie)
    import json
    doc_id = json.loads(body)["doc"]["id"] if status == 200 else None
    jar = cookie or (set_cookie.split(";")[0] if set_cookie else None)
    return status, doc_id, jar


print("a hostname nobody configured:")
status, _, _ = call("GET", "/api/config", host="evil.example")
check(status == 403, f"a request for an unknown host is refused ({status})")
status, _, _ = call("GET", "/api/config", host="pdf.example.com")
check(status == 200, f"the configured host is served ({status})")
status, _, _ = call("GET", "/api/config", host=f"127.0.0.1:{PORT}")
check(status == 200, f"localhost still works for the operator ({status})")

print("which hostnames are served:")
from pdfeditor import config  # noqa: E402

config.ALLOWED_HOSTS = [".onrender.com", "pdf.example.com"]
for host, want in [("pdf-editor-xy.onrender.com", True), ("pdf.example.com", True), ("10.0.0.7", True),
                   ("localhost", True), ("[::1]", True), ("evil.example", False),
                   ("notonrender.com", False), ("pdf.example.com.evil.example", False)]:
    got = config.host_allowed(host)
    check(got == want, f"{host} is {'served' if want else 'refused'}")
config.ALLOWED_HOSTS = ["pdf.example.com"]

print("one visitor cannot reach another's document:")
status, mine, jar_a = open_doc()
check(status == 200 and bool(jar_a), "a visitor gets a cookie when they open a document")
status_b, theirs, jar_b = open_doc()
check(jar_b and jar_b != jar_a, "a second visitor gets a different cookie")
status, _, _ = call("GET", f"/api/doc/{mine}", cookie=jar_a)
check(status == 200, "a visitor can reach their own document")
status, _, _ = call("GET", f"/api/doc/{mine}", cookie=jar_b)
check(status == 404, f"another visitor cannot reach it ({status})")
status, _, _ = call("GET", f"/api/doc/{mine}")
check(status == 404, f"nor can someone with no cookie ({status})")

print("one visitor cannot push out another's documents:")
keep = theirs
for _ in range(3):  # the cap is two per visitor
    open_doc(cookie=jar_a)
status, _, _ = call("GET", f"/api/doc/{mine}", cookie=jar_a)
check(status == 404, "a visitor's own oldest document is dropped once they pass the cap")
status, _, _ = call("GET", f"/api/doc/{keep}", cookie=jar_b)
check(status == 200, "the other visitor's document is untouched")

print("an upload larger than the limit:")
try:
    status, body, _ = call("POST", "/api/open", b"%PDF-1.4\n" + b"0" * (2 * 1024 * 1024))
except urllib.error.URLError as exc:
    status = f"connection closed ({exc.reason})"  # also a refusal, just a blunter one
check(status == 413 or "closed" in str(status), f"a 2 MB upload is refused when the cap is 1 MB ({status})")

server.shutdown()
print("\nFAILURES:" if failures else "\nall public-mode checks passed", *failures, sep="\n  ")
sys.exit(1 if failures else 0)
