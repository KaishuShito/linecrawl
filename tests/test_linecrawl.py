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

            stats = json.loads(run_cli("--db", str(db), "--json", "stats").stdout)
            self.assertEqual(stats["stats"]["messages"], 4)

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


if __name__ == "__main__":
    unittest.main()
