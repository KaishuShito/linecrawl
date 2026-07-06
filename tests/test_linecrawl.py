import base64
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "linecrawl.py"
FIXTURES = ROOT / "tests" / "fixtures"


def run_cli(*args, check=True):
    result = subprocess.run(
        ["python3", str(CLI), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed: {result.args}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


class LinecrawlCliTests(unittest.TestCase):
    def test_web_chrome_paths_do_not_activate_or_reposition_chrome(self):
        source = CLI.read_text(encoding="utf-8")
        web_region = source[
            source.index("LINE_WEB_MEDIA_FETCH_JS") : source.index("def upsert_chat")
        ]
        forbidden = ("activate", "set index", "set bounds", "front window", "active tab")
        for token in forbidden:
            self.assertNotIn(token, web_region)
        self.assertIn('"ax"', source)

    def test_save_chat_import_is_idempotent_and_searchable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "linecrawl.db"
            sample = FIXTURES / "sample-save-chat.txt"

            first = run_cli("--db", str(db), "--json", "import", str(sample))
            self.assertEqual(json.loads(first.stdout)["results"][0]["status"], "imported")

            second = run_cli("--db", str(db), "--json", "import", str(sample))
            self.assertEqual(json.loads(second.stdout)["results"][0]["status"], "unchanged")

            search = run_cli("--db", str(db), "--json", "search", "ありがとう")
            self.assertEqual(len(json.loads(search.stdout)["results"]), 1)

            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from messages").fetchone()[0], 4)
                self.assertEqual(conn.execute("select count(*) from source_files").fetchone()[0], 1)

    def test_force_reimport_replaces_changed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "linecrawl.db"
            source = Path(tmp) / "[LINE]Fixture.txt"
            source.write_text((FIXTURES / "sample-save-chat.txt").read_text(), encoding="utf-8")
            run_cli("--db", str(db), "--json", "import", str(source))

            source.write_text((FIXTURES / "sample-save-chat-updated.txt").read_text(), encoding="utf-8")
            updated = run_cli("--db", str(db), "--json", "import", str(source))
            payload = json.loads(updated.stdout)
            self.assertEqual(payload["results"][0]["status"], "imported")
            self.assertEqual(payload["results"][0]["messages"], 5)

            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from messages").fetchone()[0], 5)

    def test_doctor_and_stats_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "linecrawl.db"
            run_cli("--db", str(db), "--json", "import", str(FIXTURES / "sample-save-chat.txt"))

            doctor = json.loads(run_cli("--db", str(db), "--json", "doctor").stdout)
            self.assertTrue(doctor["ok"])
            self.assertFalse(doctor["auth_required"])
            self.assertEqual(doctor["messages"], 4)
            self.assertNotIn("explain", doctor)

            explained = json.loads(run_cli("--db", str(db), "--json", "doctor", "--explain").stdout)
            self.assertEqual(explained["messages"], 4)
            self.assertIn("explain", explained)
            self.assertIn("media_full", explained["explain"])
            self.assertIn("guide", explained)
            self.assertIn("first_run", explained["guide"])
            self.assertIn("line_web_prerequisites", explained["guide"])

            stats = json.loads(run_cli("--db", str(db), "--json", "stats").stdout)
            self.assertEqual(stats["stats"]["messages"], 4)

    def test_help_surfaces_line_web_import_and_doctor_guidance(self):
        root_help = run_cli("--help").stdout
        self.assertIn("LINE Web Chrome", root_help)
        self.assertIn("web-import-current --scroll-steps 5", root_help)
        self.assertIn("--json is a global flag", root_help)

        web_help = run_cli("web-import-current", "--help").stdout
        self.assertIn("Image/sticker capture is ON by default", web_help)
        self.assertIn("--no-media", web_help)
        self.assertIn("--full-media", web_help)
        self.assertIn("opens each image viewer", web_help)

        doctor_help = run_cli("doctor", "--help").stdout
        self.assertIn("media pipeline", doctor_help)
        self.assertIn("--explain", doctor_help)
        self.assertIn("linecrawl --json doctor --explain", doctor_help)

        web_doctor_help = run_cli("web-doctor", "--help").stdout
        self.assertIn("Allow JavaScript from Apple Events", web_doctor_help)
        self.assertIn("--chrome-profile-root", web_doctor_help)

        launchd_web_help = run_cli("launchd-install-web", "--help").stdout
        self.assertIn("com.linecrawl.webwatch", launchd_web_help)
        self.assertIn("ongoing local", launchd_web_help)

    def test_all_subcommand_help_pages_render_without_tracebacks(self):
        subcommands = [
            "import",
            "import-downloads",
            "desktop-save-current",
            "web-dump-current",
            "web-import-current",
            "web-chats",
            "web-import-all",
            "web-watch-current",
            "web-import-json",
            "web-doctor",
            "web-dump-js",
            "chats",
            "search",
            "messages",
            "media",
            "sql",
            "doctor",
            "stats",
            "watch",
            "launchd-install",
            "launchd-install-web",
            "launchd-status",
            "launchd-uninstall",
            "edb-doctor",
            "edb-import",
        ]
        for subcommand in subcommands:
            with self.subTest(subcommand=subcommand):
                result = run_cli(subcommand, "--help")
                self.assertIn("usage: linecrawl", result.stdout)
                self.assertNotIn("Traceback", result.stdout + result.stderr)

        search_help = run_cli("search", "--help").stdout
        self.assertIn("'%Podcast%'", search_help)
        self.assertIn("--chat '%Family%'", search_help)

    def test_import_downloads_matches_line_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "linecrawl.db"
            downloads = root / "Downloads"
            downloads.mkdir()
            (downloads / "[LINE]Fixture.txt").write_text(
                (FIXTURES / "sample-save-chat.txt").read_text(), encoding="utf-8"
            )
            (downloads / "not-line.txt").write_text("ignore", encoding="utf-8")

            result = run_cli(
                "--db",
                str(db),
                "--json",
                "import-downloads",
                "--downloads",
                str(downloads),
            )
            payload = json.loads(result.stdout)
            self.assertEqual(len(payload["results"]), 1)
            self.assertEqual(payload["results"][0]["chat"], "Fixture")

    def test_web_import_json_is_idempotent_and_searchable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "linecrawl.db"
            sample = FIXTURES / "sample-line-web-dump.json"

            first = run_cli("--db", str(db), "--json", "web-import-json", str(sample))
            payload = json.loads(first.stdout)
            self.assertEqual(payload["import"]["status"], "imported")
            self.assertEqual(payload["import"]["messages"], 2)
            self.assertEqual(payload["import"]["chat"], "ExampleChat")

            second = run_cli("--db", str(db), "--json", "web-import-json", str(sample))
            self.assertEqual(json.loads(second.stdout)["import"]["status"], "unchanged")

            search = run_cli("--db", str(db), "--json", "search", "さすが")
            self.assertEqual(len(json.loads(search.stdout)["results"]), 1)

            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from messages").fetchone()[0], 2)
                self.assertEqual(conn.execute("select count(*) from source_files").fetchone()[0], 1)

    def test_web_import_json_saves_media_and_links_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "linecrawl.db"
            sample = FIXTURES / "sample-line-web-dump-with-media.json"

            first = run_cli("--db", str(db), "--json", "web-import-json", str(sample))
            payload = json.loads(first.stdout)
            self.assertEqual(payload["import"]["status"], "imported")
            # 1 text + 2 decodable images; the data-less image row is skipped.
            self.assertEqual(payload["import"]["messages"], 3)
            self.assertEqual(payload["import"]["media"], 2)

            media_dir = Path(tmp) / "media" / "ExampleChat"
            files = sorted(media_dir.glob("*.png"))
            self.assertEqual(len(files), 2)
            for path in files:
                self.assertTrue(path.read_bytes().startswith(b"\x89PNG"))

            messages = json.loads(
                run_cli("--db", str(db), "--json", "messages", "--chat", "%ExampleChat%").stdout
            )["messages"]
            photo_messages = [m for m in messages if m["content"] == "[Photo]"]
            self.assertEqual(len(photo_messages), 2)
            for message in photo_messages:
                self.assertEqual(len(message["media"]), 1)
                media_path = Path(message["media"][0]["path"])
                self.assertTrue(media_path.is_absolute())
                self.assertTrue(media_path.exists())

            listing = json.loads(
                run_cli("--db", str(db), "--json", "media", "--chat", "%ExampleChat%").stdout
            )["media"]
            self.assertEqual(len(listing), 2)
            self.assertTrue(all(Path(m["path"]).exists() for m in listing))

    def test_web_media_reimport_with_new_blob_urls_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "linecrawl.db"
            sample = FIXTURES / "sample-line-web-dump-with-media.json"
            run_cli("--db", str(db), "--json", "web-import-json", str(sample))

            # Same images arrive again under fresh session blob URLs.
            payload = json.loads(sample.read_text(encoding="utf-8"))
            for item in payload["messages"]:
                if item.get("kind") == "image" and item.get("src"):
                    item["src"] = item["src"].replace("1111", "9999").replace("2222", "8888")
                    item["id"] = f"image:{item['direction']}:{item['src']}"
            updated = root / "updated-dump.json"
            updated.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            run_cli("--db", str(db), "--json", "web-import-json", str(updated))

            with sqlite3.connect(db) as conn:
                self.assertEqual(
                    conn.execute("select count(*) from messages where content='[Photo]'").fetchone()[0], 2
                )
                self.assertEqual(conn.execute("select count(*) from media").fetchone()[0], 2)
            self.assertEqual(len(list((root / "media" / "ExampleChat").glob("*.png"))), 2)

    def test_full_media_import_prefers_full_resolution_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "linecrawl.db"
            sample = FIXTURES / "sample-line-web-dump-with-full-media.json"
            fixture = json.loads(sample.read_text(encoding="utf-8"))
            full_bytes = base64.b64decode(fixture["messages"][2]["full_data"].split(",", 1)[1])

            payload = json.loads(run_cli("--db", str(db), "--json", "web-import-json", str(sample)).stdout)
            self.assertEqual(payload["import"]["messages"], 3)
            self.assertEqual(payload["import"]["media"], 2)

            media = json.loads(run_cli("--db", str(db), "--json", "media", "--chat", "%ExampleChat%").stdout)["media"]
            by_quality = {m["quality"]: m for m in media}
            self.assertEqual(set(by_quality), {"full", "thumbnail"})
            self.assertEqual(Path(by_quality["full"]["path"]).read_bytes(), full_bytes)
            self.assertEqual((by_quality["full"]["width"], by_quality["full"]["height"]), (2, 2))

            doctor = json.loads(run_cli("--db", str(db), "--json", "doctor").stdout)
            self.assertEqual(doctor["media"], 2)
            self.assertEqual(doctor["media_full"], 1)

    def test_full_media_upgrades_existing_thumbnail_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "linecrawl.db"
            run_cli("--db", str(db), "--json", "web-import-json", str(FIXTURES / "sample-line-web-dump-with-media.json"))
            run_cli("--db", str(db), "--json", "web-import-json", str(FIXTURES / "sample-line-web-dump-with-full-media.json"))

            with sqlite3.connect(db) as conn:
                self.assertEqual(
                    conn.execute("select count(*) from messages where content='[Photo]'").fetchone()[0], 2
                )
                rows = conn.execute("select quality, path from media order by quality").fetchall()
            self.assertEqual([r[0] for r in rows], ["full", "thumbnail"])
            # The upgraded row replaced its thumbnail file with the full-resolution one.
            self.assertEqual(len(list((Path(tmp) / "media" / "ExampleChat").glob("*.png"))), 2)
            for _quality, path in rows:
                self.assertTrue(Path(path).exists())

    def test_thumbnail_reimport_does_not_downgrade_full_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "linecrawl.db"
            run_cli("--db", str(db), "--json", "web-import-json", str(FIXTURES / "sample-line-web-dump-with-full-media.json"))
            run_cli("--db", str(db), "--json", "web-import-json", str(FIXTURES / "sample-line-web-dump-with-media.json"))

            with sqlite3.connect(db) as conn:
                qualities = [r[0] for r in conn.execute("select quality from media order by quality")]
            self.assertEqual(qualities, ["full", "thumbnail"])

    def test_doctor_reports_media_pipeline_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "linecrawl.db"
            run_cli("--db", str(db), "--json", "web-import-json", str(FIXTURES / "sample-line-web-dump-with-media.json"))

            doctor = json.loads(run_cli("--db", str(db), "--json", "doctor").stdout)
            self.assertEqual(doctor["media"], 2)
            self.assertEqual(doctor["media_files_missing"], 0)
            self.assertEqual(doctor["media_dir"], str(Path(tmp) / "media"))
            self.assertNotEqual(doctor["media_latest_captured"], "")
            self.assertIn("web_watch_running", doctor)
            self.assertIn("web_watch_launchd_loaded", doctor)

    def test_web_import_json_accumulates_changed_dumps_for_same_chat_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "linecrawl.db"
            sample = FIXTURES / "sample-line-web-dump.json"
            updated = root / "sample-line-web-dump-updated.json"
            payload = json.loads(sample.read_text(encoding="utf-8"))
            payload["messages"].append(
                {
                    "kind": "message",
                    "id": "incoming:11:00 PM:追加テスト",
                    "direction": "incoming",
                    "time": "11:00 PM",
                    "date_label": "Today",
                    "content": "追加テスト message",
                    "top": 500,
                    "left": 300,
                }
            )
            updated.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            run_cli("--db", str(db), "--json", "web-import-json", str(sample))
            run_cli("--db", str(db), "--json", "web-import-json", str(updated))

            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from messages").fetchone()[0], 3)
                self.assertEqual(conn.execute("select count(*) from source_files").fetchone()[0], 2)


class LineWebAllChatsTests(unittest.TestCase):
    """In-process tests for sidebar enumeration and the all-chats crawl.

    Browser JavaScript cannot run here, so these fake line_web_execute_js /
    line_web_dump and verify the Python orchestration around them.
    """

    def setUp(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("linecrawl_module", CLI)
        self.lc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.lc)

    def fake_sidebar_executor(self, total=30, page=12, row_height=71):
        """Simulate the virtualized sidebar: only rows near the current scroll
        position are rendered, and the state machine mirrors the browser-side
        seen-accumulation of LINE_WEB_CHATLIST_JS."""
        state = {"scroll": 0, "seen": {}, "calls": []}
        client_height = page * row_height
        scroll_height = total * row_height

        def executor(js, method="auto", debug_url=None):
            import re

            mode = re.search(r'__linecrawlChatMode="(\w+)"', js).group(1)
            state["calls"].append(mode)
            if mode == "reset":
                state["seen"] = {}
            first = state["scroll"] // row_height
            for i in range(first, min(total, first + page + 2)):
                mid = f"mid-{i:03d}"
                state["seen"][mid] = {
                    "mid": mid,
                    "name": f"chat {i:03d}",
                    "unread": 5 if i == 1 else 0,
                    "order": i * row_height,
                    "current": i == 0,
                    "preview": "",
                }
            if mode == "scroll":
                state["scroll"] = min(state["scroll"] + int(client_height * 0.85), scroll_height - client_height)
            if mode == "restore":
                state["scroll"] = 0
            return json.dumps(
                {
                    "ok": True,
                    "current_mid": "mid-000",
                    "at_bottom": state["scroll"] + client_height >= scroll_height - 4,
                    "chats": sorted(state["seen"].values(), key=lambda c: c["order"]),
                }
            )

        return executor, state

    def test_list_chats_scrolls_virtualized_sidebar_and_restores(self):
        executor, state = self.fake_sidebar_executor(total=30, page=12)
        original = self.lc.line_web_execute_js
        self.lc.line_web_execute_js = executor
        try:
            listing = self.lc.line_web_list_chats(method="applescript")
        finally:
            self.lc.line_web_execute_js = original
        self.assertEqual(len(listing["chats"]), 30)
        self.assertEqual(listing["current_mid"], "mid-000")
        self.assertEqual(state["calls"][0], "reset")
        self.assertEqual(state["calls"][-1], "restore")
        self.assertEqual(state["scroll"], 0)

    def test_web_import_all_skips_unread_and_restores_original_chat(self):
        import contextlib
        import io

        opened = []

        def fake_listing(method="auto", debug_url=None, max_scroll_pages=100):
            return {
                "ok": True,
                "current_mid": "mid-current",
                "chats": [
                    {"mid": "mid-current", "name": "open chat", "unread": 0, "order": 0, "current": True},
                    {"mid": "mid-unread", "name": "unread chat", "unread": 7, "order": 71, "current": False},
                    {"mid": "mid-b", "name": "サイドバー名", "unread": 0, "order": 142, "current": False},
                ],
            }

        def fake_open(mid, method="auto", debug_url=None, timeout=8.0, settle=1.0):
            opened.append(mid)

        def fake_dump(method="auto", debug_url=None, scroll_steps=0, with_media=False, full_media=False):
            mid = opened[-1]
            return {
                "ok": True,
                "url": f"chrome-extension://x/index.html#/chats/{mid}",
                "chat_name": "heuristic header",
                "extracted_at": "2026-07-06T00:00:00Z",
                "messages": [
                    {
                        "kind": "message",
                        "id": f"incoming:10:00:hello {mid}",
                        "direction": "incoming",
                        "time": "10:00",
                        "date_label": "Today",
                        "content": f"hello {mid}",
                        "top": 100,
                        "left": 300,
                    }
                ],
            }

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "linecrawl.db"
            originals = (
                self.lc.line_web_list_chats,
                self.lc.line_web_open_chat,
                self.lc.line_web_dump,
                self.lc.resolve_web_method,
            )
            self.lc.line_web_list_chats = fake_listing
            self.lc.line_web_open_chat = fake_open
            self.lc.line_web_dump = fake_dump
            self.lc.resolve_web_method = lambda method="auto", debug_url=None: "applescript"
            stdout = io.StringIO()
            try:
                with contextlib.redirect_stdout(stdout):
                    code = self.lc.main(
                        ["--db", str(db), "--json", "web-import-all", "--delay", "0", "--no-media"]
                    )
            finally:
                (
                    self.lc.line_web_list_chats,
                    self.lc.line_web_open_chat,
                    self.lc.line_web_dump,
                    self.lc.resolve_web_method,
                ) = originals

            self.assertEqual(code, 0)
            summary = json.loads(stdout.getvalue())
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["chats_crawled"], 2)
            self.assertEqual([c["mid"] for c in summary["skipped_unread"]], ["mid-unread"])
            # every crawled chat was opened, and the originally open chat was restored last
            self.assertEqual(opened, ["mid-current", "mid-b", "mid-current"])

            with sqlite3.connect(db) as conn:
                chat_names = {row[0] for row in conn.execute("select name from chats")}
                # sidebar names win over the in-page header heuristic
                self.assertIn("サイドバー名", chat_names)
                self.assertIn("open chat", chat_names)
                self.assertNotIn("heuristic header", chat_names)
                self.assertNotIn("unread chat", chat_names)
                count = conn.execute("select count(*) from messages").fetchone()[0]
                self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
