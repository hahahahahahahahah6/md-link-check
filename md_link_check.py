#!/usr/bin/env python3
"""md-link-check: find broken links in your markdown before your readers do.

Scans .md files for local and http(s) links and reports the dead ones.
Stdlib only, Python 3.9+.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 10

# Matches [text](url) and ![alt](src); keeps the leading "!" to tell them apart.
# The URL may contain one level of balanced parentheses (Wikipedia-style).
LINK_RE = re.compile(
    r"(!?)\[([^\]]*)\]\("
    r"((?:[^()\s]|\([^()]*\))+)"
    r"(?:\s+\"[^\"]*\")?"
    r"\)"
)
# Matches [text][label], [text][] and ![alt][label] (reference-style links).
REFLINK_RE = re.compile(r"(!?)\[([^\]]*)\]\[([^\]]*)\]")
# Matches [label]: url reference definitions.
REFDEF_RE = re.compile(r"(?m)^[ ]{0,3}\[([^\]]+)\]:\s*<?(\S+?)>?(?:\s|$)")
# Matches <https://example.com> autolinks.
AUTOLINK_RE = re.compile(r"<(https?://[^>\s]+)>")
# Matches bare http(s) URLs not inside markup. Parens are allowed and then
# balanced (a trailing ")" without a match is sentence punctuation).
BARE_URL_RE = re.compile(r"(?<![\]<\"'`=])(https?://[^\s<>\]\"'`]+)")

KINDS = ("link", "image", "autolink", "bare")


def _mask_indented_code(text):
    """Blank out indented code blocks (4 spaces / tab after a blank line).

    A 4-space-indented line directly under paragraph text is a lazy
    continuation, not code (CommonMark), so only blank-line-preceded
    indented lines are masked.
    """
    out = []
    prev_blank = True
    for line in text.split("\n"):
        if prev_blank and (line.startswith("    ") or line.startswith("\t")):
            out.append("")
        else:
            out.append(line)
        prev_blank = not line.strip()
    return "\n".join(out)


def _strip_bare_url(url):
    """Trim trailing punctuation; drop ')'s with no matching '(' (those are
    sentence punctuation, not part of the URL)."""
    url = url.rstrip(".,;:!?")
    while url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    return url.rstrip(".,;:!?")


def extract_links(text):
    """Yield (url, kind) tuples from markdown text. Images are kind 'image'."""
    seen = set()
    # Mask out fenced code blocks, indented code blocks and inline code
    # so we don't flag examples.
    def mask(m):
        return "\n" * m.group(0).count("\n")

    masked = re.sub(r"```.*?```", mask, text, flags=re.DOTALL)
    masked = re.sub(r"~~~.*?~~~", mask, masked, flags=re.DOTALL)
    masked = _mask_indented_code(masked)
    masked = re.sub(r"`[^`]*`", mask, masked)

    def emit(url, kind):
        # Dedupe identical URLs (checked once), keeping the first kind seen.
        if url not in seen:
            seen.add(url)
            yield url, kind

    for m in LINK_RE.finditer(masked):
        bang, url = m.group(1), m.group(3)
        if url.startswith("#"):  # anchor-only links: nothing to check
            continue
        kind = "image" if bang else "link"
        yield from emit(url, kind)
    # Reference-style links: resolve [text][label] / [text][] via definitions.
    defs = {}
    for m in REFDEF_RE.finditer(masked):
        defs[m.group(1).strip().lower()] = m.group(2)
    for m in REFLINK_RE.finditer(masked):
        bang, text, label = m.group(1), m.group(2), m.group(3)
        url = defs.get((label or text).strip().lower())
        if not url or url.startswith("#"):
            continue
        yield from emit(url, "image" if bang else "link")
    for m in AUTOLINK_RE.finditer(masked):
        yield from emit(m.group(1), "autolink")
    for m in BARE_URL_RE.finditer(masked):
        url = _strip_bare_url(m.group(1))
        yield from emit(url, "bare")


def check_local(url, base_dir, root_dir=None):
    """Check a local (file) link. Returns None if OK, else a reason string.

    `base_dir` is the markdown file's directory (for relative links);
    `root_dir` is the scan root (for "/root/relative" links). URL-encoded
    characters (%20 etc.) are decoded before checking.
    """
    path = urllib.parse.unquote(url.split("#", 1)[0])
    if not path:  # pure anchor into this file: always OK
        return None
    if path.startswith("/"):
        base = root_dir if root_dir else base_dir
        full = os.path.normpath(os.path.join(base, path.lstrip("/")))
    else:
        full = os.path.normpath(os.path.join(base_dir, path))
    if not os.path.exists(full):
        return "missing file"
    return None


def _check_http_method(url, method, opener=None):
    req = urllib.request.Request(url, method=method,
                                 headers={"User-Agent": "md-link-check/1.0"})
    try:
        with (opener or urllib.request).urlopen(req, timeout=TIMEOUT) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:  # DNS failure, timeout, refused, SSL, ...
        return "%s: %s" % (type(e).__name__, e)
    if code >= 400:
        return "HTTP %d" % code
    return None


def check_http(url, opener=None):
    """HEAD the URL, falling back to GET when the server refuses HEAD
    (403/405 are the common "HEAD not supported" signals). Returns None
    if OK, else a reason string."""
    reason = _check_http_method(url, "HEAD", opener=opener)
    if reason in ("HTTP 403", "HTTP 405"):
        reason = _check_http_method(url, "GET", opener=opener)
    return reason


def scan_files(paths, no_network=False, opener=None):
    """Scan files/dirs. Returns (results, n_checked) where results maps
    file -> list of (url, kind, reason)."""
    files = []  # (path, scan_root): scan_root resolves "/root/relative" links
    for p in paths:
        ap = os.path.abspath(p)
        if os.path.isdir(ap):
            for root, _, names in os.walk(ap):
                files.extend((os.path.join(root, n), ap) for n in names
                             if n.endswith(".md"))
        elif os.path.isfile(ap):
            files.append((ap, os.path.dirname(ap)))
        else:
            print("warning: %s: no such file or directory" % p,
                  file=sys.stderr)
    files.sort()

    http_cache = {}
    results = {}
    n_checked = 0
    for path, scan_root in files:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        base_dir = os.path.dirname(path)
        broken = []
        for url, kind in extract_links(text):
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme in ("http", "https") and no_network:
                continue
            n_checked += 1
            if parsed.scheme in ("http", "https"):
                if no_network:
                    continue
                if url not in http_cache:
                    http_cache[url] = check_http(url, opener=opener)
                reason = http_cache[url]
            elif parsed.scheme:  # mailto:, ftp:, tel:, ... : out of scope
                continue
            else:
                reason = check_local(url, base_dir, scan_root)
            if reason:
                broken.append({"url": url, "kind": kind, "reason": reason})
        results[path] = broken
    return results, n_checked


def format_text(results, n_checked, n_broken):
    lines = []
    for path in sorted(results):
        broken = results[path]
        if not broken:
            continue
        lines.append(path)
        for b in broken:
            tag = "BROKEN (img)" if b["kind"] == "image" else "BROKEN"
            lines.append("  %s %s  -- %s" % (tag, b["url"], b["reason"]))
    lines.append("%d checked, %d broken" % (n_checked, n_broken))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="md-link-check",
        description="Find broken links in markdown files.")
    ap.add_argument("paths", nargs="+",
                    help="markdown files or directories (recursive)")
    ap.add_argument("--no-network", action="store_true",
                    help="skip http(s) link checks")
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON")
    args = ap.parse_args(argv)

    results, n_checked = scan_files(args.paths, no_network=args.no_network)
    n_broken = sum(len(v) for v in results.values())

    if args.json:
        payload = {
            "checked": n_checked,
            "broken": n_broken,
            "files": {
                path: [{"url": b["url"], "kind": b["kind"],
                        "reason": b["reason"]} for b in broken]
                for path, broken in sorted(results.items())
            },
        }
        print(json.dumps(payload, indent=2))
    else:
        print(format_text(results, n_checked, n_broken))

    return 1 if n_broken else 0


if __name__ == "__main__":
    sys.exit(main())
