#!/usr/bin/env python3
"""Early Steam build alerts, local update queue and download confirmation."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass

LOG = logging.getLogger("steam-watch")
BASE = Path(__file__).resolve().parent


def parse_vdf(text: str) -> dict:
    """Parse Steam's quoted KeyValues format; reject truncated snapshots."""
    token_pattern = re.compile(r'//[^\n]*|"(?:\\.|[^"\\])*"|[{}]|[^\s{}"]+')
    matches = list(token_pattern.finditer(text))
    end = 0
    for match in matches:
        if text[end:match.start()].strip():
            raise ValueError("Некорректный VDF")
        end = match.end()
    if text[end:].strip():
        raise ValueError("Неполный VDF")
    tokens = [m.group() for m in matches if not m.group().startswith("//")]
    index = 0

    def scalar(token: str) -> str:
        if token.startswith('"'):
            if not token.endswith('"'):
                raise ValueError("Незакрытая строка VDF")
            return re.sub(r'\\([\\"])', r'\1', token[1:-1])
        return token

    def block(nested: bool = False) -> dict:
        nonlocal index
        result = {}
        while index < len(tokens):
            key = tokens[index]
            index += 1
            if key == "}":
                if nested:
                    return result
                raise ValueError("Лишняя скобка VDF")
            if key == "{" or index == len(tokens):
                raise ValueError("Неполный VDF")
            value = tokens[index]
            index += 1
            if value == "}":
                raise ValueError("Нет значения VDF")
            result[scalar(key)] = block(True) if value == "{" else scalar(value)
        if nested:
            raise ValueError("Неполный VDF")
        return result

    return block()


def steam_roots() -> list[Path]:
    home = Path.home()
    if sys.platform == "win32":
        import winreg
        roots = []
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                roots.append(Path(winreg.QueryValueEx(key, "SteamPath")[0]))
        except OSError:
            pass
        roots.extend(Path(os.environ.get(env, default)) / "Steam" for env, default in
                     [("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                      ("PROGRAMFILES", r"C:\Program Files")])
        return roots
    if sys.platform == "darwin":
        return [home / "Library/Application Support/Steam"]
    return [home / ".local/share/Steam", home / ".steam/steam",
            home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
            home / "snap/steam/common/.local/share/Steam"]


def find_manifest(config: dict) -> Path:
    game = config["game"]
    if game.get("manifest_path"):
        return Path(game["manifest_path"]).expanduser()
    roots = [Path(game["steam_path"]).expanduser()] if game.get("steam_path") else steam_roots()
    libraries = list(roots)
    for root in roots:
        vdf = root / "steamapps/libraryfolders.vdf"
        if vdf.exists():
            data = parse_vdf(vdf.read_text(encoding="utf-8-sig"))
            for key, value in data.get("libraryfolders", {}).items():
                if key.isdigit():
                    path = value.get("path") if isinstance(value, dict) else value
                    if path:
                        libraries.append(Path(path))
    matches = list(dict.fromkeys(p.resolve() for root in libraries
                   if (p := root / "steamapps" / f'appmanifest_{game["app_id"]}.acf').is_file()))
    if not matches:
        raise FileNotFoundError("Игра не найдена. Укажите game.manifest_path в конфиге.")
    if len(matches) > 1:
        raise ValueError("Несколько установок игры: выберите game.manifest_path.")
    return matches[0]


def steam_running() -> bool:
    if sys.platform == "win32":
        result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe", "/FO", "CSV", "/NH"],
                                capture_output=True, timeout=5)
        return b'"steam.exe"' in result.stdout.lower()
    result = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True, timeout=5)
    return any(Path(line.strip()).name.lower() in {"steam", "steam_osx"}
               for line in result.stdout.splitlines())


@dataclass(frozen=True)
class Snapshot:
    build: str
    target: str
    flags: int
    downloaded: int
    branch: str

    @property
    def pending(self) -> bool:
        # UpdateRequired alone can also mean validation/repair. Require a new build.
        return bool(self.target not in {"", "0", self.build} and self.flags & (2 | 8 | 256 | 512 | 1024))


def read_snapshot(path: Path, app_id: int) -> Snapshot:
    data = parse_vdf(path.read_text(encoding="utf-8-sig"))["AppState"]
    if str(data["appid"]) != str(app_id):
        raise ValueError("AppID манифеста не совпадает с конфигом")
    build = data["buildid"]
    if not build.isdigit() or int(build) <= 0:
        raise ValueError("Нет установленной сборки: дождитесь завершения установки игры")
    user = data.get("UserConfig", {})
    return Snapshot(build, data.get("TargetBuildID", "0"), int(data["StateFlags"]),
                    int(data.get("BytesDownloaded", "0")), user.get("BetaKey") or "public")


class Tracker:
    def __init__(self, previous: Snapshot | None = None, sent: set[str] | None = None):
        self.previous = previous
        self.sent = sent or set()

    def observe(self, current: Snapshot) -> list[tuple[str, str]]:
        previous = self.previous
        events = []
        identity = f"{current.branch}:{current.build}:{current.target}"
        if current.pending:
            events.append((f"queued:early:{identity}",
                           f"Steam на этом ПК отметил обновление: {current.build} → {current.target} "
                           f"(ветка {current.branch}). Откройте Steam → Загрузки и нажмите «Скачать сейчас». "
                           "Получение данных ещё не подтверждено."))
        elif (current.target in {"", "0"} and current.flags & 2
              and not current.flags & (32 | 128)):
            # Some clients do not write TargetBuildID until downloading starts.
            # A requirement alone may also be validation; never call this a proven release.
            events.append((f"required:{current.branch}:{current.build}",
                           "Steam на этом ПК запросил обновление или проверку файлов; "
                           "целевая сборка ещё не записана. Откройте Steam → Загрузки "
                           "и проверьте игру. Новый релиз и получение данных пока не подтверждены."))
        # Only a live increase proves bytes were received; a flag/cached counter doesn't.
        if (previous and current.pending and current.branch == previous.branch
                and current.target == previous.target and current.build == previous.build
                and current.downloaded > previous.downloaded):
            events.append((f"download:{identity}",
                           f"Обновление загружается на этом ПК: сборка {current.target}, "
                           f"получено {current.downloaded:,} байт."))
        if (previous and current.build != previous.build and current.branch == previous.branch
                and current.flags & 4 and not current.pending):
            events.append((f"installed:{current.branch}:{current.build}",
                           f"Steam установил другую сборку: {previous.build} → {current.build}."))
        self.previous = current
        return [(key, message) for key, message in events if key not in self.sent]


def parse_app_info(output: str, app_id: int, branch: str) -> str:
    """Extract a single complete KeyValues app block from noisy SteamCMD output."""
    marker = re.search(r'"' + str(app_id) + r'"\s*\{', output)
    if marker is None:
        raise ValueError("SteamCMD не вернул информацию об игре")
    depth = 0
    for token in re.finditer(r'"(?:\\.|[^"\\])*"|[{}]', output[marker.start():]):
        if token.group() == "{":
            depth += 1
        elif token.group() == "}":
            depth -= 1
            if depth == 0:
                data = parse_vdf(output[marker.start():marker.start() + token.end()])
                info = data[str(app_id)]["depots"]["branches"][branch]
                if info.get("pwdrequired", "0") != "0":
                    raise ValueError("Закрытая ветка не поддерживается анонимной проверкой")
                build = info["buildid"]
                if not build.isdigit() or int(build) <= 0:
                    raise ValueError("Некорректный buildid в ответе SteamCMD")
                return build
    raise ValueError("Неполный ответ SteamCMD")


def published_event(current: Snapshot, branch: str, build: str) -> tuple[str, str] | None:
    # A rolled-back installed beta can have a larger ID; report a difference, not "newer".
    if (current.branch != branch or current.build == build
            or (current.pending and current.target == build)):
        return None
    return (f"published:{branch}:{build}",
            f"В Steam опубликована другая сборка: {build} (ветка {branch}), "
            f"на этом ПК установлена {current.build}. Проверьте Steam → Загрузки. "
            "Доступность обновления в вашем клиенте пока не подтверждена.")


class MetadataProbe:
    """One nonblocking SteamCMD metadata job at a time; bounded retries and cleanup."""
    def __init__(self, config: dict, app_id: int):
        self.config = config
        self.app_id = app_id
        self.process = None
        self.output = None
        self.next_check = 0.0
        self.started = 0.0
        self.branch = "public"
        self.failures = 0
        self.last_problem = None
        self.last_result = None
        self.command = None
        if config["enabled"]:
            self.command = shutil.which(str(Path(config["steamcmd_path"]).expanduser()))
            if self.command is None:
                LOG.warning("SteamCMD не найден: ранняя проверка публикаций выключена. "
                            "Установите SteamCMD и задайте release.steamcmd_path. "
                            "Локальные уведомления продолжают работать.")

    def close(self) -> None:
        if self.process is not None:
            if self.process.poll() is None:
                if os.name == "posix":
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    self.process.kill()
            self.process.wait(timeout=5)
            self.process = None
        if self.output is not None:
            self.output.close()
            self.output = None

    def failed(self, now: float, problem: str) -> None:
        self.close()
        self.failures += 1
        delay = min(900, self.config["poll_seconds"] * 2 ** min(self.failures, 4))
        self.next_check = now + delay
        if problem != self.last_problem:
            LOG.warning("Ранняя проверка сборки: %s; повтор через %s с. "
                        "Локальные уведомления продолжают работать.", problem, delay)
            self.last_problem = problem

    def poll(self, current: Snapshot, now: float) -> tuple[str, str] | None:
        if self.command is None:
            return None
        if self.process is not None:
            result = self.process.poll()
            if result is None:
                if now - self.started >= self.config["timeout_seconds"]:
                    self.failed(now, "таймаут SteamCMD")
                return None
            try:
                if result != 0:
                    raise ValueError(f"SteamCMD завершился с кодом {result}")
                self.output.seek(0)
                output = self.output.read().decode("utf-8", errors="replace")
                build = parse_app_info(output, self.app_id, self.branch)
                branch = self.branch
                self.close()
                self.failures = 0
                self.last_problem = None
                # Wait a full interval after completion, not just after launching.
                self.next_check = now + self.config["poll_seconds"]
                if self.last_result != (branch, build):
                    LOG.info("SteamCMD: ветка %s, опубликованная сборка %s", branch, build)
                    self.last_result = (branch, build)
                return branch, build
            except (OSError, ValueError, KeyError) as error:
                self.failed(now, str(error))
                return None
        if now < self.next_check:
            return None
        try:
            self.output = tempfile.TemporaryFile()
            self.branch = current.branch
            self.process = subprocess.Popen(
                [self.command, "+login", "anonymous", "+app_info_update", "1",
                 "+app_info_print", str(self.app_id), "+quit"],
                stdin=subprocess.DEVNULL, stdout=self.output, stderr=subprocess.STDOUT,
                start_new_session=(os.name == "posix"))
            self.started = now
        except OSError as error:
            self.failed(now, str(error))
        return None


def run_command(args: list[str], **kwargs) -> None:
    subprocess.run(args, check=True, timeout=10, **kwargs)


def desktop(title: str, message: str) -> None:
    if sys.platform == "darwin":
        # Arguments, not interpolated AppleScript source.
        run_command(["osascript", "-e", 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run', title, message], capture_output=True)
    elif sys.platform == "win32":
        script = '''Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.Visible = $true
$n.ShowBalloonTip(5000, $env:SW_TITLE, $env:SW_MESSAGE, [System.Windows.Forms.ToolTipIcon]::Info)
Start-Sleep -Seconds 6
$n.Dispose()'''
        run_command(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                    env={**os.environ, "SW_TITLE": title, "SW_MESSAGE": message}, capture_output=True)
    else:
        run_command(["notify-send", title, message], capture_output=True)


def sound() -> None:
    if sys.platform == "win32":
        import winsound
        winsound.MessageBeep()
    elif sys.platform == "darwin":
        run_command(["afplay", "/System/Library/Sounds/Glass.aiff"], capture_output=True)
    else:
        print("\a", end="", flush=True)


def telegram(config: dict, message: str) -> None:
    token = os.environ[config["token_env"]]
    chat = os.environ[config["chat_id_env"]]
    payload = urllib.parse.urlencode({"chat_id": chat, "text": message}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload)
    with urllib.request.urlopen(request, timeout=10) as response:
        if not json.load(response).get("ok"):
            raise ValueError("Telegram отклонил сообщение")


def notify(config: dict, message: str, *, retry: bool = True) -> bool:
    """Retry failed channels; successful channels are not resent in this process."""
    title = f'{config["game"]["name"]} — Steam'
    channels = config["notifications"]
    success = True
    for name, action in [("desktop", lambda: desktop(title, message)),
                         ("sound", sound),
                         ("telegram", lambda: telegram(config["telegram"], f"{title}\n{message}"))]:
        enabled = config["telegram"]["enabled"] if name == "telegram" else channels[name]
        cache_key = (name, title, message)
        if not enabled or cache_key in DELIVERED:
            continue
        try:
            action()
            DELIVERED.add(cache_key)
        except Exception as error:
            # urllib exceptions can include a bot token in their URL; log type only.
            LOG.warning("Оповещение %s не доставлено (%s); %s", name, type(error).__name__,
                        "повтор через 60 секунд" if retry else "в тестовом режиме повторов нет")
            success = False
    return success


DELIVERED: set[tuple[str, str, str]] = set()


def load_config(path: Path) -> dict:
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    # Old configs keep working; early signals default to enabled if keys are omitted.
    release = config.setdefault("release", {})
    for key, value in {"enabled": True, "steamcmd_path": "steamcmd",
                       "poll_seconds": 60, "timeout_seconds": 45}.items():
        release.setdefault(key, value)
    config["notifications"].setdefault("published", True)
    if type(release["enabled"]) is not bool or not isinstance(release["steamcmd_path"], str) or not release["steamcmd_path"]:
        raise ValueError("release.enabled: bool; release.steamcmd_path: непустой путь")
    for key, low, high in [("poll_seconds", 60, 3600), ("timeout_seconds", 10, 300)]:
        if type(release[key]) not in (int, float) or not low <= release[key] <= high:
            raise ValueError(f"release.{key}: число от {low} до {high}")
    game = config["game"]
    if type(game["app_id"]) is not int or game["app_id"] <= 0 or not isinstance(game["name"], str):
        raise ValueError("game.app_id должен быть положительным целым, game.name — строкой")
    watch = config["watch"]
    interval = watch["poll_seconds"]
    if type(interval) not in (int, float) or not 1 <= interval <= 3600:
        raise ValueError("watch.poll_seconds: число от 1 до 3600")
    for section, keys in [("notifications", ["desktop", "sound", "queued", "installed", "published"]), ("telegram", ["enabled"])]:
        for key in keys:
            if type(config[section][key]) is not bool:
                raise ValueError(f"{section}.{key} должен быть true или false")
    if config["telegram"]["enabled"]:
        for key in ("token_env", "chat_id_env"):
            if not os.environ.get(config["telegram"][key]):
                raise ValueError(f'Задайте переменную {config["telegram"][key]} для Telegram')
    return config


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or not isinstance(data.get("sent", []), list)
                or not all(isinstance(key, str) for key in data.get("sent", []))):
            raise ValueError("Неверный формат состояния")
        return data
    except FileNotFoundError:
        return {}
    except (ValueError, OSError):
        LOG.warning("Файл состояния не читается; начинаем заново")
        return {}


def save_state(path: Path, sent: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"sent": sorted(sent)}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)



def simulate_update(config: dict) -> int:
    """Exercise real detection and notification channels with in-memory snapshots."""
    LOG.info("ТЕСТ: через 3 секунды сымитируем обновление, затем загрузку и установку. "
             "Сообщения помечены [ТЕСТ]; Steam не требуется.")
    tracker = Tracker(Snapshot("100", "0", 4, 0, "public"))
    steps = [Snapshot("100", "200", 6, 0, "public"),
             Snapshot("100", "200", 262, 1048576, "public"),
             Snapshot("200", "0", 4, 0, "public")]
    success = True
    for current in steps:
        time.sleep(3)
        for key, message in tracker.observe(current):
            message = "[ТЕСТ] " + message
            LOG.info(message)
            kind = key.split(":", 1)[0]
            if kind in {"queued", "installed"} and not config["notifications"][kind]:
                LOG.info("ТЕСТ: канал события %s выключен в конфиге; только консоль", kind)
            elif not notify(config, message, retry=False):
                success = False
            tracker.sent.add(key)
    LOG.info("ТЕСТ завершён. История реальных событий не изменена.")
    return 0 if success else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=BASE / "config.toml")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--once", action="store_true", help="Прочитать статус и выйти без уведомлений")
    modes.add_argument("--test-notification", action="store_true", help="Проверить настроенные оповещения")
    modes.add_argument("--simulate-update", action="store_true",
                       help="Через 3 секунды сымитировать обновление, затем загрузку и установку")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    probe = None
    try:
        config = load_config(args.config)
        if args.simulate_update:
            return simulate_update(config)
        if args.test_notification:
            return 0 if notify(config, "Тест оповещения Steam Watch", retry=False) else 1
        manifest = find_manifest(config)
        first = read_snapshot(manifest, config["game"]["app_id"])
        LOG.info("Манифест: %s; build=%s target=%s branch=%s pending=%s",
                 manifest, first.build, first.target, first.branch, first.pending)
        if args.once:
            LOG.info("Это сохранённый локальный статус; доступность загрузки не проверена")
            return 0
        state_path = Path(config["watch"]["state_file"]).expanduser()
        if not state_path.is_absolute():
            state_path = args.config.resolve().parent / state_path
        # Separate state per installation and game.
        state_path = state_path / f'{config["game"]["app_id"]}-{hashlib.sha256(str(manifest.resolve()).encode()).hexdigest()[:12]}.json'
        saved = load_state(state_path)
        tracker = Tracker(sent=set(saved.get("sent", [])))
        persisted = set(tracker.sent)
        pending: dict[str, tuple[str, float]] = {}
        failures = 0
        last_problem = None
        current = first
        probe = MetadataProbe(config["release"], config["game"]["app_id"])
        if probe.command:
            LOG.info("Ранняя проверка публикаций: SteamCMD, интервал %s с", config["release"]["poll_seconds"])

        def enqueue(events):
            for key, message in events:
                if key in tracker.sent or key in pending:
                    continue
                LOG.info(message)
                kind = key.split(":", 1)[0]
                if kind == "required":
                    kind = "queued"
                if kind in {"queued", "installed", "published"} and not config["notifications"][kind]:
                    tracker.sent.add(key)
                else:
                    pending[key] = (message, 0)

        delay = config["watch"]["poll_seconds"]
        next_local = 0.0
        while True:
            if time.monotonic() >= next_local:
                try:
                    if not steam_running():
                        raise RuntimeError("Steam не запущен. Жду запуска клиента.")
                    current = read_snapshot(manifest, config["game"]["app_id"])
                    enqueue(tracker.observe(current))
                    failures = 0
                    last_problem = None
                    delay = config["watch"]["poll_seconds"]
                except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
                    # Do not compare counters across a stopped client / unreadable snapshot.
                    tracker.previous = None
                    problem = str(error)
                    if problem != last_problem:
                        LOG.warning("%s", problem)
                        last_problem = problem
                    failures += 1
                    delay = min(60, config["watch"]["poll_seconds"] * 2 ** min(failures, 6))
                next_local = time.monotonic() + delay
            now = time.monotonic()
            release_result = probe.poll(current, now)
            if release_result:
                event = published_event(current, *release_result)
                if event:
                    enqueue([event])
            for key, (message, retry_at) in list(pending.items()):
                if now >= retry_at:
                    if notify(config, message):
                        tracker.sent.add(key)
                        del pending[key]
                    else:
                        pending[key] = (message, now + 60)
            if tracker.sent != persisted:
                save_state(state_path, tracker.sent)
                persisted = set(tracker.sent)
            # Local retry backoff must not delay completion of an in-flight metadata job.
            time.sleep(min(delay, config["watch"]["poll_seconds"]) if probe.command else delay)
    except KeyboardInterrupt:
        LOG.info("Остановлено")
        return 0
    except (OSError, ValueError, KeyError) as error:
        LOG.error("%s", error)
        return 1

    finally:
        if probe is not None:
            probe.close()


if __name__ == "__main__":
    raise SystemExit(main())
