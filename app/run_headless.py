#!/usr/bin/env python3
"""Запускает оба headless-бинарника параллельно с одним -cookies каждый."""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VK_BIN = ROOT / "headless-vk-creator-linux-x64"
TELEMOST_BIN = ROOT / "headless-telemost-creator-linux-x64"

JOIN_LINK_RE = re.compile(r"join_link:\s*(\S+)")


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


def stream_reader(label: str, proc: subprocess.Popen) -> None:
    """Дублирует вывод процесса в stdout и после CALL CREATED выделяет join_link."""
    assert proc.stdout is not None
    awaiting = False
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        stripped = line.strip()
        if stripped == "CALL CREATED":
            awaiting = True
            continue
        if awaiting:
            m = JOIN_LINK_RE.search(line)
            if m:
                url = m.group(1).rstrip()
                print(
                    "\n---------- run_headless: найдено ----------\n"
                    "  CALL CREATED\n"
                    f"  join_link: {url}\n"
                    f"  ({label})\n"
                    "-------------------------------------------\n",
                    flush=True,
                )
                awaiting = False


def main() -> int:
    args = parse_args()
    vk_cookie = resolve_one(args.vk_cookies, "VK_COOKIES")
    tm_cookie = resolve_one(args.telemost_cookies, "TELEMOST_COOKIES")

    if not vk_cookie:
        print("run_headless: нужен один файл куков VK (--vk-cookies или VK_COOKIES)", file=sys.stderr)
        return 1
    if not tm_cookie:
        print(
            "run_headless: нужен один файл куков Telemost (--telemost-cookies или TELEMOST_COOKIES)",
            file=sys.stderr,
        )
        return 1

    for label, cpath in (("VK", vk_cookie), ("Telemost", tm_cookie)):
        if not Path(cpath).is_file():
            print(f"run_headless: нет файла куков {label}: {cpath}", file=sys.stderr)
            return 1

    jobs: list[tuple[str, Path, str]] = [
        ("VK", VK_BIN, vk_cookie),
        ("Telemost", TELEMOST_BIN, tm_cookie),
    ]

    procs: list[subprocess.Popen] = []

    def shutdown(signum: int, frame: object | None) -> None:
        for proc in procs:
            if proc.poll() is None:
                proc.send_signal(signum)
        for proc in procs:
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

    proc_by_label: list[tuple[str, subprocess.Popen]] = []
    for label, binary, cookie_path in jobs:
        if not binary.is_file():
            print(f"run_headless: не найден {binary}", file=sys.stderr)
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
        procs.append(proc)
        proc_by_label.append((label, proc))

    for label, proc in proc_by_label:
        threading.Thread(target=stream_reader, args=(label, proc), daemon=True).start()

    try:
        while True:
            for proc in procs:
                code = proc.poll()
                if code is not None:
                    for q in procs:
                        if q is not proc and q.poll() is None:
                            q.terminate()
                    for q in procs:
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
