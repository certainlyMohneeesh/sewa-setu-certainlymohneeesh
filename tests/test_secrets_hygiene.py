# -*- coding: utf-8 -*-
"""Secrets hygiene: no credential may live in a tracked file.

GitGuardian flagged app/config/app.ini, docker-compose.yml, app/app.py and
tests/test_fix3_security.py. Everything secret now comes from environment
variables (supplied by an untracked .env file / the host), and the repository
only contains placeholders. These tests guard the current tree; values that
were committed earlier remain in git history and have been rotated.
"""

import configparser
import os
import re
import subprocess
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")
SECRET_WORDS = re.compile(r"(PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY)", re.I)
# NAME = "literal" / NAME = 'literal' where the name looks secret-bearing
LITERAL_ASSIGNMENT = re.compile(
    r"""(?im)^\s*[\w.\[\]"']*(?:password|passwd|secret|token|api_?key)\w*["']?\s*=\s*(["'])(?!\1)[^"'\n]{4,}\1""")


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                         check=True).stdout.split("\n")
    return [f for f in out if f]


def read(relative):
    with open(os.path.join(ROOT, relative), encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# configuration files
# ---------------------------------------------------------------------------

def test_app_ini_contains_no_secrets():
    cfg = configparser.ConfigParser()
    cfg.read(os.path.join(APP_DIR, "config", "app.ini"))
    leaked = {"%s.%s" % (section, option): value
              for section in cfg.sections() for option, value in cfg.items(section)
              if SECRET_WORDS.search(option) or option in ("secret_key", "password_hash")
              if value.strip()}
    assert leaked == {}, "app.ini must not hold secrets (use environment variables): %s" % sorted(leaked)


def _compose():
    return yaml.safe_load(read("docker-compose.yml"))


def test_docker_compose_has_no_literal_secrets():
    problems = []
    for name, service in _compose()["services"].items():
        env = service.get("environment") or {}
        if isinstance(env, list):
            env = dict(item.split("=", 1) if "=" in item else (item, "") for item in env)
        for key, value in env.items():
            if SECRET_WORDS.search(key) and not str(value).startswith("${"):
                problems.append("%s.%s" % (name, key))
    assert problems == [], "compose must reference .env, not embed secrets: %s" % problems


def test_compose_hands_every_secret_to_the_containers_that_need_it():
    services = _compose()["services"]

    def env(name):
        value = services[name].get("environment") or {}
        return value if isinstance(value, dict) else dict(i.split("=", 1) for i in value)

    assert "POSTGRES_PASSWORD" in env("db")
    for service in ("app", "scheduler"):          # the nightly job needs the DB too
        assert "SEWASETU_DB_PASSWORD" in env(service), service
    assert "SECRET_KEY" in env("app")
    assert "ADMIN_PASSWORD" in env("app") or "ADMIN_PASSWORD_HASH" in env("app")
    for key, value in {**env("db"), **env("app")}.items():
        if SECRET_WORDS.search(key):
            assert ":?" in str(value), "%s must fail loudly when unset" % key


def test_env_file_is_ignored_and_a_blank_template_is_provided():
    assert ".env" not in tracked_files()
    ignored = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT)
    assert ignored.returncode == 0, ".env must be in .gitignore"
    template = read(".env.example")
    keys = {}
    for line in template.splitlines():
        if line.strip() and not line.startswith("#"):
            key, _, value = line.partition("=")
            keys[key.strip()] = value.strip()
    assert {"POSTGRES_PASSWORD", "SECRET_KEY", "ADMIN_USERNAME"} <= set(keys)
    assert "ADMIN_PASSWORD" in keys
    for key, value in keys.items():
        if SECRET_WORDS.search(key):
            assert value == "", "%s in .env.example must be blank" % key


def test_assessor_credentials_document_holds_no_password():
    text = read("ASSESSOR_CREDENTIALS.md")
    assert not re.search(r"(?im)^\|\s*password\s*\|\s*`", text)
    for token in re.findall(r"`([^`\n]+)`", text):
        looks_random = re.fullmatch(r"[A-Za-z0-9_\-]{16,}", token) and not token.isupper()
        assert not looks_random, "ASSESSOR_CREDENTIALS.md must not contain a secret: " + token[:4] + "..."


# ---------------------------------------------------------------------------
# source code and tests
# ---------------------------------------------------------------------------

def test_no_secret_literals_in_source_or_tests():
    me = os.path.relpath(__file__, ROOT)
    hits = []
    for path in tracked_files():
        if not path.endswith(".py") or path == me:
            continue
        for number, line in enumerate(read(path).splitlines(), 1):
            if LITERAL_ASSIGNMENT.search(line):
                hits.append("%s:%d: %s" % (path, number, line.strip()[:70]))
    assert hits == [], "\n".join(hits)


def test_no_hashes_or_hex_keys_in_tracked_config():
    for path in ("app/config/app.ini", "docker-compose.yml", "ASSESSOR_CREDENTIALS.md"):
        text = read(path)
        assert "pbkdf2:" not in text and "scrypt:" not in text, path
        assert not re.search(r"\b[0-9a-f]{64}\b", text), path


# ---------------------------------------------------------------------------
# the app reads secrets from the environment
# ---------------------------------------------------------------------------

def test_database_connection_settings_come_from_the_environment(app_module, monkeypatch):
    import psycopg2
    seen = {}
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: seen.update(k) or object())
    monkeypatch.setenv("SEWASETU_DB_PASSWORD", "value-from-the-environment")
    monkeypatch.setenv("SEWASETU_DB_HOST", "db.example.internal")
    app_module.get_db()
    assert seen["password"] == "value-from-the-environment"
    assert seen["host"] == "db.example.internal"


def _run_app(code, *args, **env):
    """Run python code with `app` importable, and none of the app secrets inherited."""
    full = {k: v for k, v in os.environ.items()
            if k not in ("SECRET_KEY", "ADMIN_USERNAME", "ADMIN_PASSWORD", "ADMIN_PASSWORD_HASH")}
    full.update(SEWASETU_SKIP_STARTUP_TASKS="1", **env)
    return subprocess.run([sys.executable, "-c", code, *args], cwd=APP_DIR, env=full,
                          capture_output=True, text=True, timeout=60)


def test_app_refuses_to_start_without_a_secret_key():
    result = _run_app("import app")
    assert result.returncode != 0
    assert "SECRET_KEY" in result.stderr


def test_app_refuses_a_short_secret_key():
    assert _run_app("import app", SECRET_KEY="short").returncode != 0


def test_admin_password_can_be_supplied_as_a_hash_in_the_environment():
    from werkzeug.security import generate_password_hash
    chosen = "pw-" + os.urandom(6).hex()
    result = _run_app(
        "import app, sys\n"
        "print('RESULT', app.admin_credentials_valid('ops', sys.argv[1]),"
        " app.admin_credentials_valid('ops', 'wrong'))",
        chosen, SECRET_KEY="k" * 40, ADMIN_USERNAME="ops",
        ADMIN_PASSWORD_HASH=generate_password_hash(chosen))
    assert "RESULT True False" in result.stdout, result.stderr


# ---------------------------------------------------------------------------
# the nightly cron job needs the same environment (cron starts with an empty one)
# ---------------------------------------------------------------------------

def test_crontab_loads_the_container_environment_before_running_the_job():
    cron = read("app/deploy/crontab")
    job = [line for line in cron.splitlines() if "deemed_approval.py" in line][0]
    assert "sewasetu.env" in job and job.index("sewasetu.env") < job.index("deemed_approval.py")


def test_scheduler_entrypoint_exports_the_environment_safely(tmp_path):
    entrypoint = os.path.join(APP_DIR, "deploy", "scheduler-entrypoint.sh")
    assert os.path.exists(entrypoint)
    env_file = tmp_path / "sewasetu.env"
    tricky = "it's a $HOME \"tricky\" `value` ; &"
    env = dict(os.environ, ENV_FILE=str(env_file), CRON_CMD="true", SEWASETU_DB_PASSWORD=tricky)
    done = subprocess.run(["sh", entrypoint], env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert (env_file.stat().st_mode & 0o077) == 0, "env file must be private"
    out = subprocess.run(["sh", "-c", '. "$1"; printf %s "$SEWASETU_DB_PASSWORD"', "sh", str(env_file)],
                         env={"PATH": os.environ["PATH"]}, capture_output=True, text=True)
    assert out.stdout == tricky


# ---------------------------------------------------------------------------
# generating a private .env
# ---------------------------------------------------------------------------

def _generate(tmp_path, name):
    target = tmp_path / name
    script = os.path.join(ROOT, "scripts", "gen-env.sh")
    assert os.path.exists(script)
    done = subprocess.run(["sh", script], env=dict(os.environ, ENV_FILE=str(target)),
                          capture_output=True, text=True, timeout=30)
    return done, target


def test_gen_env_creates_a_private_file_with_strong_random_secrets(tmp_path):
    done, target = _generate(tmp_path, "one.env")
    assert done.returncode == 0, done.stderr
    values = dict(line.split("=", 1) for line in target.read_text().splitlines()
                  if line and not line.startswith("#"))
    assert len(values["SECRET_KEY"]) >= 64
    assert len(values["POSTGRES_PASSWORD"]) >= 24 and len(values["ADMIN_PASSWORD"]) >= 20
    assert values["ADMIN_USERNAME"]
    assert (target.stat().st_mode & 0o077) == 0
    for secret in (values["SECRET_KEY"], values["POSTGRES_PASSWORD"], values["ADMIN_PASSWORD"]):
        assert secret not in done.stdout + done.stderr, "secrets must not be echoed"
    _, other = _generate(tmp_path, "two.env")
    assert other.read_text() != target.read_text()


def test_gen_env_never_overwrites_an_existing_file(tmp_path):
    target = tmp_path / "keep.env"
    target.write_text("POSTGRES_PASSWORD=mine\n")
    done, _ = _generate(tmp_path, "keep.env")
    assert target.read_text() == "POSTGRES_PASSWORD=mine\n"
    assert done.returncode != 0
