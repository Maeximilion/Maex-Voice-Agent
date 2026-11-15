"""Probe (review #207, area G): the new site file is checked only after the
shared Caddy was recreated with it. For a new customer networks.json gains a
network, so `up -d` recreates the container, which loads every site file at
start: a file Caddy refuses takes every customer down until the rollback and
the restart policy bring it back. Issue #687 asked to validate before it goes
live."""

from api.tests.test_new_stack import FakeDocker, _customer, _stack
from scripts import new_stack


def test_the_site_is_validated_before_the_shared_caddy_restarts(tmp_path, monkeypatch):
    root = tmp_path / "srv"
    _stack(root)
    docker = FakeDocker()
    monkeypatch.setattr(new_stack, "run", docker)

    new_stack.create(_customer(tmp_path), "a.example.org", root, False)

    texts = [" ".join(c) for c in docker.commands]
    up = next(i for i, t in enumerate(texts) if "maex-caddy" in t and " up -d" in t)
    validate = next(i for i, t in enumerate(texts) if "caddy validate" in t)
    assert validate < up, texts[up:validate + 1]
