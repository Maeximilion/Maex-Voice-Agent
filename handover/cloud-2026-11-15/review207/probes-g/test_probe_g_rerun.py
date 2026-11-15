"""Probe (review #207, area G): a rerun keeps .env byte for byte, prints no
secret value, and a refused rerun keeps the site."""

from api.tests.test_new_stack import FakeDocker, _customer, _stack
from scripts import new_stack


def test_rerun_keeps_env_and_prints_no_secret(tmp_path, monkeypatch, capsys):
    root = tmp_path / "srv"
    stack = _stack(root)
    monkeypatch.setattr(new_stack, "run", FakeDocker())
    new_stack.create(_customer(tmp_path), "a.example.org", root, False)
    env = (stack / ".env").read_text()
    first = capsys.readouterr()
    new_stack.create(_customer(tmp_path), "a.example.org", root, False)
    second = capsys.readouterr()
    assert (stack / ".env").read_text() == env
    values = new_stack.read_env(stack / ".env")
    for name in new_stack.FRESH_VARIABLES + ("DATABASE_URL", "MONITOR_PUSH_TOKEN"):
        for out in (first.out, first.err, second.out, second.err):
            assert values[name] not in out, name
    assert "Push server user" not in second.out
