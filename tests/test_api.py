"""End-to-end API test against a live server thread.  Run: python tests/test_api.py"""

import io
import json
import os
import sys
import threading
import urllib.request
import zipfile

import pymupdf as fitz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pdfeditor.server import Handler, Server  # noqa: E402

SAMPLES = os.path.join(ROOT, "tests", "samples")
server = Server(("127.0.0.1", 0), Handler)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"
failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def call(method, path, body=None, raw=None, headers=None, expect=200):
    hdrs = {"X-PDF-Editor": "1", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    elif raw is not None:
        data = raw
        hdrs.setdefault("Content-Type", "application/octet-stream")
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req) as resp:
            payload = resp.read()
            status = resp.status
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as err:
        payload, status, ctype = err.read(), err.code, err.headers.get("Content-Type", "")
    if status != expect:
        print("   unexpected status", status, payload[:300])
    if "json" in ctype:
        return status, json.loads(payload)
    return status, payload


# security: missing header / wrong host
req = urllib.request.Request(BASE + "/api/new", data=b"{}", method="POST", headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(req)
    check(False, "POST without header rejected")
except urllib.error.HTTPError as e:
    check(e.code == 403, "POST without header rejected")
req = urllib.request.Request(BASE + "/api/config", headers={"Host": "evil.example"})
try:
    urllib.request.urlopen(req)
    check(False, "foreign Host rejected")
except urllib.error.HTTPError as e:
    check(e.code == 403, "foreign Host rejected (DNS rebinding)")

st, cfg = call("GET", "/api/config")
check(st == 200 and len(cfg["fonts"]) > 10, f"config: {len(cfg['fonts'])} fonts, ocr={cfg['ocr']['available']}")

pdf = open(os.path.join(SAMPLES, "report.pdf"), "rb").read()
st, res = call("POST", "/api/open", raw=pdf, headers={"X-Filename": "report.pdf"})
doc = res["doc"]
doc_id = doc["id"]
pages = doc["pages"]
check(len(pages) == 3, "opened 3 pages")
check(pages[2]["w"] < pages[2]["h"], "rotated landscape page normalised to portrait display size")

st, png = call("GET", f"/api/doc/{doc_id}/page/{pages[0]['id']}.png?scale=0.5&rev={pages[0]['rev']}")
check(st == 200 and png[:4] == b"\x89PNG", "page renders as PNG")

st, text = call("GET", f"/api/doc/{doc_id}/page/{pages[0]['id']}/text")
lines = text["text"]["lines"]
line = next(l for l in lines if l["text"].startswith("Prepared for"))
check(len(lines) > 10 and text["text"]["paragraphs"], "text payload has lines and paragraphs")

st, res = call("POST", f"/api/doc/{doc_id}/text/edit", {"pageId": pages[0]["id"], "edits": [
    {"lineId": line["id"], "origText": line["text"], "text": line["text"].replace("Acme", "Initech"), "bbox": line["bbox"]}]})
check(st == 200 and res["doc"]["canUndo"], "edit text")
new_rev = res["doc"]["pages"][0]["rev"]
check(new_rev != pages[0]["rev"], "page revision bumped after edit")

st, res = call("POST", f"/api/doc/{doc_id}/text/edit", {"pageId": pages[0]["id"], "edits": [
    {"lineId": line["id"], "origText": "stale text", "text": "x"}]}, expect=400)
check(st == 400 and "changed" in res["error"], "stale edit rejected with friendly error")

# objects: text box, rectangle, highlight, image, note, redaction
img = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 20), 1)
img.set_rect(img.irect, (30, 120, 220, 255))
st, asset = call("POST", f"/api/doc/{doc_id}/assets", raw=img.tobytes("png"), headers={"Content-Type": "image/png"})
check(st == 200 and asset["w"] == 40, "image asset uploaded")
pid0 = pages[0]["id"]
ops = [
    {"op": "add", "pageId": pid0, "object": {"id": "t1", "type": "text", "x": 300, "y": 140, "w": 200, "h": 40,
                                            "text": "Approved\nby Jane", "font": "Georgia", "size": 14, "color": "#1a7f37", "bold": True}},
    {"op": "add", "pageId": pid0, "object": {"id": "r1", "type": "rect", "x": 40, "y": 600, "w": 520, "h": 90,
                                            "stroke": "#d0021b", "strokeWidth": 2, "fill": None}},
    {"op": "add", "pageId": pid0, "object": {"id": "h1", "type": "markup", "kind": "highlight", "rects": [[50, 140, 120, 154]], "color": "#ffd400"}},
    {"op": "add", "pageId": pid0, "object": {"id": "i1", "type": "image", "x": 450, "y": 20, "w": 80, "h": 40, "asset": asset["asset"], "opacity": 0.6}},
    {"op": "add", "pageId": pid0, "object": {"id": "n1", "type": "note", "x": 560, "y": 150, "text": "Check this figure"}},
    {"op": "add", "pageId": pid0, "object": {"id": "k1", "type": "ink", "paths": [[[300, 700], [320, 690], [340, 710], [360, 695]]], "stroke": "#0000ff", "strokeWidth": 3}},
    {"op": "add", "pageId": pid0, "object": {"id": "a1", "type": "arrow", "x1": 300, "y1": 760, "x2": 400, "y2": 740, "stroke": "#000000", "strokeWidth": 2}},
    {"op": "add", "pageId": pid0, "object": {"id": "s1", "type": "symbol", "kind": "check", "x": 420, "y": 725, "w": 20, "h": 20, "color": "#0a0"}},
    {"op": "add", "pageId": pid0, "object": {"id": "x1", "type": "redact", "x": 48, "y": 508, "w": 200, "h": 16}},
]
st, res = call("POST", f"/api/doc/{doc_id}/objects", {"ops": ops})
check(st == 200 and len(res["doc"]["objects"][pid0]) == 9, "objects added")
st, res = call("POST", f"/api/doc/{doc_id}/objects", {"ops": [{"op": "update", "pageId": pid0, "object": {**ops[0]["object"], "x": 310}}]})
check(res["doc"]["objects"][pid0][0]["x"] == 310, "object updated")
st, res = call("POST", f"/api/doc/{doc_id}/undo", {})
check(res["doc"]["objects"][pid0][0]["x"] == 300, "undo restores object position")
st, res = call("POST", f"/api/doc/{doc_id}/redo", {})
check(res["doc"]["objects"][pid0][0]["x"] == 310, "redo")

# page operations keep objects attached to their pages
st, res = call("POST", f"/api/doc/{doc_id}/pages/duplicate", {"pageIds": [pid0]})
check(len(res["doc"]["pages"]) == 4 and len(res["doc"]["objects"]) == 2, "duplicate page copies its objects")
order = [p["id"] for p in res["doc"]["pages"]]
st, res = call("POST", f"/api/doc/{doc_id}/pages/reorder", {"order": list(reversed(order))})
check(res["doc"]["pages"][-1]["id"] == pid0, "reorder pages")
st, res = call("POST", f"/api/doc/{doc_id}/pages/delete", {"pageIds": [order[1]]})
check(len(res["doc"]["pages"]) == 3 and pid0 in res["doc"]["objects"], "delete page, objects of other pages kept")
st, res = call("POST", f"/api/doc/{doc_id}/pages/rotate", {"pageIds": [order[1] if False else res['doc']['pages'][0]['id']], "angle": 90})
check(st == 200, "rotate page")
st, res = call("POST", f"/api/doc/{doc_id}/pages/blank", {"at": 0})
check(len(res["doc"]["pages"]) == 4, "insert blank page")
st, res = call("POST", f"/api/doc/{doc_id}/pages/insert-pdf", raw=pdf, headers={"X-Filename": "report.pdf", "X-At": "4"})
check(len(res["doc"]["pages"]) == 7 and res["result"]["inserted"] == 3, "insert PDF pages")
st, res = call("POST", f"/api/doc/{doc_id}/pages/insert-images", {"assets": [asset["asset"]], "at": 7, "pageSize": "a4"})
check(len(res["doc"]["pages"]) == 8, "insert image page")

# forms
form_page = next(p for p in res["doc"]["pages"])  # find the form page by probing
form_pid = None
for p in res["doc"]["pages"]:
    st, f = call("GET", f"/api/doc/{doc_id}/page/{p['id']}/forms")
    if f["fields"]:
        form_pid, fields = p["id"], f["fields"]
        break
check(form_pid is not None, "form fields listed")
name_field = next(f for f in fields if f["name"] == "full_name")
cb = next(f for f in fields if f["type"] == "checkbox")
call("POST", f"/api/doc/{doc_id}/forms", {"pageId": form_pid, "xref": name_field["xref"], "value": "Jane Doe"})
st, res = call("POST", f"/api/doc/{doc_id}/forms", {"pageId": form_pid, "xref": cb["xref"], "value": True})
st, f = call("GET", f"/api/doc/{doc_id}/page/{form_pid}/forms")
check(any(x["value"] == "Jane Doe" for x in f["fields"]), "text field filled")
check(any(x["type"] == "checkbox" and x["value"] not in ("Off", "", False) for x in f["fields"]), "checkbox ticked")

# tools
st, res = call("POST", f"/api/doc/{doc_id}/tools/watermark", {"text": "DRAFT", "opacity": 0.2, "pages": "1-2"})
check(st == 200, "watermark")
st, res = call("POST", f"/api/doc/{doc_id}/tools/stamp", {"format": "Page {n} of {total}", "position": "bottom-right"})
check(st == 200, "page numbers")
st, res = call("POST", f"/api/doc/{doc_id}/tools/metadata", {"title": "Edited report", "author": "Tester"})
check(res["doc"]["metadata"]["title"] == "Edited report", "metadata")
st, res = call("POST", f"/api/doc/{doc_id}/text/replace", {"find": "billing", "replace": "invoicing", "wholeWord": True})
check(res["result"]["replaced"] >= 1, f"find & replace ({res['result']['replaced']})")
st, res = call("POST", f"/api/doc/{doc_id}/search", {"q": "invoicing"})
check(len(res["hits"]) >= 1, "search finds replaced text")

# outputs
st, out = call("POST", f"/api/doc/{doc_id}/export", {"flatten": False, "compress": "balanced"})
exported = fitz.open("pdf", out)
st, info = call("GET", f"/api/doc/{doc_id}")
p_obj = next(i for i, p in enumerate(info["doc"]["pages"]) if p["id"] == pid0)
page = exported[p_obj]
txt = page.get_text()
check("Approved" in txt and "Jane" in txt, "text box baked into export")
check("Initech" in txt, "text edit present in export")
check("Contact" not in txt, "redaction removed text under the box")
kinds = sorted({a.type[1] for a in page.annots()})
check("Highlight" in kinds and "Text" in kinds, f"annotations baked: {kinds}")
check(len(page.get_images()) >= 1, "image baked")
check(exported.metadata.get("title") == "Edited report", "metadata exported")

st, out = call("POST", f"/api/doc/{doc_id}/export", {"password": "s3cret", "flatten": True})
enc = fitz.open("pdf", out)
check(enc.needs_pass and enc.authenticate("s3cret"), "password protected export")
check(not list(enc[p_obj].annots()), "flatten removes annotations (baked into content)")

st, res = call("POST", "/api/open", raw=out, headers={"X-Filename": "enc.pdf"}, expect=401)
check(st == 401 and res.get("needsPassword"), "opening encrypted PDF asks for password")
st, res = call("POST", "/api/open", raw=out, headers={"X-Filename": "enc.pdf", "X-Password": "s3cret"})
check(st == 200, "open with password")

st, z = call("POST", f"/api/doc/{doc_id}/split", {"mode": "every", "value": 3})
names = zipfile.ZipFile(io.BytesIO(z)).namelist()
check(len(names) == 3, f"split into {len(names)} files")
st, x = call("POST", f"/api/doc/{doc_id}/extract", {"pages": "1,3"})
check(fitz.open("pdf", x).page_count == 2, "extract pages")
st, z = call("POST", f"/api/doc/{doc_id}/export-images", {"pages": "1-2", "format": "jpg", "dpi": 50})
check(len(zipfile.ZipFile(io.BytesIO(z)).namelist()) == 2, "export pages as images")
st, t = call("POST", f"/api/doc/{doc_id}/extract-text", {})
check(b"invoicing" in t, "extract text")

if cfg["ocr"]["available"]:
    scan = open(os.path.join(SAMPLES, "scanned.pdf"), "rb").read()
    raster = fitz.open()
    src = fitz.open("pdf", scan)
    pg = raster.new_page(width=src[0].rect.width, height=src[0].rect.height)
    pg.insert_image(pg.rect, pixmap=src[0].get_pixmap(dpi=150))
    st, res = call("POST", "/api/open", raw=raster.tobytes(), headers={"X-Filename": "raster.pdf"})
    rid = res["doc"]["id"]
    st, res = call("POST", f"/api/doc/{rid}/tools/ocr", {"language": "eng", "dpi": 200})
    st, text = call("GET", f"/api/doc/{rid}/page/{res['doc']['pages'][0]['id']}/text")
    ocr_text = " ".join(l["text"] for l in text["text"]["lines"])
    check("Quarterly" in ocr_text, "OCR makes a raster page searchable/editable")

# regression: several requests on ONE keep-alive connection must not leak request bodies
import http.client  # noqa: E402

conn = http.client.HTTPConnection("127.0.0.1", PORT)
H = {"X-PDF-Editor": "1", "Content-Type": "application/json", "Host": f"127.0.0.1:{PORT}"}


def keepalive_post(path, body):
    conn.request("POST", path, body=json.dumps(body), headers=H)
    r = conn.getresponse()
    return r.status, json.loads(r.read())


ka_page = pages[0]["id"]
st, info = call("GET", f"/api/doc/{doc_id}")
ka_page = info["doc"]["pages"][0]["id"]
s1, _ = keepalive_post(f"/api/doc/{doc_id}/objects", {"ops": [{"op": "add", "pageId": ka_page, "object": {"id": "ka1", "type": "rect", "x": 1, "y": 1, "w": 5, "h": 5}}]})
s2, r2 = keepalive_post(f"/api/doc/{doc_id}/objects", {"ops": [{"op": "update", "pageId": ka_page, "object": {"id": "ka1", "type": "rect", "x": 40, "y": 1, "w": 5, "h": 5}}]})
ka = [o for o in r2["doc"]["objects"][ka_page] if o["id"] == "ka1"]
check(s1 == 200 and s2 == 200 and len(ka) == 1 and ka[0]["x"] == 40, "keep-alive connection: bodies not reused between requests")

print("\nFAILURES:" if failures else "\nall API checks passed", *failures, sep="\n  ")
server.shutdown()
