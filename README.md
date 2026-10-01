# md-link-check

Find broken links in your markdown before your readers do.

Launch day. Your README is live, the demo link everyone clicks first goes
nowhere — the repo moved, the docs page got renamed, nobody noticed during
review. Link rot is quiet until it isn't.

`md-link-check` is a fast, local-first link checker for markdown. Point it at
your README and docs folder, and it tells you which links are dead: local
file links, image sources, autolinks, and bare URLs — before you ship.

## Install

No dependencies, Python 3.9+ only:

```bash
curl -O https://raw.githubusercontent.com/hahahahahahahahah6/md-link-check/main/md_link_check.py
chmod +x md_link_check.py
```

Or clone the repo:

```bash
git clone https://github.com/hahahahahahahahah6/md-link-check.git
```

## Usage

```bash
md-link-check README.md docs/
```

Scans files recursively for directories (`*.md`). Example output:

```
$ python3 md_link_check.py README.md docs/
README.md
  BROKEN https://example.com/demo  -- HTTP 404
  BROKEN (img) assets/screenshot.png  -- missing file
docs/setup.md
  BROKEN ../old-install.md  -- missing file
24 checked, 3 broken
```

Exit code is `0` when everything is fine, `1` when anything is broken — so it
drops straight into CI:

```bash
python3 md_link_check.py README.md docs/ || echo "dead links found"
```

Options:

- `--no-network` — skip http(s) checks, verify local links only. Made for
  sandboxes and offline CI.
- `--json` — machine-readable output: `{"checked": 24, "broken": 3, "files": {...}}`.

What it checks:

- `[text](url)` links and `![alt](src)` image sources (images are flagged
  separately as `BROKEN (img)`)
- `<https://...>` autolinks and bare `https://...` URLs
- Local links resolve relative to each markdown file; `file.md#anchor` only
  verifies the file exists. `#anchor`-only links are skipped.
- http(s) links get a `HEAD` request with a 10s timeout, following redirects;
  status `>= 400` or any network error means broken. Identical URLs are
  checked once.
- Links inside code blocks and inline code are ignored — examples in your
  docs won't trip the checker.

## Why not a crawler?

Crawlers are built to walk the whole web. `md-link-check` is built for the
README/docs workflow: it checks *your* markdown, locally, in milliseconds.
Local links are verified against the filesystem, remote links get one HEAD
request each, and `--no-network` gives you a pure offline pass for sandboxed
build environments. One file, stdlib only, no install step, no daemon.

## License

MIT — see [LICENSE](LICENSE).
