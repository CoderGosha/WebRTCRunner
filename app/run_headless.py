#!/usr/bin/env python3
"""Запускает оба headless-бинарника параллельно с одним -cookies каждый."""

from __future__ import annotations

import argparse
from typing import TextIO
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VK_BIN = ROOT / "headless-vk-creator-linux-x64"
TELEMOST_BIN = ROOT / "headless-telemost-creator-linux-x64"

COOKIES_LOCAL = Path(os.environ.get("COOKIES_LOCAL_DIR", "/app/cookies/local"))
COOKIES_VOLUME = Path(os.environ.get("COOKIES_VOLUME_DIR", "/app/cookies/volume"))

JOIN_LINK_RE = re.compile(r"join_link:\s*(\S+)")
LOG_PREFIX_MAIN = "[run_headless]"


def log_main(msg: str, file: TextIO = sys.stdout) -> None:
    for ln in msg.rstrip("\n").split("\n"):
        print(f"{LOG_PREFIX_MAIN} {ln}", file=file, flush=True)


def _telegram_configured() -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return bool(token and chat_id)


def send_telegram_text(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    server = os.environ.get("SERVER_NAME", "").strip() or "—"
    body = f"Сервер: {server}\n\n{text}"
    payload = json.dumps(
        {"chat_id": chat_id, "text": body, "disable_web_page_preview": True},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                log_main(f"Telegram HTTP {resp.status}", file=sys.stderr)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        log_main(f"Telegram HTTP {e.code}: {detail}", file=sys.stderr)
    except OSError as e:
        log_main(f"Telegram: {e}", file=sys.stderr)


def send_telegram_join_link(label: str, url: str) -> None:
    text = (
        "Обновлены настройки конференций\n\n"
        f"CALL CREATED ({label})\njoin_link:\n{url}"
    )
    send_telegram_text(text)


def send_telegram_conference_suspended(stopped_label: str, exit_code: int) -> None:
    lines = ["Приостановлена конференция."]
    if exit_code != 0:
        lines.append("Сбой приложения.")
    lines.append(f"Первым завершился процесс: {stopped_label}, код выхода: {exit_code}.")
    send_telegram_text("\n".join(lines))


def prestart_delay_seconds() -> int:
    raw = os.environ.get("PRESTART_SECONDS", "3").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 3


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Запуск VK и Telemost headless с файлом куков.")
    p.add_argument(
        "--vk-cookies",
        metavar="PATH",
        default=None,
        help="Один файл куков VK. По умолчанию: VK_COOKIES.",
    )
    p.add_argument(
        "--telemost-cookies",
        metavar="PATH",
        default=None,
        help="Один файл куков Telemost (-cookies). По умолчанию: TELEMOST_COOKIES.",
    )
    return p.parse_args()


def resolve_one(cli: str | None, env_name: str) -> str | None:
    if cli is not None and cli.strip():
        return cli.strip()
    raw = os.environ.get(env_name)
    return raw.strip() if raw and raw.strip() else None


def resolve_cookie_file(spec: str, label: str) -> str | None:
    """Сначала локальная директория (bind), затем named volume. Либо абсолютный путь, если файл есть."""
    spec = spec.strip()
    p = Path(spec)
    if p.is_file():
        resolved = str(p.resolve())
        log_main(f"куки {label}: {resolved}")
        return resolved
    name = p.name
    if not name:
        log_main(f"неверное имя файла куков {label}: {spec!r}", file=sys.stderr)
        return None
    for base, src in ((COOKIES_LOCAL, "local"), (COOKIES_VOLUME, "volume")):
        candidate = (base / name).resolve()
        if candidate.is_file():
            log_main(f"куки {label}: {candidate} (источник: {src})")
            return str(candidate)
    log_main(
        f"нет файла куков {label} ({name!r}) ни в {COOKIES_LOCAL}, ни в {COOKIES_VOLUME}",
        file=sys.stderr,
    )
    return None


def stream_reader(label: str, proc: subprocess.Popen) -> None:
    """Дублирует вывод процесса в stdout с префиксом [VK] / [Telemost]."""
    assert proc.stdout is not None
    prefix = f"[{label}]"
    awaiting = False
    for line in proc.stdout:
        sys.stdout.write(f"{prefix} {line}")
        sys.stdout.flush()
        stripped = line.strip()
        if stripped == "CALL CREATED":
            awaiting = True
            continue
        if awaiting:
            m = JOIN_LINK_RE.search(line)
            if m:
                url = m.group(1).rstrip()
                print(flush=True)
                log_main("---------- найдено ----------")
                log_main("  CALL CREATED")
                log_main(f"  join_link: {url}")
                log_main(f"  ({label})")
                log_main("-------------------------------------------")
                print(flush=True)
                send_telegram_join_link(label, url)
                awaiting = False


def main() -> int:
    args = parse_args()
    vk_cookie = resolve_one(args.vk_cookies, "VK_COOKIES")
    tm_cookie = resolve_one(args.telemost_cookies, "TELEMOST_COOKIES")

    if not vk_cookie:
        log_main("нужен один файл куков VK (--vk-cookies или VK_COOKIES)", file=sys.stderr)
        return 1
    if not tm_cookie:
        log_main(
            "нужен один файл куков Telemost (--telemost-cookies или TELEMOST_COOKIES)",
            file=sys.stderr,
        )
        return 1

    vk_resolved = resolve_cookie_file(vk_cookie, "VK")
    tm_resolved = resolve_cookie_file(tm_cookie, "Telemost")
    if not vk_resolved or not tm_resolved:
        return 1

    jobs: list[tuple[str, Path, str]] = [
        ("VK", VK_BIN, vk_resolved),
        ("Telemost", TELEMOST_BIN, tm_resolved),
    ]

    labeled_procs: list[tuple[str, subprocess.Popen]] = []

    def shutdown(signum: int, frame: object | None) -> None:
        for _, proc in labeled_procs:
            if proc.poll() is None:
                proc.send_signal(signum)
        for _, proc in labeled_procs:
            if proc.poll() is None:
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        sys.exit(128 + signum if signum > 0 else 1)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    env = os.environ.copy()

    delay = prestart_delay_seconds()
    if delay > 0:
        msg = f"Через {delay} с запуск конференций VK и Telemost."
        log_main(f"пауза {delay} с перед запуском процессов…")
        if _telegram_configured():
            send_telegram_text(msg)
        time.sleep(delay)

    for label, binary, cookie_path in jobs:
        if not binary.is_file():
            log_main(f"не найден {binary}", file=sys.stderr)
            return 1
        os.chmod(binary, binary.stat().st_mode | 0o111)
        cmd = [str(binary), "-cookies", cookie_path]
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        labeled_procs.append((label, proc))

    for label, proc in labeled_procs:
        threading.Thread(target=stream_reader, args=(label, proc), daemon=True).start()

    try:
        while True:
            for label, proc in labeled_procs:
                code = proc.poll()
                if code is not None:
                    send_telegram_conference_suspended(label, code)
                    for other_label, q in labeled_procs:
                        if q is not proc and q.poll() is None:
                            q.terminate()
                    for _, q in labeled_procs:
                        if q.poll() is None:
                            try:
                                q.wait(timeout=30)
                            except subprocess.TimeoutExpired:
                                q.kill()
                                q.wait()
                    return code
            time.sleep(0.25)
    except KeyboardInterrupt:
        shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    raise SystemExit(main())
