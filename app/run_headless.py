#!/usr/bin/env python3
"""Запускает headless-бинарники VK, Telemost и WBStream по отдельным флагам окружения."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import TextIO
import html
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
WBSTREAM_BIN = ROOT / "headless-wbstream-creator-linux-x64"

COOKIES_LOCAL = Path(os.environ.get("COOKIES_LOCAL_DIR", "/app/cookies/local"))
COOKIES_VOLUME = Path(os.environ.get("COOKIES_VOLUME_DIR", "/app/cookies/volume"))

JOIN_LINK_RE = re.compile(r"join_link:\s*(\S+)")
LOG_PREFIX_MAIN = "[run_headless]"
RESTART_DELAY_SECONDS = 60
MAX_RESTARTS = 10


def log_main(msg: str, file: TextIO = sys.stdout) -> None:
    for ln in msg.rstrip("\n").split("\n"):
        print(f"{LOG_PREFIX_MAIN} {ln}", file=file, flush=True)


def _telegram_configured() -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return bool(token and chat_id)


def send_telegram_text(text: str, parse_mode: str | None = None) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    server = os.environ.get("SERVER_NAME", "").strip() or "—"
    body = f"Сервер: {server}\n\n{text}"
    payload_obj: dict[str, object] = {
        "chat_id": chat_id,
        "text": body,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload_obj["parse_mode"] = parse_mode
    payload = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")
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
    # Все ссылки отправляем как code block по запросу пользователя.
    text = (
        "Обновлены настройки конференций\n\n"
        f"CALL CREATED ({html.escape(label)})\n"
        "join_link:\n"
        f"<pre>{html.escape(url)}</pre>"
    )
    send_telegram_text(text, parse_mode="HTML")


def send_telegram_conference_suspended(stopped_label: str, exit_code: int) -> None:
    lines = ["Приостановлен процесс конференции."]
    if exit_code != 0:
        lines.append("Сбой приложения.")
    lines.append(f"Завершился процесс: {stopped_label}, код выхода: {exit_code}.")
    send_telegram_text("\n".join(lines))


@dataclass
class JobState:
    label: str
    binary: Path
    extra_args: list[str]
    proc: subprocess.Popen | None = None
    restart_attempts: int = 0
    restart_at: float | None = None
    stopped_forever: bool = False


def env_enabled(env_key: str, default: str = "1") -> bool:
    raw = os.environ.get(env_key, default).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def prestart_delay_seconds() -> int:
    raw = os.environ.get("PRESTART_SECONDS", "3").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 3


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Запуск VK/Telemost (куки) и WBStream без куков; включается через *_ENABLED.",
    )
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


def start_job(job: JobState, env: dict[str, str]) -> bool:
    if not job.binary.is_file():
        log_main(f"не найден {job.binary}", file=sys.stderr)
        return False
    os.chmod(job.binary, job.binary.stat().st_mode | 0o111)
    cmd = [str(job.binary), *job.extra_args]
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    job.proc = proc
    job.restart_at = None
    threading.Thread(target=stream_reader, args=(job.label, proc), daemon=True).start()
    return True


def main() -> int:
    args = parse_args()
    vk_on = env_enabled("VK_ENABLED")
    tm_on = env_enabled("TELEMOST_ENABLED")
    wb_on = env_enabled("WBSTREAM_ENABLED")

    vk_cookie = resolve_one(args.vk_cookies, "VK_COOKIES") if vk_on else None
    tm_cookie = resolve_one(args.telemost_cookies, "TELEMOST_COOKIES") if tm_on else None

    if vk_on:
        if not vk_cookie:
            log_main("нужен один файл куков VK (--vk-cookies или VK_COOKIES)", file=sys.stderr)
            return 1
        vk_resolved = resolve_cookie_file(vk_cookie, "VK")
        if not vk_resolved:
            return 1
    else:
        vk_resolved = None

    if tm_on:
        if not tm_cookie:
            log_main(
                "нужен один файл куков Telemost (--telemost-cookies или TELEMOST_COOKIES)",
                file=sys.stderr,
            )
            return 1
        tm_resolved = resolve_cookie_file(tm_cookie, "Telemost")
        if not tm_resolved:
            return 1
    else:
        tm_resolved = None

    jobs: list[JobState] = []
    if vk_on:
        jobs.append(JobState("VK", VK_BIN, ["-cookies", vk_resolved]))
    if tm_on:
        jobs.append(JobState("Telemost", TELEMOST_BIN, ["-cookies", tm_resolved]))
    if wb_on:
        jobs.append(JobState("WBStream", WBSTREAM_BIN, []))

    if not jobs:
        log_main(
            "включён хотя бы один сервис: VK_ENABLED, TELEMOST_ENABLED или WBSTREAM_ENABLED",
            file=sys.stderr,
        )
        return 1

    def shutdown(signum: int, frame: object | None) -> None:
        for job in jobs:
            if job.proc is not None and job.proc.poll() is None:
                job.proc.send_signal(signum)
        for job in jobs:
            if job.proc is not None and job.proc.poll() is None:
                try:
                    job.proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    job.proc.kill()
                    job.proc.wait()
        sys.exit(128 + signum if signum > 0 else 1)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    env = os.environ.copy()

    delay = prestart_delay_seconds()
    if delay > 0:
        labels = [job.label for job in jobs]
        msg = f"Через {delay} с запуск: {', '.join(labels)}."
        log_main(f"пауза {delay} с перед запуском процессов…")
        if _telegram_configured():
            send_telegram_text(msg)
        time.sleep(delay)

    for job in jobs:
        if not start_job(job, env):
            return 1

    try:
        while True:
            now = time.time()
            active_or_pending = False
            for job in jobs:
                if job.stopped_forever:
                    continue
                if job.proc is not None:
                    code = job.proc.poll()
                    if code is None:
                        active_or_pending = True
                        continue
                    send_telegram_conference_suspended(job.label, code)
                    job.proc = None
                    if code == 0:
                        job.stopped_forever = True
                        log_main(f"{job.label}: процесс завершился штатно, без перезапуска.")
                        continue
                    if job.restart_attempts >= MAX_RESTARTS:
                        job.stopped_forever = True
                        log_main(
                            f"{job.label}: достигнут лимит перезапусков ({MAX_RESTARTS}), больше не перезапускаем.",
                            file=sys.stderr,
                        )
                        send_telegram_text(
                            f"{job.label}: достигнут лимит перезапусков ({MAX_RESTARTS}), перезапуски остановлены."
                        )
                        continue
                    job.restart_attempts += 1
                    job.restart_at = now + RESTART_DELAY_SECONDS
                    active_or_pending = True
                    log_main(
                        f"{job.label}: падение (код {code}), перезапуск через {RESTART_DELAY_SECONDS} сек "
                        f"(попытка {job.restart_attempts}/{MAX_RESTARTS}).",
                        file=sys.stderr,
                    )
                    continue
                if job.restart_at is not None:
                    active_or_pending = True
                    if now >= job.restart_at:
                        if start_job(job, env):
                            log_main(
                                f"{job.label}: успешно перезапущен (попытка {job.restart_attempts}/{MAX_RESTARTS})."
                            )
                        else:
                            job.stopped_forever = True
                            send_telegram_text(
                                f"{job.label}: не удалось перезапустить процесс, перезапуски остановлены."
                            )
            if not active_or_pending:
                log_main("Все процессы остановлены, завершаем run_headless.")
                return 0
            time.sleep(0.25)
    except KeyboardInterrupt:
        shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    raise SystemExit(main())
