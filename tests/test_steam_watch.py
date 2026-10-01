import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from steam_watch import Snapshot, Tracker, find_manifest, load_config, parse_vdf, read_snapshot, save_state, load_state, notify, DELIVERED, main


def snapshot(build="100", target="0", flags=4, downloaded=0, branch="public"):
    return Snapshot(build, target, flags, downloaded, branch)


class WatchTests(unittest.TestCase):
    def test_pending_requires_new_target_not_repair_or_cached_target(self):
        for current in [snapshot(flags=6), snapshot(target="100", flags=6),
                        snapshot(target="200", flags=4)]:
            self.assertEqual(Tracker().observe(current), [])
        self.assertTrue(Tracker().observe(snapshot(target="200", flags=6))[0][0].startswith("queued:"))

    def test_download_requires_live_bytes_not_running_flag(self):
        tracker = Tracker()
        queued = snapshot(target="200", flags=6, downloaded=50)
        events = tracker.observe(queued)
        tracker.sent.update(key for key, _ in events)
        self.assertEqual(tracker.observe(queued), [])
        self.assertEqual(tracker.observe(snapshot(target="200", flags=262, downloaded=50)), [])
        events = tracker.observe(snapshot(target="200", flags=262, downloaded=51))
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0][0].startswith("download:"))

    def test_target_switch_and_counter_reset_do_not_confirm_download(self):
        tracker = Tracker(snapshot(target="200", flags=6, downloaded=100))
        events = tracker.observe(snapshot(target="300", flags=6, downloaded=200))
        self.assertFalse(any(key.startswith("download:") for key, _ in events))
        events = tracker.observe(snapshot(target="300", flags=6, downloaded=0))
        self.assertFalse(any(key.startswith("download:") for key, _ in events))

    def test_install_detects_fast_update_and_ignores_branch_switch(self):
        events = Tracker(snapshot()).observe(snapshot(build="200"))
        self.assertTrue(events[0][0].startswith("installed:"))
        self.assertEqual(Tracker(snapshot()).observe(snapshot(build="200", branch="beta")), [])

    def test_restart_deduplicates_but_new_build_notifies(self):
        current = snapshot(target="200", flags=6)
        tracker = Tracker()
        keys = {key for key, _ in tracker.observe(current)}
        restarted = Tracker(sent=keys)
        self.assertEqual(restarted.observe(current), [])
        self.assertTrue(restarted.observe(snapshot(target="300", flags=6)))

    def test_vdf_nested_escapes_comments_and_truncation(self):
        data = parse_vdf('"root" { // comment\n "path" "C:\\\\Steam" "nested" { "x" "y" }}')
        self.assertEqual(data["root"]["path"], 'C:\\Steam')
        self.assertEqual(data["root"]["nested"], {"x": "y"})
        for bad in ['"root" { "x" "y"', '"x"', '}', '"root" { "x" }', '"root" { "x" "unterminated }']:
            with self.assertRaises(ValueError):
                parse_vdf(bad)

    def test_discover_external_library_and_read_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            steam = root / "Steam"
            library = root / "external"
            (steam / "steamapps").mkdir(parents=True)
            (library / "steamapps").mkdir(parents=True)
            (steam / "steamapps/libraryfolders.vdf").write_text(
                f'"libraryfolders" {{ "1" {{ "path" "{library.as_posix()}" }} }}')
            manifest = library / "steamapps/appmanifest_252490.acf"
            manifest.write_text('"AppState" { "appid" "252490" "buildid" "100" "StateFlags" "6" "TargetBuildID" "200" }')
            self.assertEqual(find_manifest({"game": {"app_id": 252490, "steam_path": str(steam)}}), manifest.resolve())
            self.assertTrue(read_snapshot(manifest, 252490).pending)
            with self.assertRaises(ValueError):
                read_snapshot(manifest, 1)
            state = root / "state/data.json"
            save_state(state, {"one"})
            self.assertEqual(load_state(state), {"sent": ["one"]})

    def test_main_defaults_notify_only_download_and_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text(Path("config.example.toml").read_text())
            samples = [snapshot(), snapshot(target="200", flags=6),
                       snapshot(target="200", flags=262, downloaded=10),
                       snapshot(build="200")]
            with (patch("sys.argv", ["steam_watch.py", "--config", str(config)]),
                  patch("steam_watch.find_manifest", return_value=root / "manifest.acf"),
                  patch("steam_watch.read_snapshot", side_effect=samples),
                  patch("steam_watch.steam_running", return_value=True),
                  patch("steam_watch.notify", return_value=True) as notification,
                  patch("steam_watch.time.sleep", side_effect=[None, None, KeyboardInterrupt])):
                self.assertEqual(main(), 0)
                self.assertEqual(notification.call_count, 2)
                messages = [call.args[1] for call in notification.call_args_list]
                self.assertIn("загружается", messages[0])
                self.assertIn("установил", messages[1])
            self.assertTrue(list((root / ".state").glob("*.json")))

    def test_invalid_saved_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            for content in ['[]', '{"sent": [1]}', '{"sent": "bad"}', '{']:
                path.write_text(content)
                self.assertEqual(load_state(path), {})

    def test_config_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            text = Path("config.example.toml").read_text()
            path.write_text(text)
            self.assertEqual(load_config(path)["game"]["app_id"], 252490)
            path.write_text(text.replace("poll_seconds = 2", "poll_seconds = 0"))
            with self.assertRaises(ValueError):
                load_config(path)

    def test_failed_channel_retried_without_resending_successful_one(self):
        DELIVERED.clear()
        config = {"game": {"name": "Rust"}, "notifications": {"desktop": True, "sound": True},
                  "telegram": {"enabled": False}}
        with patch("steam_watch.desktop") as desktop, patch("steam_watch.sound", side_effect=[OSError(), None]) as sound:
            self.assertFalse(notify(config, "test"))
            self.assertTrue(notify(config, "test"))
            self.assertEqual(desktop.call_count, 1)
            self.assertEqual(sound.call_count, 2)
        DELIVERED.clear()


if __name__ == "__main__":
    unittest.main()
