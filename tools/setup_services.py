"""Configure fixed localhost database ports and start only the database containers."""

import argparse
import os
import secrets
import shutil
import socket
import subprocess
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values, set_key

from downloader_bot.core.config import connection_urls, parse_integer

ROOT = Path(__file__).resolve().parents[1]


def previous_port(values: dict, key: str) -> str:
    try:
        parsed = urlsplit(values.get(key) or "")
        if parsed.hostname in {"localhost", "127.0.0.1"} and parsed.port:
            return str(parsed.port)
    except ValueError:
        pass
    return ""


def configure_environment(path: Path, template: Path) -> dict:
    if not path.exists():
        shutil.copyfile(template, path)
    values = dotenv_values(path, encoding="utf-8-sig", interpolate=False)
    chosen = set()
    updates = {}
    # Collect both explicit ports before selecting any missing one.
    for key, url_key in (("POSTGRES_PORT", "DATABASE_URL"), ("REDIS_PORT", "REDIS_URL")):
        port = values.get(key) or previous_port(values, url_key)
        if not port:
            continue
        number = parse_integer(key, port, 1, 65535)
        if number in chosen:
            raise RuntimeError("POSTGRES_PORT and REDIS_PORT must be different")
        chosen.add(number)
        updates[key] = str(number)

    with ExitStack() as reservations:
        for key in ("POSTGRES_PORT", "REDIS_PORT"):
            if key in updates:
                continue
            while True:
                reservation = reservations.enter_context(socket.socket())
                reservation.bind(("127.0.0.1", 0))
                number = reservation.getsockname()[1]
                if number not in chosen:
                    break
            chosen.add(number)
            updates[key] = str(number)

    password = values.get("DEV_DB_PASSWORD") or ""
    if not password or password == "YOUR_PASSWORD":
        password = secrets.token_hex(32)
    updates["DEV_DB_PASSWORD"] = password
    updates["DATABASE_URL"], updates["REDIS_URL"] = connection_urls(updates)
    if not values.get("FFMPEG_PATH") and (ffmpeg := shutil.which("ffmpeg")):
        updates["FFMPEG_PATH"] = ffmpeg

    # Literal single-quoted values keep dollar signs in passwords out of interpolation.
    for key, value in updates.items():
        set_key(path, key, value, quote_mode="always", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    values.update(updates)
    return values


def compose_command(root: Path, env_file: Path, project: str, distribution=None, user="root"):
    if distribution:
        try:
            relative_env = env_file.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            raise RuntimeError(
                "WSL setup requires the environment file inside the project folder"
            ) from None
        return [
            "wsl",
            "--distribution",
            distribution,
            "--user",
            user,
            "--cd",
            str(root),
            "--exec",
            "docker",
            "compose",
            "--env-file",
            relative_env,
            "-p",
            project,
        ]
    return [
        "docker",
        "compose",
        "--project-directory",
        str(root),
        "--env-file",
        str(env_file),
        "-p",
        project,
    ]


def start_services(root: Path, env_file: Path, project: str, values: dict, **options) -> None:
    command = compose_command(root, env_file, project, **options)
    environment = os.environ.copy()
    for key in ("POSTGRES_PORT", "REDIS_PORT", "DEV_DB_PASSWORD"):
        environment[key] = values[key]
    if options.get("distribution"):
        # Forward configuration as environment variables, never as secret CLI arguments.
        entries = environment.get("WSLENV", "").split(":")
        entries.extend(f"{key}/u" for key in ("POSTGRES_PORT", "REDIS_PORT", "DEV_DB_PASSWORD"))
        environment["WSLENV"] = ":".join(entry for entry in entries if entry)
    subprocess.run(
        command
        + ["up", "-d", "--remove-orphans", "--wait", "--wait-timeout", "90", "postgres", "redis"],
        cwd=root,
        env=environment,
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="saintcrispy")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--wsl-distribution")
    parser.add_argument("--wsl-user", default="root")
    parser.add_argument("--configure-only", action="store_true")
    args = parser.parse_args()
    try:
        path = args.env_file.resolve()
        values = configure_environment(path, ROOT / ".env.example")
        if not args.configure_only:
            start_services(
                ROOT,
                path,
                args.project,
                values,
                distribution=args.wsl_distribution,
                user=args.wsl_user,
            )
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        if isinstance(error, RuntimeError):
            print(str(error))
        else:
            print(f"Database setup failed: {type(error).__name__}")
        return 1
    print(f"PostgreSQL: 127.0.0.1:{values['POSTGRES_PORT']}")
    print(f"Redis: 127.0.0.1:{values['REDIS_PORT']}")
    print("Ports and connection URLs saved in .env; credentials omitted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
