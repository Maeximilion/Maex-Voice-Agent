"""`deploy/backup.cron`: when the backups run and in which folders (T-9.3, #888 D2).

Apart from test_scripts_backup.py, whose tests need gpg and a Postgres client and
are skipped without them: these read and run the cron lines only.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_cron_file_runs_the_backup_daily_at_three():
    lines = [
        line
        for line in (REPO_ROOT / "deploy" / "backup.cron").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]

    # A customer stack's database and the operator stack's own (#827 F9).
    job, operator = lines
    assert job.startswith("0 3 * * * ")
    assert operator.startswith("15 3 * * * cd /srv/maex/ops && ")
    for line in lines:
        assert "scripts/backup.sh" in line
        # The shell opens the log before the script runs; without the folder no backup.
        assert line.index("mkdir -p backups") < line.index(">> backups/")


def test_the_cron_line_dumps_every_customer_stack_of_the_root(tmp_path):
    """#888 D2: the line named one folder, so no stack of `new_stack.py` was
    dumped. Run the line itself over a root with stacks and other folders."""
    line = (REPO_ROOT / "deploy" / "backup.cron").read_text().splitlines()
    (job,) = [row for row in line if row.startswith("0 3 * * * ")]
    command = job.removeprefix("0 3 * * * ").replace("/srv/maex/", f"{tmp_path}/")

    def stack(name: str, env: bool = True, script: str = "exit 0") -> None:
        folder = tmp_path / name / "scripts"
        folder.mkdir(parents=True)
        (folder / "backup.sh").write_text(f"pwd > {tmp_path}/ran_{name}\n{script}\n")
        if env:
            (tmp_path / name / ".env").write_text("")

    stack("alpha", script="exit 1")  # a failing stack does not stop the next
    stack("beta")
    stack("ops")  # its own line at 03:15
    stack("half", env=False)
    (tmp_path / "caddy" / "sites").mkdir(parents=True)

    subprocess.run(["sh", "-c", command], check=False, cwd="/")

    ran = sorted(path.name for path in tmp_path.glob("ran_*"))
    assert ran == ["ran_alpha", "ran_beta"]
    assert (tmp_path / "ran_beta").read_text().strip() == str(tmp_path / "beta")
    assert (tmp_path / "beta" / "backups" / "backup.log").exists()
