import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from steam_watch import Snapshot, Tracker, find_manifest, load_config, parse_vdf, read_snapshot, save_state, load_state, notify, DELIVERED, main, parse_app_info, published_event, MetadataProbe


def snapshot(build="100", target="0", flags=4, downloaded=0, branch="public"):
    return Snapshot(build, target, flags, downloaded, branch)


class WatchTests(unittest.TestCase):
    def test_pending_requires_new_target_not_repair_or_cached_target(self):
        for current in [snapshot(flags=6 | 32), snapshot(target="100", flags=6),
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

    def test_main_defaults_notify_queue_download_and_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text(Path("config.example.toml").read_text())
            samples = [snapshot(), snapshot(target="200", flags=6),
                       snapshot(target="200", flags=262, downloaded=10),
                       snapshot(build="200")]
            with (patch("sys.argv", ["steam_watch.py", "--config", str(config)]),
                  patch("steam_watch.shutil.which", return_value=None),
                  patch("steam_watch.find_manifest", return_value=root / "manifest.acf"),
                  patch("steam_watch.read_snapshot", side_effect=samples),
                  patch("steam_watch.steam_running", return_value=True),
                  patch("steam_watch.notify", return_value=True) as notification,
                  patch("steam_watch.time.monotonic", side_effect=range(100)),
                  patch("steam_watch.time.sleep", side_effect=[None, None, KeyboardInterrupt])):
                self.assertEqual(main(), 0)
                self.assertEqual(notification.call_count, 3)
                messages = [call.args[1] for call in notification.call_args_list]
                self.assertIn("Скачать сейчас", messages[0])
                self.assertIn("загружается", messages[1])
                self.assertIn("установил", messages[2])
            self.assertTrue(list((root / ".state").glob("*.json")))



    def test_early_requirement_without_target_is_cautiously_reported(self):
        tracker = Tracker()
        events = tracker.observe(snapshot(flags=6))
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0][0].startswith("required:"))
        self.assertIn("пока не подтверждены", events[0][1])
        tracker.sent.update(key for key, _ in events)
        self.assertEqual(tracker.observe(snapshot(flags=6)), [])
        # Once Steam records the target, a specific queue signal is still available.
        self.assertTrue(tracker.observe(snapshot(target="200", flags=6))[0][0].startswith("queued:"))

    def test_metadata_reads_output_from_real_nonblocking_process(self):
        config = {"enabled": True, "steamcmd_path": "steamcmd", "poll_seconds": 60, "timeout_seconds": 45}
        original_popen = subprocess.Popen
        output = '"252490" { "depots" { "branches" { "public" { "buildid" "200" } } } }'

        def launch_python_fixture(command, **kwargs):
            self.assertIn("+app_info_print", command)
            return original_popen([sys.executable, "-c", f"print({output!r})"], **kwargs)

        with (patch("steam_watch.shutil.which", return_value="/steamcmd"),
              patch("steam_watch.subprocess.Popen", side_effect=launch_python_fixture)):
            probe = MetadataProbe(config, 252490)
            try:
                self.assertIsNone(probe.poll(snapshot(), 0))
                probe.process.wait(timeout=5)
                self.assertEqual(probe.poll(snapshot(), 1), ("public", "200"))
                self.assertIsNone(probe.process)
                self.assertIsNone(probe.output)
            finally:
                probe.close()

    def test_parse_steamcmd_output_with_noise_and_branches(self):
        output = 'Steam Console Client\nLogging in...OK\n"252490" { "common" { "name" "A } game" } "depots" { "branches" { "public" { "buildid" "200" "pwdrequired" "0" } "beta" { "buildid" "300" } } } }\nExiting'
        self.assertEqual(parse_app_info(output, 252490, "public"), "200")
        self.assertEqual(parse_app_info(output, 252490, "beta"), "300")
        with self.assertRaises(KeyError):
            parse_app_info(output, 252490, "missing")
        for broken in ["No app info", output[:50], output.replace('"200"', '"0"'),
                       output.replace('"pwdrequired" "0"', '"pwdrequired" "1"')]:
            with self.assertRaises((ValueError, KeyError)):
                parse_app_info(broken, 252490, "public")

    def test_publication_is_preliminary_and_matches_local_branch(self):
        event = published_event(snapshot(), "public", "200")
        self.assertTrue(event[0].startswith("published:"))
        self.assertIn("пока не подтверждена", event[1])
        self.assertIsNone(published_event(snapshot(build="200"), "public", "200"))
        self.assertIsNone(published_event(snapshot(branch="beta"), "public", "200"))
        self.assertIsNone(published_event(snapshot(target="200", flags=6), "public", "200"))

    def test_metadata_single_job_and_interval(self):
        config = {"enabled": True, "steamcmd_path": "steamcmd", "poll_seconds": 60, "timeout_seconds": 45}
        process = MagicMock()
        process.poll.return_value = None
        with patch("steam_watch.shutil.which", return_value="/steamcmd"), patch("steam_watch.subprocess.Popen", return_value=process) as launch:
            probe = MetadataProbe(config, 252490)
            try:
                self.assertIsNone(probe.poll(snapshot(), 0))
                self.assertIsNone(probe.poll(snapshot(), 1))
                self.assertEqual(launch.call_count, 1)
                command = launch.call_args.args[0]
                self.assertEqual(command, ["/steamcmd", "+login", "anonymous", "+app_info_update", "1", "+app_info_print", "252490", "+quit"])
                probe.output.write(b'"252490" { "depots" { "branches" { "public" { "buildid" "200" } } } }')
                process.poll.return_value = 0
                self.assertEqual(probe.poll(snapshot(), 2), ("public", "200"))
                self.assertIsNone(probe.poll(snapshot(), 61))
                self.assertEqual(launch.call_count, 1)
                probe.poll(snapshot(), 62)
                self.assertEqual(launch.call_count, 2)
            finally:
                probe.close()

    def test_metadata_timeout_backoff_and_cleanup(self):
        config = {"enabled": True, "steamcmd_path": "steamcmd", "poll_seconds": 60, "timeout_seconds": 45}
        process = MagicMock()
        process.pid = 12345
        process.poll.return_value = None
        with (patch("steam_watch.shutil.which", return_value="/steamcmd"),
              patch("steam_watch.subprocess.Popen", return_value=process) as launch,
              patch("steam_watch.os.killpg") as kill):
            probe = MetadataProbe(config, 252490)
            probe.poll(snapshot(), 0)
            probe.poll(snapshot(), 45)
            self.assertIsNone(probe.process)
            self.assertIsNone(probe.output)
            self.assertEqual(probe.next_check, 165)
            self.assertEqual(probe.failures, 1)
            if __import__("os").name == "posix":
                kill.assert_called_once()
            else:
                process.kill.assert_called_once()
            probe.poll(snapshot(), 164)
            self.assertEqual(launch.call_count, 1)

    def test_missing_steamcmd_keeps_local_monitoring(self):
        with patch("steam_watch.shutil.which", return_value=None):
            probe = MetadataProbe({"enabled": True, "steamcmd_path": "steamcmd"}, 252490)
            self.assertIsNone(probe.poll(snapshot(), 0))
            probe.close()

    def test_metadata_can_notify_while_desktop_steam_is_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text(Path("config.example.toml").read_text())
            probe = MagicMock()
            probe.command = "/steamcmd"
            probe.poll.side_effect = [None, ("public", "200")]
            with (patch("sys.argv", ["steam_watch.py", "--config", str(config)]),
                  patch("steam_watch.MetadataProbe", return_value=probe),
                  patch("steam_watch.find_manifest", return_value=root / "manifest.acf"),
                  patch("steam_watch.read_snapshot", return_value=snapshot()),
                  patch("steam_watch.steam_running", return_value=False),
                  patch("steam_watch.notify", return_value=True) as notification,
                  patch("steam_watch.time.monotonic", return_value=10),
                  patch("steam_watch.time.sleep", side_effect=[None, KeyboardInterrupt])):
                self.assertEqual(main(), 0)
                self.assertEqual(notification.call_count, 1)
                self.assertIn("опубликована", notification.call_args.args[1])
                self.assertEqual(probe.poll.call_count, 2)
                probe.close.assert_called_once()

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
            path.write_text(text.replace("poll_seconds = 1", "poll_seconds = 0"))
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
