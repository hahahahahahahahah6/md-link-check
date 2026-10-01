"""Smoke tests for md-link-check. No external network: the http path is
tested against a localhost http.server running in a thread."""

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import md_link_check


class Handler(BaseHTTPRequestHandler):
    def _serve(self):
        if self.path == "/ok":
            self.send_response(200)
        elif self.path == "/missing":
            self.send_response(404)
        elif self.path == "/head-forbidden":
            # Some hosts reject HEAD: succeed only on GET.
            self.send_response(403 if self.command == "HEAD" else 200)
        elif self.path == "/head-unsupported":
            self.send_response(405 if self.command == "HEAD" else 200)
        else:
            self.send_response(500)
        self.end_headers()

    do_HEAD = _serve
    do_GET = _serve

    def log_message(self, *args):
        pass


class TestMdLinkCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = f"http://127.0.0.1:{self.port}"

    def write(self, name, text):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def run_cli(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = md_link_check.main(list(args))
        return code, buf.getvalue()

    def test_good_local_link_passes(self):
        self.write("other.md", "# other\n")
        md = self.write("README.md", "See [other](other.md).\n")
        results, n = md_link_check.scan_files([md], no_network=True)
        self.assertEqual(results[md], [])
        self.assertEqual(n, 1)

    def test_missing_local_file_flagged(self):
        md = self.write("README.md", "See [gone](nope.md).\n")
        results, _ = md_link_check.scan_files([md], no_network=True)
        self.assertEqual(len(results[md]), 1)
        self.assertEqual(results[md][0]["reason"], "missing file")
        code, out = self.run_cli(md, "--no-network")
        self.assertEqual(code, 1)
        self.assertIn("BROKEN", out)
        self.assertIn("1 checked, 1 broken", out)

    def test_anchor_only_skipped(self):
        md = self.write("README.md", "Jump [up](#top).\n")
        results, n = md_link_check.scan_files([md], no_network=True)
        self.assertEqual(results[md], [])
        self.assertEqual(n, 0)

    def test_image_src_checked_separately(self):
        self.write("logo.png", "fake")
        md = self.write(
            "README.md",
            "![logo](logo.png)\n![missing](gone.png)\n")
        results, _ = md_link_check.scan_files([md], no_network=True)
        broken = results[md]
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0]["kind"], "image")
        self.assertEqual(broken[0]["url"], "gone.png")
        code, out = self.run_cli(md, "--no-network")
        self.assertIn("BROKEN (img)", out)

    def test_http_ok_and_404(self):
        md = self.write(
            "README.md",
            "good [ok](%s/ok)\nbad [no](%s/missing)\n" % (self.base, self.base))
        results, n = md_link_check.scan_files([md])
        self.assertEqual(n, 2)
        broken = results[md]
        self.assertEqual(len(broken), 1)
        self.assertIn("404", broken[0]["reason"])
        code, out = self.run_cli(md)
        self.assertEqual(code, 1)

    def test_autolink_and_bare_url(self):
        md = self.write(
            "README.md",
            "visit <%s/missing> or %s/missing today\n" % (self.base, self.base))
        results, n = md_link_check.scan_files([md])
        broken = results[md]
        self.assertEqual(len(broken), 1)  # deduped: checked once
        kinds = {b["kind"] for b in broken}
        self.assertTrue({"autolink", "bare"} & kinds)

    def test_no_network_skips_http(self):
        md = self.write(
            "README.md", "[x](http://192.0.2.1/unreachable)\n")
        code, out = self.run_cli(md, "--no-network")
        self.assertEqual(code, 0)
        self.assertIn("0 checked, 0 broken", out)

    def test_json_valid(self):
        md = self.write("README.md", "[gone](nope.md)\n")
        code, out = self.run_cli(md, "--no-network", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["checked"], 1)
        self.assertEqual(payload["broken"], 1)
        self.assertEqual(payload["files"][md][0]["url"], "nope.md")

    def test_directory_recursive(self):
        sub = os.path.join(self.tmp.name, "docs")
        os.makedirs(sub)
        self.write("docs/a.md", "[gone](nope.md)\n")
        self.write("README.md", "# hi\n")
        results, _ = md_link_check.scan_files([self.tmp.name],
                                              no_network=True)
        md_files = sorted(results)
        self.assertEqual(len(md_files), 2)
        self.assertEqual(len(results[os.path.join(sub, "a.md")]), 1)

    def test_code_blocks_ignored(self):
        md = self.write(
            "README.md",
            "```\n[broken](nope.md)\n```\n`[also](nope2.md)`\n")
        results, n = md_link_check.scan_files([md], no_network=True)
        self.assertEqual(results[md], [])
        self.assertEqual(n, 0)

    def test_head_403_falls_back_to_get(self):
        md = self.write("README.md",
                        f"[x]({self.base}/head-forbidden)\n")
        results, n = md_link_check.scan_files([md])
        self.assertEqual(results[md], [])
        self.assertEqual(n, 1)

    def test_head_405_falls_back_to_get(self):
        md = self.write("README.md",
                        f"[x]({self.base}/head-unsupported)\n")
        results, n = md_link_check.scan_files([md])
        self.assertEqual(results[md], [])
        self.assertEqual(n, 1)

    def test_real_404_not_rescued_by_get(self):
        md = self.write("README.md", f"[x]({self.base}/missing)\n")
        results, _ = md_link_check.scan_files([md])
        self.assertEqual(len(results[md]), 1)
        self.assertEqual(results[md][0]["reason"], "HTTP 404")

    def test_url_with_balanced_parens(self):
        links = list(md_link_check.extract_links(
            "[w](https://en.wikipedia.org/wiki/Python_(programming_language))\n"))
        self.assertEqual(
            links,
            [("https://en.wikipedia.org/wiki/Python_(programming_language)",
              "link")])

    def test_bare_url_trailing_paren_stripped(self):
        links = dict(md_link_check.extract_links(
            "see (https://example.com/a) and https://example.com/b_(c).\n"))
        self.assertIn("https://example.com/a", links)
        self.assertIn("https://example.com/b_(c)", links)

    def test_root_relative_link_resolves_to_scan_root(self):
        os.makedirs(os.path.join(self.tmp.name, "docs"))
        self.write("docs/a.md", "# a\n")
        md = self.write("README.md", "[a](/docs/a.md)\n")
        results, n = md_link_check.scan_files([self.tmp.name],
                                              no_network=True)
        self.assertEqual(results[md], [])
        self.assertEqual(n, 1)

    def test_root_relative_missing_flagged(self):
        md = self.write("README.md", "[a](/docs/nope.md)\n")
        results, _ = md_link_check.scan_files([self.tmp.name],
                                              no_network=True)
        self.assertEqual(len(results[md]), 1)

    def test_percent_encoded_local_path(self):
        self.write("my file.md", "x\n")
        md = self.write("README.md", "[f](my%20file.md)\n")
        results, n = md_link_check.scan_files([md], no_network=True)
        self.assertEqual(results[md], [])
        self.assertEqual(n, 1)

    def test_tilde_fence_ignored(self):
        md = self.write("README.md", "~~~\n[broken](nope.md)\n~~~\n")
        results, n = md_link_check.scan_files([md], no_network=True)
        self.assertEqual(results[md], [])
        self.assertEqual(n, 0)

    def test_indented_code_block_ignored(self):
        md = self.write("README.md", "\n    [broken](nope.md)\n")
        results, n = md_link_check.scan_files([md], no_network=True)
        self.assertEqual(results[md], [])
        self.assertEqual(n, 0)

    def test_indented_after_paragraph_not_code(self):
        # A 4-space-indented line directly under paragraph text is a lazy
        # continuation (CommonMark), so the link is still checked.
        md = self.write("README.md", "para\n    [gone](nope.md)\n")
        results, n = md_link_check.scan_files([md], no_network=True)
        self.assertEqual(len(results[md]), 1)
        self.assertEqual(n, 1)

    def test_reference_style_links(self):
        md = self.write(
            "README.md",
            "[x][ref] and [y][] and ![i][img]\n"
            "\n"
            "[ref]: https://example.com/ok\n"
            "[y]: other.md\n"
            "[img]: pic.png\n")
        self.write("other.md", "# o\n")
        self.write("pic.png", "x")
        results, n = md_link_check.scan_files([md], no_network=True)
        self.assertEqual(results[md], [])
        # [x][ref] is skipped (network), [y][] and ![i][img] are checked
        self.assertEqual(n, 2)

    def test_reference_link_case_insensitive(self):
        md = self.write("README.md", "[x][REF]\n\n[ref]: other.md\n")
        self.write("other.md", "# o\n")
        results, _ = md_link_check.scan_files([md], no_network=True)
        self.assertEqual(results[md], [])


if __name__ == "__main__":
    unittest.main()
