"""scripts/new_stack.py: a customer's stack, its .env and its site in the shared Caddy.

The Docker steps are not run here (no docker CLI in the test container); they
were run against the workbench's Docker when the script was written. What is
checked: every secret fresh and distinct, the .env quoting round trip, the
customer layout of Compose, and the cross-site check of docs/23 §8 on site
files rendered from the real deploy/Caddyfile for two customers.
"""

import json
import stat

import pytest

from api.domain.customers.phone import E164
from api.models.tenants import CUSTOMER_KEY
from api.tests.test_commands import REPO_ROOT
from api.tests.test_compose_ports import _load, _services
from scripts import new_stack

CADDYFILE = (REPO_ROOT / "deploy" / "Caddyfile").read_text()
EXAMPLE = (REPO_ROOT / ".env.example").read_text()
CUSTOMER = REPO_ROOT / "deploy" / "customer.example.json"


def _env(key: str, domain: str, name: str = "Pizzeria") -> dict[str, str]:
    return new_stack.fresh_env({"key": key, "name": name}, domain)


def _site(key: str, domain: str) -> str:
    return new_stack.site_text(CADDYFILE, key, _env(key, domain))


def test_every_secret_is_fresh_and_none_repeats():
    first, second = _env("alpha", "a.example.org"), _env("beta", "b.example.org")
    made = [first[name] for name in new_stack.SECRETS]
    made += [second[name] for name in new_stack.SECRETS]
    assert all(len(value) == 64 for value in made)
    assert len(set(made)) == len(made)
    assert first["DATABASE_URL"].startswith("postgresql+psycopg://maex_app:")
    assert first["POSTGRES_PASSWORD"] in first["OWNER_DATABASE_URL"]
    assert first["POSTGRES_PASSWORD"] not in first["DATABASE_URL"]
    assert first["ENV"] == "prod"
    assert first["COMPOSE_PROJECT_NAME"] == "maex-alpha"
    assert first["BACKUP_DOCKER_NETWORK"] == "maex-alpha_backend"


def test_the_env_keeps_its_comments_and_reads_back(tmp_path):
    values = _env("alpha", "a.example.org", name='Luigi\'s "Pizza" $5 # Nord')
    path = tmp_path / ".env"
    new_stack.write_private(path, new_stack.env_text(EXAMPLE, values), 0o600)
    text = path.read_text()
    read = new_stack.read_env(path)
    assert {name: read[name] for name in values} == values
    # Each name once: the active line replaced, a commented one taken over.
    for name in ("DATABASE_URL", "OWNER_DATABASE_URL", "N8N_ENCRYPTION_KEY"):
        assert text.count(f"\n{name}=") == 1, name
    assert "# ── Datenbank" in text
    assert "COMPOSE_FILE=" in text  # not in the example: appended
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_read_env_takes_the_operators_spellings(tmp_path):
    path = tmp_path / ".env"
    path.write_text('A=\'$2a$14$abc\'\nB=plain\n# C=commented\nD="x\\"y$$z"\n')
    assert new_stack.read_env(path) == {"A": "$2a$14$abc", "B": "plain", "D": 'x"y$z'}


def test_two_customers_get_sites_of_their_own():
    sites = {
        "alpha": _site("alpha", "a.example.org"),
        "beta": _site("beta", "b.example.org"),
    }
    assert new_stack.check_sites(sites) == []
    alpha = sites["alpha"]
    assert "reverse_proxy maex-alpha-api-1:8000" in alpha
    assert "reverse_proxy maex-alpha-n8n-1:5678" in alpha
    assert "agent.a.example.org {" in alpha
    assert "(transport-alpha)" in alpha and "import transport-alpha" in alpha
    # The security headers and the public path list come along unchanged.
    assert "frame-ancestors 'none'" in alpha
    assert "@public path /gui /gui/* /v1/kitchen/* /health" in alpha
    assert "{$" not in alpha and "{env." not in alpha


@pytest.mark.parametrize(
    "tamper, expected",
    [
        (
            lambda a, b: a.replace("maex-alpha-api-1", "maex-beta-api-1"),
            "not to its own",
        ),
        (lambda a, b: a.replace("maex-alpha-push-1:80", "push:80"), "not to its own"),
        (lambda a, b: a.replace("agent.a.example.org", "agent.b.example.org"), "host"),
        (lambda a, b: _with_secret_of(a, b), "same proxy secret"),
    ],
)
def test_the_cross_site_check_finds_a_site_that_reaches_another_stack(tamper, expected):
    alpha, beta = _site("alpha", "a.example.org"), _site("beta", "b.example.org")
    problems = new_stack.check_sites({"alpha": tamper(alpha, beta), "beta": beta})
    assert any(expected in problem for problem in problems), problems


def _with_secret_of(alpha: str, beta: str) -> str:
    _, (secret,), _ = new_stack._site_facts(beta)
    _, (own,), _ = new_stack._site_facts(alpha)
    return alpha.replace(own, secret)


def test_a_changed_caddyfile_stops_the_rendering():
    with pytest.raises(new_stack.Refused, match="reverse_proxy api:8000"):
        new_stack.site_text(
            CADDYFILE.replace("reverse_proxy api:8000", "reverse_proxy app:8000"),
            "alpha",
            _env("alpha", "a.example.org"),
        )


def test_the_shared_caddy_joins_each_stacks_edge_network():
    compose = json.loads(new_stack.networks_file(["beta", "alpha"]))
    assert compose["services"]["caddy"]["networks"] == [
        "maex-alpha_edge",
        "maex-beta_edge",
    ]
    assert compose["networks"]["maex-beta_edge"] == {"external": True}


def test_the_customer_layout_has_no_caddy_and_an_ops_network_for_the_api():
    layout = _load(REPO_ROOT / "deploy" / "docker-compose.customer.yml")
    services = _services(layout)
    assert services["caddy"] == {"profiles": ["single"]}
    on_ops = {name for name, s in services.items() if "ops" in s.get("networks", [])}
    assert on_ops == {"api"}
    assert layout["networks"]["ops"] == {"internal": True}
    assert new_stack.COMPOSE_FILES.endswith("deploy/docker-compose.customer.yml")


def test_the_shared_caddy_matches_the_stacks_caddy():
    shared = _services(_load(REPO_ROOT / "deploy" / "caddy" / "docker-compose.yml"))
    single = _services(_load(REPO_ROOT / "deploy" / "docker-compose.prod.yml"))
    for setting in ("image", "cap_drop", "cap_add", "read_only", "security_opt"):
        assert shared["caddy"][setting] == single["caddy"][setting], setting


def test_the_key_rule_is_the_databases():
    assert new_stack.CUSTOMER_KEY.pattern == CUSTOMER_KEY
    assert new_stack.E164.pattern == E164.pattern


@pytest.mark.parametrize(
    "phone", ["0721 5551234", "+49 721 5551234", "+4972215551234\n"]
)
def test_a_team_number_not_in_e164_is_refused_before_anything_starts(tmp_path, phone):
    spec = json.loads(CUSTOMER.read_text()) | {"team_phone": phone}
    path = tmp_path / "customer.json"
    path.write_text(json.dumps(spec))
    argv = [str(path), "--domain", "a.example.org", "--root", str(tmp_path / "srv")]
    assert new_stack.main(argv) == 2
    assert not (tmp_path / "srv").exists()


def test_an_e164_team_number_becomes_team_phone():
    spec = {"key": "pilot", "name": "x", "team_phone": "+4972215551234"}
    assert new_stack.fresh_env(spec, "a.example.org")["TEAM_PHONE"] == "+4972215551234"


def test_wrong_input_exits_2_and_a_dry_run_writes_nothing(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"key": "Pilot", "name": "x"}))
    root = tmp_path / "srv"
    assert (
        new_stack.main([str(bad), "--domain", "a.example.org", "--root", str(root)])
        == 2
    )
    assert new_stack.main([str(CUSTOMER), "--domain", "A_B", "--root", str(root)]) == 2
    bad.write_text(json.dumps({"key": "pilot", "name": "x\nENV=dev"}))
    assert (
        new_stack.main([str(bad), "--domain", "a.example.org", "--root", str(root)])
        == 2
    )
    trailing = [str(CUSTOMER), "--domain", "a.example.org\n", "--root", str(root)]
    assert new_stack.main(trailing) == 2
    argv = [
        str(CUSTOMER),
        "--domain",
        "a.example.org",
        "--root",
        str(root),
        "--dry-run",
    ]
    assert new_stack.main(argv) == 0
    assert not root.exists()
    assert "maex-pilot" in capsys.readouterr().out


def test_a_domain_another_site_has_is_refused(tmp_path):
    sites = tmp_path / "caddy" / "sites"
    sites.mkdir(parents=True)
    (sites / "other.caddy").write_text(_site("other", "a.example.org"))
    argv = [
        str(CUSTOMER),
        "--domain",
        "a.example.org",
        "--root",
        str(tmp_path),
        "--dry-run",
    ]
    assert new_stack.main(argv) == 2
    assert new_stack.main(["--check-sites", "--root", str(tmp_path)]) == 0


def test_a_rerun_with_another_domain_is_refused_before_anything_starts(tmp_path):
    stack = tmp_path / "pilot"
    stack.mkdir()
    values = new_stack.fresh_env({"key": "pilot", "name": "x"}, "a.example.org")
    (stack / ".env").write_text(new_stack.env_text(EXAMPLE, values))
    argv = [str(CUSTOMER), "--domain", "b.example.org", "--root", str(tmp_path)]
    assert new_stack.main(argv) == 2
    assert not (tmp_path / "caddy").exists()
