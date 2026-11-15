"""scripts/remove_stack.py: a leaving customer's stack goes, after its export (#576).

No Docker here: every subprocess goes through `remove_stack.run`, which a fake
replaces. The fake keeps the project's containers, volumes, networks and images
and answers the read-only `ls` calls from them, so what the script lists and
removes is checked against what "Docker" holds. The export job itself runs
against the test database in one test, so the manifest contract between the two
is the real one.
"""

import hashlib
import json
import shutil
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session, sessionmaker

from api.jobs import offboarding
from api.models import Tenant
from api.tests.test_commands import REPO_ROOT
from scripts import new_stack, remove_stack
from scripts.seed import seed

CADDYFILE = (REPO_ROOT / "deploy" / "Caddyfile").read_text()
EXAMPLE = (REPO_ROOT / ".env.example").read_text()
NAME = "Pizzeria Nord Inhaber Müller"


class FakeDocker:
    """What Docker holds for each project, and every command it was given."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.held: dict[str, dict[str, set[str]]] = {}
        self.fail: str | None = None
        self.keep_after_down: set[str] = set()
        self.on_export: Callable[[Path, str], None] = write_export

    def project(self, key: str) -> None:
        name = f"maex-{key}"
        self.held[name] = {
            "container": {f"{name}-{s}-1" for s in ("db", "api", "n8n", "push")},
            "volume": {f"{name}_{v}" for v in ("pgdata", "n8ndata", "recordings")},
            "network": {f"{name}_{n}" for n in ("backend", "edge", "ops")},
            "image": {"sha256:" + key},
        }

    def __call__(self, command, cwd, **options):
        command = [str(part) for part in command]
        self.commands.append(command)
        joined = " ".join(command)
        if self.fail and self.fail in joined:
            raise new_stack.Refused(f"failed with exit code 1: {joined}")
        out = ""
        if command[:2] in (
            ["docker", "ps"],
            ["docker", "volume"],
            ["docker", "network"],
            ["docker", "image"],
        ) and ("ls" in command or command[1] == "ps"):
            kind = "container" if command[1] == "ps" else command[1]
            label = next(c for c in command if c.startswith("label="))
            project = label.split("=", 2)[2]
            out = "\n".join(sorted(self.held.get(project, {}).get(kind, set())))
        elif "down" in command:
            project = self._project_of(Path(cwd))
            for names in self.held.get(project, {}).values():
                names.intersection_update(self.keep_after_down)
        elif command[:3] in (
            ["docker", "rm", "-f"],
            ["docker", "volume", "rm"],
            ["docker", "network", "rm"],
        ):
            for held in self.held.values():
                for names in held.values():
                    names.difference_update(command[3:])
        elif "api.jobs.offboarding" in command:
            mount = command[command.index("-v") + 1].split(":")[0]
            self.on_export(Path(mount), command[-1])
        elif command[:2] == ["rm", "-rf"]:
            shutil.rmtree(command[-1])
        return subprocess.CompletedProcess(command, 0, stdout=out)

    @staticmethod
    def _project_of(stack: Path) -> str:
        return new_stack.read_env(stack / ".env")["COMPOSE_PROJECT_NAME"]

    def mutating(self) -> list[list[str]]:
        reads = (
            ["docker", "ps"],
            ["docker", "volume", "ls"],
            ["docker", "network", "ls"],
            ["docker", "image", "ls"],
        )
        return [c for c in self.commands if not any(c[: len(r)] == r for r in reads)]


def write_export(folder: Path, key: str, other_tenants: int = 0) -> None:
    """What api.jobs.offboarding writes, in small."""
    folder.mkdir(parents=True, exist_ok=True)
    content = "order_id;total_cents\nx;1200\n"
    (folder / "orders.csv").write_text(content)
    digest = hashlib.sha256(content.encode()).hexdigest()
    manifest = {
        "customer_key": key,
        "exported_at": "2026-11-15T10:00:00+00:00",
        "other_tenants": other_tenants,
        "files": {"orders.csv": {"rows": 1, "sha256": digest}},
    }
    (folder / "manifest.json").write_text(json.dumps(manifest))


def _stack(root: Path, key: str, domain: str, **extra: str) -> None:
    stack = root / key
    (stack / "backups").mkdir(parents=True)
    env = new_stack.fresh_env({"key": key, "name": NAME}, domain) | extra
    (stack / ".env").write_text(new_stack.env_text(EXAMPLE, env))
    for day in ("20261113", "20261114"):
        (stack / "backups" / f"maex_maex_agent_{day}T020000Z.dump.gpg").write_text("x")
    sites = root / "caddy" / "sites"
    sites.mkdir(parents=True, exist_ok=True)
    (sites / f"{key}.caddy").write_text(new_stack.site_text(CADDYFILE, key, env))


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(remove_stack, "run", fake)
    return fake


@pytest.fixture
def root(tmp_path, docker) -> Path:
    root = tmp_path / "srv"
    _stack(root, "alpha", "a.example.org")
    _stack(root, "beta", "b.example.org")
    docker.project("alpha")
    docker.project("beta")
    return root


def _main(root: Path, *args: str) -> int:
    return remove_stack.main(["alpha", "--root", str(root), *args])


def test_the_dry_run_is_the_default_and_lists_exactly_what_would_go(
    root, docker, capsys
):
    write_export(root / "offboarding" / "alpha", "alpha")
    assert _main(root) == 0
    out = capsys.readouterr().out
    for name in (
        "volume maex-alpha_pgdata",
        "volume maex-alpha_n8ndata",
        "container maex-alpha-db-1",
        "network maex-alpha_edge",
        f"site file {root}/caddy/sites/alpha.caddy",
        f"folder {root}/alpha",
        f"2 backup(s) in {root}/alpha/backups",
    ):
        assert name in out, name
    assert "beta" not in out
    assert docker.mutating() == []
    assert (root / "alpha" / ".env").exists()
    assert _main(root, "--dry-run") == 0


def test_without_an_export_nothing_is_removed(root, docker, capsys):
    assert _main(root) == 1
    assert "would refuse: no export" in capsys.readouterr().out
    assert _main(root, "--confirm", "alpha") == 1
    assert "run with --export first" in capsys.readouterr().err
    assert docker.mutating() == []
    assert (root / "alpha").is_dir() and (root / "caddy/sites/alpha.caddy").is_file()


def test_the_key_must_be_typed_again_and_valid(root, docker, capsys):
    write_export(root / "offboarding" / "alpha", "alpha")
    assert _main(root, "--confirm", "beta") == 2
    assert _main(root, "--confirm", "Alpha") == 2
    assert remove_stack.main(["caddy", "--root", str(root)]) == 2
    with pytest.raises(SystemExit):
        _main(root, "--dry-run", "--confirm", "alpha")
    assert docker.mutating() == []


def test_the_removal_takes_the_stack_and_leaves_the_others(root, docker, capsys):
    write_export(root / "offboarding" / "alpha", "alpha")
    beta_env = (root / "beta" / ".env").read_bytes()
    beta_site = (root / "caddy/sites/beta.caddy").read_bytes()

    assert _main(root, "--confirm", "alpha") == 0

    steps = [" ".join(c) for c in docker.mutating()]
    order = [
        "--profile * stop",
        "api.jobs.offboarding",
        "networks.json up -d",
        "caddy validate",
        "caddy reload",
        "--profile * down --volumes",
    ]
    found = [next(i for i, s in enumerate(steps) if part in s) for part in order]
    assert found == sorted(found), steps
    assert any(s.startswith("rm -rf --one-file-system") for s in steps)
    assert not (root / "alpha").exists()
    assert not (root / "caddy/sites/alpha.caddy").exists()
    assert json.loads((root / "caddy/networks.json").read_text())["services"]["caddy"][
        "networks"
    ] == ["maex-beta_edge"]
    assert not any(
        docker.held["maex-alpha"][k] for k in ("container", "volume", "network")
    )
    # The other customer is as it was.
    assert (root / "beta" / ".env").read_bytes() == beta_env
    assert (root / "caddy/sites/beta.caddy").read_bytes() == beta_site
    assert all(docker.held["maex-beta"].values())
    assert not any("maex-beta" in s or "/beta" in s for s in steps)

    ledger = (root / "offboarding" / "removed.jsonl").read_text()
    (entry,) = [json.loads(line) for line in ledger.splitlines()]
    assert entry["customer_key"] == "alpha" and entry["project"] == "maex-alpha"
    assert entry["export"]["rows"] == {"orders.csv": 1}
    assert "volume maex-alpha_pgdata" in entry["removed"]
    assert any("crontab" in line for line in entry["by_hand"])
    # No name, number or secret of the customer in the proof.
    assert NAME not in ledger and "Müller" not in ledger
    assert new_stack.read_env(root / "beta" / ".env")["AGENT_API_TOKEN"] not in ledger
    assert (root / "offboarding" / "removed.jsonl").stat().st_mode & 0o777 == 0o640

    # A rerun finds nothing and writes no second line.
    capsys.readouterr()
    assert _main(root, "--confirm", "alpha") == 0
    assert "nothing left" in capsys.readouterr().out
    assert (root / "offboarding" / "removed.jsonl").read_text() == ledger


def test_a_refused_caddy_puts_the_site_back_and_removes_nothing(root, docker):
    write_export(root / "offboarding" / "alpha", "alpha")
    site = (root / "caddy/sites/alpha.caddy").read_bytes()
    docker.fail = "caddy reload"
    assert _main(root, "--confirm", "alpha") == 1
    assert (root / "caddy/sites/alpha.caddy").read_bytes() == site
    nets = json.loads((root / "caddy/networks.json").read_text())["services"]["caddy"][
        "networks"
    ]
    assert nets == ["maex-alpha_edge", "maex-beta_edge"]
    assert (root / "alpha" / ".env").is_file()
    assert not any("down" in c for c in docker.commands)
    assert not (root / "offboarding" / "removed.jsonl").exists()

    # Fixed and run again: the rest goes.
    docker.fail = None
    assert _main(root, "--confirm", "alpha") == 0
    assert not (root / "alpha").exists()


def test_what_down_left_goes_by_the_projects_label(root, docker):
    write_export(root / "offboarding" / "alpha", "alpha")
    docker.keep_after_down = {"maex-alpha_recordings"}
    assert _main(root, "--confirm", "alpha") == 0
    assert ["docker", "volume", "rm", "maex-alpha_recordings"] in docker.commands
    assert not docker.held["maex-alpha"]["volume"]


@pytest.mark.parametrize(
    "spoil, reason",
    [
        (
            lambda f: (f / "orders.csv").write_text("changed"),
            "changed since the export",
        ),
        (lambda f: write_export(f, "beta"), "not the export of 'alpha'"),
        (lambda f: write_export(f, "alpha", other_tenants=1), "other tenant"),
        (lambda f: (f / "manifest.json").write_text("{"), "not JSON"),
    ],
)
def test_an_export_that_is_not_whole_or_not_this_customers_is_refused(
    root, docker, capsys, spoil, reason
):
    folder = root / "offboarding" / "alpha"
    write_export(folder, "alpha")
    spoil(folder)
    assert _main(root, "--confirm", "alpha") == 1
    assert reason in capsys.readouterr().err
    assert docker.mutating() == []
    assert (root / "alpha").is_dir()


def test_backups_outside_the_stack_folder_are_refused(tmp_path, docker, capsys):
    root = tmp_path / "srv"
    _stack(root, "alpha", "a.example.org", BACKUP_DIR=str(tmp_path / "shared-backups"))
    docker.project("alpha")
    write_export(root / "offboarding" / "alpha", "alpha")
    assert _main(root) == 1
    assert "lies outside" in capsys.readouterr().err
    assert _main(root, "--confirm", "alpha") == 1
    assert docker.mutating() == []


def test_a_folder_of_another_project_is_not_touched(tmp_path, docker, capsys):
    root = tmp_path / "srv"
    _stack(root, "alpha", "a.example.org", COMPOSE_PROJECT_NAME="maex-other")
    write_export(root / "offboarding" / "alpha", "alpha")
    assert _main(root, "--confirm", "alpha") == 1
    assert "another project" in capsys.readouterr().err
    assert docker.mutating() == []


@pytest.fixture
def stack_db(migrated_db_url, monkeypatch):
    engine = create_engine(migrated_db_url)
    monkeypatch.setattr(offboarding, "SessionLocal", sessionmaker(bind=engine))
    with Session(engine) as session:
        tenant_id = uuid.UUID(seed(session, "Alpha", "Europe/Berlin").tenant_id)
        session.execute(
            update(Tenant).where(Tenant.id == tenant_id).values(customer_key="alpha")
        )
        session.commit()
    yield
    engine.dispose()


def test_export_runs_the_job_in_the_stack_and_the_removal_accepts_its_manifest(
    root, docker, stack_db, capsys
):
    def in_the_stack(folder: Path, key: str) -> None:
        assert offboarding.main(["export", str(folder), "--key", key]) == 0

    docker.on_export = in_the_stack
    assert _main(root, "--export") == 0
    (command,) = [c for c in docker.commands if "api.jobs.offboarding" in c]
    assert command[:5] == ["docker", "compose", "run", "--rm", "-T"]
    assert f"{root.resolve()}/offboarding/alpha:/export" in command
    folder = root / "offboarding" / "alpha"
    assert folder.stat().st_mode & 0o777 == 0o700
    assert remove_stack.check_export(folder, "alpha")["other_tenants"] == 0
    assert "menu_items.csv: 0 rows" in capsys.readouterr().out
    assert _main(root, "--confirm", "alpha") == 0
    assert not (root / "alpha").exists()
