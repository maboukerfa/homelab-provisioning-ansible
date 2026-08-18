#!/usr/bin/env python3
"""Re-OCR the paperless documents nobody has been over yet, and retitle them.

One pass, one job: every document that is not already marked ai-processed is
downloaded, rasterized page by page with ghostscript, transcribed by LightOnOCR
through the litellm proxy, and written back to paperless together with a title
asked of a second model from that transcription. Nothing else -- this is the
`ocr --retitle --apply` half of the interactive script it was cut down from,
with the review step removed because a timer has nobody to review for.

    paperless-ocr.py                 # a pass, for real
    paperless-ocr.py --dry-run       # what it would process, spending nothing
    paperless-ocr.py --limit 5       # stop after five documents

Deployed by roles/paperless_ocr and normally run by paperless-ocr.timer. Every
knob arrives through the environment, so this file is identical on every host
and in the repo, and it reads the two files below itself rather than leaving
them to systemd's EnvironmentFile=. That is what makes `sudo paperless-ocr.py
--dry-run` -- how you check the thing before enabling the timer -- behave
exactly like the timer does, rather than like the timer minus its unit file.

    /etc/paperless-ocr/config   the knobs, written by Ansible
    /etc/paperless-ocr/env      the two credentials, placed by hand, 0600

Anything already set in the environment wins over both, so a one-off
`OCR_LIMIT=1 paperless-ocr.py` needs nothing edited.

    PAPERLESS_URL     paperless base URL      (default http://localhost:8001)
    PAPERLESS_TOKEN   required -- paperless UI, My Profile, API Auth Token
    LITELLM_URL       proxy base URL          (default http://localhost:8006)
    LITELLM_API_KEY   required -- a virtual key from the litellm proxy
    OCR_MODEL         vision model            (default lightonocr-2-1b)
    TITLE_MODEL       titling model           (default openai/gpt-oss-120b)
    OCR_DPI           rasterization DPI       (default 150)
    OCR_LIMIT         documents per pass      (default 0, meaning all of them)
    PROCESSED_FIELD   the marker field        (default ai-processed)

APPLYING OVERWRITES THE DOCUMENT CONTENT IN PAPERLESS. Nothing is copied aside
first, and that is a deliberate call rather than an oversight: the original
file is never touched, so paperless can always redo its own OCR from it
(`document_archiver --overwrite`), which makes a per-document backup here a
second copy of something already recoverable. An empty transcription is never
applied -- it would blank the document rather than improve it.

The marker is what keeps this affordable. ai-processed = true is set only once
both the text and the title landed, so a pass over a paperless with nothing new
in it is a single listing call, and a run killed halfway costs only the
document it was in the middle of.
"""

import argparse
import base64
import html
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# The knobs, then the credentials. Kept apart because one file is Ansible's to
# rewrite on every deploy and the other is a hand-placed secret that must never
# be in git -- the same split as a stack's compose.yaml and its .env.
CONFIG_FILE = "/etc/paperless-ocr/config"
ENV_FILE = "/etc/paperless-ocr/env"


def load_env(path):
    """KEY=value lines into the environment, without overriding what is set.

    setdefault, not assignment: an inline `OCR_LIMIT=1 paperless-ocr.py` still
    wins over both files, and the first file to name a key wins over the second.
    """
    try:
        text = pathlib.Path(path).read_text()
    except OSError:                     # absent, or not ours to read
        return
    for line in text.splitlines():
        line = line.strip().removeprefix("export ").strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_env(CONFIG_FILE)                   # both before the settings below read
load_env(ENV_FILE)                      # them, knobs first, credentials second

PAPERLESS = os.environ.get("PAPERLESS_URL", "http://localhost:8001").rstrip("/")
TOKEN = os.environ.get("PAPERLESS_TOKEN", "")
CHAT_URL = os.environ.get("LITELLM_URL", "http://localhost:8006").rstrip("/") \
    + "/chat/completions"
KEY = os.environ.get("LITELLM_API_KEY", "")
OCR_MODEL = os.environ.get("OCR_MODEL", "lightonocr-2-1b")
TITLE_MODEL = os.environ.get("TITLE_MODEL", "openai/gpt-oss-120b")
DPI = int(os.environ.get("OCR_DPI", "150"))
LIMIT = int(os.environ.get("OCR_LIMIT", "0"))
FIELD = os.environ.get("PROCESSED_FIELD", "ai-processed")

ACCEPT = "application/json; version=10"     # pin the API version, not "latest"
PAGE_SIZE = 100
MAX_TOKENS = 4000       # per page of transcription
TITLE_MAX = 60          # the prompt asks for this; the model is not held to it
TITLE_CHARS = 8000      # of transcription sent for titling. Headers and dates
                        # live at the top, and a full avis d'imposition is 14k
TITLE_TOKENS = 1000     # a reasoning model spends this budget thinking before
                        # it answers, and returns content=None if it runs out
                        # first. The title is ~15 tokens; the rest is headroom
RETRY = {429, 500, 502, 503, 504}
QUOTES = "\"'«»“”‘’ "

# Stamped and tiled documents -- watermarks, security print -- send the model
# into verbatim repeat loops. Escalate the frequency penalty until the output
# stops degenerating.
PENALTIES = (0.0, 0.5, 1.0)

OCR_PROMPT = ("Transcribe all text from this image as plain text. No markdown, "
              "no HTML, no headers, no tables, no image placeholders. Preserve "
              "line and paragraph breaks only.")

TITLE_PROMPT = """Role: You are an expert Document Archivist and Information Architect. Your task is to analyze the text provided, which has been generated via OCR (Optical Character Recognition) from scanned documents.

Task: Generate a concise, descriptive, and professional title for the document based on its primary subject matter, intent, and key entities (e.g., companies, dates, or specific project names) and always use the english or french language depending on the original document language.

Guidelines:

    Contextual Accuracy: Prioritize formal headers, dates, and recurring keywords to determine the document type (e.g., Invoice, Technical Specification, Meeting Minutes, Contract).

    OCR Resilience: Ignore "noise" typical of OCR, such as garbled characters, misread page numbers, or fragmented headers.

    Format: Output only the suggested title. Limit the length to no more than 60 characters. Do not include introductory text like "The title is..." or "I suggest..."

    Naming Convention: Use a standard format: [Document Type] - [Main Subject/Entity]. If a date is not found, omit it.
    Exceptions:
        - For payslip use this naming format : Payslip - Month(always in english) YEAR - Family Name(Capitalize only first letter) (COMPANY), for example : Payslip - June 2026 - Boukerfa (INRAE)
        - IMPORTANT: For all document types EXCEPT payslips, do NOT include any person's name in the title. Use only company names, project names, or subject matter.

Tone: Professional and objective."""

# The model answers in markdown and HTML however plainly the prompt forbids it,
# so the prompt is the request and these are the enforcement.
IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")          # ![image](image_1.png)
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")          # [label](target) -> label
TAG = re.compile(r"<[^>]+>")                         # <div style="...">, <br />
HEADING = re.compile(r"^\s{0,3}#{1,6}\s*")
EMPHASIS = re.compile(r"\*\*|__|`{1,3}")
RULE = re.compile(r"^\s*([-*_]\s*){3,}$")            # --- , *** , ___
TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]{3,}\|?\s*$")  # |---|:--:|
NOTE = re.compile(r"^\s*(Note|Remarque)\s*:", re.I)  # model commentary


class Fail(Exception):
    """An expected failure: one line in the journal, no traceback.

    Carries the HTTP status when there was one, which is what tells "slow down"
    apart from "you are wrong" without going back through the message text.
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def log(message):
    """One timestamped line, on stderr.

    Everything goes to stderr, unlike the interactive script this came from:
    the only reader is journalctl, and splitting the stream there just
    interleaves two orderings of the same run.
    """
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"[{stamp}] {message}", file=sys.stderr, flush=True)


# ----------------------------------------------------------------------- http

def http(url, payload=None, *, method=None, headers=None, timeout=120):
    """GET, or POST if payload is given. Returns parsed JSON."""
    body = json.dumps(payload).encode() if payload is not None else None
    verb = method or ("POST" if body else "GET")
    request = urllib.request.Request(url, data=body, method=verb, headers={
        **({"Content-Type": "application/json"} if body else {}),
        **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode(errors="replace") or "{}")
    except urllib.error.HTTPError as e:      # a URLError subclass, catch first
        detail = e.read().decode(errors="replace").strip()[:400]
        raise Fail(f"{verb} {url} -> {e.code} {e.reason}"
                   + (f": {detail}" if detail else ""), status=e.code) from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise Fail(f"{verb} {url} -> {e}") from None


def api(path, payload=None, method=None):
    """One paperless call. Paths are absolute: /api/documents/?page_size=100"""
    return http(PAPERLESS + path, payload, method=method, headers={
        "Authorization": f"Token {TOKEN}", "Accept": ACCEPT})


def paged(path):
    """Every result across every page, one page in flight at a time.

    The `next` link comes back with whatever hostname paperless believes it is
    reachable at, which is not necessarily the one we reached it on -- so keep
    the path and the query and rebuild the rest ourselves.
    """
    while path:
        page = api(path)
        yield from page["results"]
        following = urllib.parse.urlsplit(page.get("next") or "")
        path = following.path + (f"?{following.query}" if following.query else "")


def download(doc_id, dest):
    """The archive PDF, straight to disk.

    Deliberately not api(): that decodes every response as JSON, which turns a
    PDF into an exception rather than a file ghostscript will open.

    The archive version rather than ?original=true, because paperless renders
    one for everything it OCRs -- so a photographed receipt arrives here as a
    PDF like everything else and there is only ever one input format to handle.
    """
    request = urllib.request.Request(
        f"{PAPERLESS}/api/documents/{doc_id}/download/",
        headers={"Authorization": f"Token {TOKEN}", "Accept": ACCEPT})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            dest.write_bytes(response.read())
    except OSError as e:
        raise Fail(f"download {doc_id} -> {e}") from None


# --------------------------------------------------------------- the document

def processed_field(create=True):
    """The id of the boolean field that means "this tool has been over it".

    Created when it is missing, so a paperless this has never run against needs
    nothing set up by hand first.
    """
    for field in paged(f"/api/custom_fields/?page_size={PAGE_SIZE}"):
        if field["name"] == FIELD:
            return field["id"]
    if not create:
        log(f"custom field {FIELD} does not exist yet -- a real run creates it")
        return 0                        # an id no document can be carrying
    created = api("/api/custom_fields/", {"name": FIELD, "data_type": "boolean"})
    log(f"created custom field {FIELD} ({created['id']})")
    return created["id"]


def pending(field, limit):
    """Documents not yet marked, newest first.

    Filtered here rather than in the query: the field is only true on documents
    this tool finished, and "carries the field with some other value" is not
    the same question. Newest first because a scan you just filed is the one
    you are waiting to see a title on.
    """
    found = []
    for doc in paged(f"/api/documents/?page_size={PAGE_SIZE}&ordering=-created"):
        if any(c["field"] == field and c["value"] is True
               for c in doc.get("custom_fields") or []):
            continue
        found.append(doc)
        if limit and len(found) >= limit:
            break
    return found


def rasterize(pdf, outdir):
    """One PNG per page.

    The default 150 DPI lands an A4 page a little under 1800px on the long
    edge, which is where the vision model stops getting anything out of more
    pixels and starts only getting slower. The alpha bits matter more than they
    look: ghostscript renders aliased by default, and 1-bit edges on 8pt type
    is exactly the input OCR reads back as garbled characters.
    """
    proc = subprocess.run(
        ["gs", "-sDEVICE=png16m", f"-r{DPI}", "-dTextAlphaBits=4",
         "-dGraphicsAlphaBits=4", "-dNOPAUSE", "-dBATCH", "-q",
         f"-sOutputFile={outdir}/page-%d.png", str(pdf)],
        capture_output=True, text=True, timeout=600)
    if proc.returncode:
        raise Fail(f"gs exit {proc.returncode}: "
                   f"{(proc.stderr or proc.stdout).strip()[:400]}")
    return sorted(outdir.glob("page-*.png"),
                  key=lambda p: int(p.stem.split("-")[1]))


# --------------------------------------------------------------------- models

def chat(payload, tries=5):
    """One call to the litellm proxy, retried when it pushes back.

    The proxy caps the title model at ten requests a minute. Backing off from
    the 429 rather than pacing every call to stay under it is the right way
    round here: one document is a single title call and several much slower OCR
    calls, so the cap is almost never what is in the way.
    """
    for attempt in range(1, tries + 1):
        try:
            return http(CHAT_URL, payload, timeout=600,
                        headers={"Authorization": f"Bearer {KEY}"})
        except Fail as failure:
            if attempt == tries or failure.status not in RETRY:
                raise
            pause = min(60, 10 * 2 ** (attempt - 1))
            log(f"  proxy said {failure.status}, waiting {pause}s "
                f"(attempt {attempt}/{tries})")
            time.sleep(pause)


def degenerate(text, finish_reason):
    """True if the page looks like a repetition loop rather than a transcription."""
    if finish_reason == "length":
        return True
    blocks = [b for b in text.split("\n\n") if b.strip()]
    return len(blocks) >= 6 and len(set(blocks)) / len(blocks) < 0.5


def transcribe(png):
    """One page through the proxy, which speaks the OpenAI chat shape."""
    encoded = base64.b64encode(png.read_bytes()).decode()
    for penalty in PENALTIES:
        answer = chat({
            "model": OCR_MODEL,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": OCR_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + encoded}}]}],
            "max_tokens": MAX_TOKENS, "temperature": 0.0,
            "frequency_penalty": penalty,
        })
        choice = answer["choices"][0]
        text = choice["message"]["content"] or ""
        if not degenerate(text, choice.get("finish_reason")):
            break
        log(f"  {png.stem}: degenerate at penalty={penalty} "
            f"({len(text)} ch), retrying")
    return text.strip()


def clean(text):
    """Formatting out, words kept, repeated blocks collapsed.

    Paperless indexes this text for search, so a page that comes back as
    <div style="display: flex"> is worse than useless: it makes CSS keywords
    match the document while burying the words that should.
    """
    lines = []
    for line in html.unescape(text).splitlines():
        for pattern, replacement in ((IMAGE, ""), (LINK, r"\1"), (TAG, ""),
                                     (HEADING, ""), (EMPHASIS, "")):
            line = pattern.sub(replacement, line)
        if RULE.match(line) or TABLE_SEP.match(line):
            continue
        if "|" in line:            # a table row: keep the cells, drop the pipes
            line = "  ".join(c.strip() for c in line.strip().strip("|").split("|"))
        lines.append(line.rstrip())

    kept, previous = [], None
    for block in "\n".join(lines).split("\n\n"):
        block = block.strip()
        if not block or block == previous or NOTE.match(block):
            continue
        kept.append(block)
        previous = block
    return "\n\n".join(kept).strip()


def tidy(raw):
    """The title line: no quotes, no markdown, no lead-in, capped at TITLE_MAX.

    The prompt forbids a preamble, so a line ending in ':' is one the model was
    not supposed to send -- skip it rather than title the document "I suggest:".
    """
    lines = [l.strip() for l in clean(raw).splitlines() if l.strip()]
    line = next((l for l in lines if not l.endswith(":")), "").strip(QUOTES)
    for lead in ("title:", "titre:"):       # in case it ignores the format rule
        if line.lower().startswith(lead):
            line = line[len(lead):].strip(QUOTES)
    if len(line) > TITLE_MAX:          # cut on a word boundary, not mid-word
        line = (line[:TITLE_MAX].rsplit(" ", 1)[0]
                or line[:TITLE_MAX]).rstrip(" -–—")
    return line


def title_for(text):
    answer = chat({
        "model": TITLE_MODEL,
        "messages": [{"role": "system", "content": TITLE_PROMPT},
                     {"role": "user", "content": text[:TITLE_CHARS]}],
        "max_tokens": TITLE_TOKENS, "temperature": 0.0})
    return tidy(answer["choices"][0]["message"]["content"] or "")


# ------------------------------------------------------------------ the pass

def process(doc, field):
    """Download, transcribe, title, write both back, mark it done."""
    doc_id = doc["id"]
    original = (doc.get("content") or "").strip()

    # Everything on disk is scratch and none of it outlives the document: the
    # transcription goes to paperless, which is the only place anyone reads it.
    with tempfile.TemporaryDirectory(prefix=f"paperless-ocr-{doc_id}-") as tmp:
        work = pathlib.Path(tmp)
        pdf = work / "document.pdf"
        download(doc_id, pdf)
        with pdf.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise Fail("no archive PDF -- paperless skipped OCR for this "
                           "one, so there is nothing to rasterize")
        pages = rasterize(pdf, work)
        chunks = []
        for number, png in enumerate(pages, 1):
            chunks.append(transcribe(png))
            log(f"  page {number}/{len(pages)}: {len(chunks[-1])} ch")
        text = clean("\n\n".join(chunks))

    if not text:
        # Applying would blank the document rather than improve it, and there
        # would be nothing to title it from either.
        raise Fail("the transcription came back empty")
    log(f"  {len(original)} -> {len(text)} ch")
    if len(text) < len(original) / 2:
        log("  WARNING: the new text is less than half the old -- check this "
            "document, paperless read more out of it than the model did")

    title = title_for(text)
    if not title:
        raise Fail("no title came back")
    log(f"  title: {doc.get('title')!r} -> {title!r}")

    # One PATCH, so a retitled document can never end up carrying the new title
    # beside the old text.
    api(f"/api/documents/{doc_id}/", {"content": text, "title": title},
        method="PATCH")

    # The marker goes through bulk_edit, never a PATCH: `custom_fields` in a
    # PATCH replaces the whole list, so marking a document that way silently
    # drops every other field it carries. bulk_edit adds one and is idempotent.
    result = api("/api/documents/bulk_edit/", {
        "documents": [doc_id], "method": "modify_custom_fields",
        "parameters": {"add_custom_fields": {str(field): True},
                       "remove_custom_fields": []}})
    if result.get("result") != "OK":
        raise Fail(f"applied, but bulk_edit answered {result}")
    log(f"  applied, {FIELD} = true")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=LIMIT, metavar="N",
                    help=f"stop after N documents (default {LIMIT or 'all'})")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be processed, spend nothing")
    args = ap.parse_args()

    if not TOKEN:
        raise Fail(f"set PAPERLESS_TOKEN in {ENV_FILE} -- paperless UI, "
                   "My Profile, API Auth Token")
    if not KEY:
        raise Fail(f"set LITELLM_API_KEY in {ENV_FILE} -- a virtual key from "
                   "the litellm proxy")
    if not args.dry_run and shutil.which("gs") is None:
        raise Fail("ghostscript is not installed: no gs on PATH")

    field = processed_field(create=not args.dry_run)
    documents = pending(field, args.limit)
    if not documents:
        log(f"nothing to do: every document is {FIELD} = true")
        return 0

    if args.dry_run:
        log(f"would process {len(documents)} document(s):")
        for doc in documents:
            log(f"  {doc['id']:>5}  {doc.get('title') or ''}")
        return 0

    failed = 0
    for doc in documents:
        log(f"{doc['id']}: {doc.get('title') or ''} "
            f"({doc.get('page_count') or '?'} page(s))")
        try:
            process(doc, field)
        except (Fail, OSError, KeyError, subprocess.TimeoutExpired) as failure:
            # One bad document does not stop the pass: it stays unmarked and
            # comes round again on the next timer, and everything after it
            # still gets done tonight rather than next time somebody looks.
            log(f"  ERROR: {doc['id']} failed: {failure}")
            failed += 1

    log(f"{len(documents) - failed} processed"
        + (f", {failed} failed" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Fail as failure:
        log(f"ERROR: {failure}")
        sys.exit(1)
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
