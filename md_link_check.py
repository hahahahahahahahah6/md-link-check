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

# Matches [text](url) and ![alt](src); we keep the leading "!" to tell them apart.
LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
# Matches <https://example.com> autolinks.
AUTOLINK_RE = re.compile(r"<(https?://[^>\s]+)>")
# Matches bare http(s) URLs not inside markup.
BARE_URL_RE = re.compile(r"(?<![\]\(<\"'`=])(https?://[^\s<>\]\"'`)]+)")

KINDS = ("link", "image", "autolink", "bare")


def extract_links(text):
    """Yield (url, kind) tuples from markdown text. Images are kind 'image'."""
    seen = set()
    # Mask out fenced code blocks and inline code so we don't flag examples.
    def mask(m):
        return "\n" * m.group(0).count("\n")

    masked = re.sub(r"```.*?```", mask, text, flags=re.DOTALL)
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
    for m in AUTOLINK_RE.finditer(masked):
        yield from emit(m.group(1), "autolink")
    for m in BARE_URL_RE.finditer(masked):
        url = m.group(1).rstrip(".,;:!?")
        yield from emit(url, "bare")


def check_local(url, base_dir):
    """Check a local (file) link. Returns None if OK, else a reason string."""
    path = url.split("#", 1)[0]
    if not path:  # pure anchor into this file: always OK
        return None
    full = os.path.normpath(os.path.join(base_dir, path))
    if not os.path.exists(full):
        return "missing file"
    return None


def check_http(url, opener=None):
    """HEAD the URL. Returns None if OK, else a reason string."""
    req = urllib.request.Request(url, method="HEAD",
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


def scan_files(paths, no_network=False, opener=None):
    """Scan files/dirs. Returns (results, n_checked) where results maps
    file -> list of (url, kind, reason)."""
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, names in os.walk(p):
                files.extend(os.path.join(root, n) for n in names
                             if n.endswith(".md"))
        elif os.path.isfile(p):
            files.append(p)
        else:
            print("warning: %s: no such file or directory" % p,
                  file=sys.stderr)
    files.sort()

    http_cache = {}
    results = {}
    n_checked = 0
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        base_dir = os.path.dirname(os.path.abspath(path))
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
                reason = check_local(url, base_dir)
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
