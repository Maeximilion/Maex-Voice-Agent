"""`docker-compose.yml`: der api-Service traegt jeden Pfad, den die Suite liest.

Die Container-Mounts sind die einzige Stelle, an der `make test` von der CI abweichen
kann, ohne dass am Code etwas fehlt: die CI laeuft auf einem vollen Checkout, der
Container sieht nur, was gemountet ist. Fehlt ein Pfad, faellt pytest lokal mit
FileNotFoundError aus, waehrend die CI gruen bleibt - der Fehler zeigt sich also genau
dort, wo niemand hinsieht.

Dieser Test prueft statisch und ohne Docker, was die Suite an Repo-Pfaden anfasst:
jeden Dateiverweis aus den Slash-Befehlen und jeden Pfad, den ein Test ueber
`parents[2]` oder `REPO_ROOT` oeffnet. Jeder davon muss in einem Mount liegen.
"""

import posixpath
import re
from pathlib import PurePosixPath

import yaml

from api.tests.test_commands import (
    DIR_REF_RE,
    FILE_REF_RE,
    GENERATED_DIRS,
    REPO_ROOT,
    _command_files,
)

COMPOSE = REPO_ROOT / "docker-compose.yml"
# Verzeichnisse mit Python, das die Suite ausfuehrt - dort stehen die Pfadzugriffe.
SOURCE_DIRS = ("api", "evals", "sim", "scripts")
# `... parents[2] / "deploy" / "Caddyfile"` wird als "deploy/Caddyfile" gelesen: die
# ganze Kette, nicht nur das erste Segment - sonst faende der Test `.claude/commands`
# nicht wieder, das genau deshalb enger gemountet ist als `.claude`.
ROOT_PATH_RE = re.compile(r'(?:parents\[2\]|REPO_ROOT)((?:\s*/\s*"[^"]+")+)')
SEGMENT_RE = re.compile(r'"([^"]+)"')
# Ein benanntes Volume ist ein blosser Name: keine Trennzeichen, kein Punkt-Praefix.
NAMED_VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
# Pfade, die niemals in den Container gehoeren: die Umgebungsdatei und alles unter
# .claude/ - dort liegen im Haupt-Checkout die Worktrees samt eigener .env.
GESCHUETZT = (".env", ".claude")


def _geraet(name: str) -> str | None:
    """Der Host-Pfad hinter einem benannten Volume, falls es in Wahrheit ein Bind ist.

    Der lokale Treiber kann ein Volume an ein Host-Verzeichnis haengen
    (`driver_opts: {type: none, o: bind, device: ...}`). Ein solches Volume traegt
    denselben Baum herein wie ein Bind, sieht im Dienst aber aus wie ein blosser
    Name - ohne diese Aufloesung bliebe es ungeprueft.
    """
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    definition = (compose.get("volumes") or {}).get(name) or {}
    return (definition.get("driver_opts") or {}).get("device")


def _quelle_und_ziel(entry: str | dict) -> tuple[str | None, str]:
    """Host-Pfad und Container-Ziel eines Volume-Eintrags, oder None bei einem Volume.

    Die Unterscheidung laeuft ueber die Form, nicht ueber das Praefix `./`: ein
    benanntes Volume ist ein blosser Name ohne Trennzeichen (`pgdata:/var/...`),
    alles andere ist ein Bind und muss geprueft werden - auch `../nachbar/.env`
    und absolute Pfade, die ohne diese Regel unbesehen durchgingen. Ein benanntes
    Volume mit `driver_opts.device` zaehlt als Bind auf genau dieses Geraet.
    """
    if isinstance(entry, dict):  # lange Compose-Schreibweise
        quelle = entry.get("source") or ""
        if entry.get("type", "bind") != "bind":
            return _geraet(quelle), entry.get("target", "")
        return quelle or None, entry.get("target", "")
    praefix, rest = "", entry
    if WINDOWS_ABS_RE.match(entry):  # C:\... - der Doppelpunkt gehoert zum Laufwerk
        praefix, rest = entry[:2], entry[2:]
    kopf, _, schwanz = rest.partition(":")
    ziel = schwanz.split(":", 1)[0]
    if not praefix and NAMED_VOLUME_RE.match(kopf):
        return _geraet(kopf), ziel
    return praefix + kopf, ziel


def _dienste() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def _binds(dienst: str = "api") -> set[tuple[str, str]]:
    """Alle Binds eines Dienstes als (normalisierte Quelle, Ziel).

    Normalisiert, weil Docker den Pfad aufloest und jede Pruefung darunter sonst rein
    lexikalisch waere: `./.claude/commands/../../.env` sieht wie ein Nachfahre von
    `commands/` aus und ist in Wahrheit die `.env` im Repo-Wurzelverzeichnis.
    """
    binds = set()
    for entry in _dienste()[dienst].get("volumes", []):
        host, ziel = _quelle_und_ziel(entry)
        if host:
            binds.add((posixpath.normpath(host.replace("\\", "/")).rstrip("/"), ziel))
    return binds


def _bind_quellen(dienst: str = "api") -> set[str]:
    return {quelle for quelle, _ in _binds(dienst)}


def _dateiquellen(dienst: str) -> set[str]:
    """Host-Dateien, die ein Dienst ueber `secrets:` oder `configs:` bekommt.

    Beides reicht eine Datei in den Container, ohne in `volumes:` aufzutauchen:
    `secrets: {repo_env: {file: .env}}` plus `api.secrets: [repo_env]` legt die
    Umgebungsdatei unter /run/secrets ab. Fuer den Waechter ist das derselbe Fall
    wie ein Bind und muss durch dieselbe Pruefung.
    """
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    quellen = set()
    for art in ("secrets", "configs"):
        definitionen = compose.get(art) or {}
        for eintrag in _dienste()[dienst].get(art) or []:
            name = eintrag.get("source") if isinstance(eintrag, dict) else eintrag
            datei = (definitionen.get(name) or {}).get("file")
            if datei:
                quellen.add(posixpath.normpath(datei.replace("\\", "/")).rstrip("/"))
    return quellen


def _nicht_aufloesbar(quelle: str) -> bool:
    """`${PWD}` und `$HOME` loest Compose auf, `yaml.safe_load` nicht.

    Was hier als Variable ankommt, kann nach der Aufloesung auf alles zeigen - auch
    auf die Repo-Wurzel. Ungeprueft durchlassen waere die groesste der Luecken.
    """
    return "$" in quelle


def _zeigt_aus_dem_repo(quelle: str) -> bool:
    """Alles, was nicht unterhalb der Repo-Wurzel liegt - von hier aus nicht pruefbar.

    Vier Formen: absolut, Laufwerksbuchstabe, oberhalb der Wurzel (`..`) und das
    Heimatverzeichnis (`~`), das Compose aufloest und das sonst als harmloser
    Pfad im Repo durchginge.
    """
    return quelle.startswith(("/", "~")) or bool(
        WINDOWS_ABS_RE.match(quelle) or PurePosixPath(quelle).parts[:1] == ("..",)
    )


def _api_mounts() -> set[str]:
    """Die Repo-Pfade, die im Container wirklich unter `/app/<pfad>` liegen.

    Das Ziel gehoert zur Pruefung: `./deploy:/tmp/deploy` mountet zwar `deploy`,
    aber nicht dorthin, wo die Suite es sucht (REPO_ROOT ist `/app`). Ohne den
    Abgleich haette ein geaendertes Ziel als Abdeckung gezaehlt.
    """
    return {
        quelle
        for quelle, ziel in _binds()
        if not _zeigt_aus_dem_repo(quelle)
        and not _nicht_aufloesbar(quelle)
        and posixpath.normpath(ziel) == f"/app/{quelle}"
    }


def _is_mounted(ref: str, mounts: set[str]) -> bool:
    """Ein Pfad ist abgedeckt, wenn er selbst oder eines seiner Elternteile gemountet ist."""
    parts = PurePosixPath(ref.strip("/")).parts
    return any("/".join(parts[:tiefe]) in mounts for tiefe in range(1, len(parts) + 1))


def _refs_aus_befehlen() -> set[str]:
    refs: set[str] = set()
    for path in _command_files():
        text = path.read_text(encoding="utf-8")
        refs |= set(FILE_REF_RE.findall(text))
        refs |= {ref for ref in DIR_REF_RE.findall(text) if ref not in GENERATED_DIRS}
    return refs


def _refs_aus_tests() -> set[str]:
    refs: set[str] = set()
    for verzeichnis in SOURCE_DIRS:
        for datei in (REPO_ROOT / verzeichnis).rglob("*.py"):
            for kette in ROOT_PATH_RE.findall(datei.read_text(encoding="utf-8")):
                refs.add("/".join(SEGMENT_RE.findall(kette)))
    return refs


def test_slash_befehle_verweisen_nur_auf_gemountete_pfade():
    mounts = _api_mounts()
    fehlend = sorted(
        ref for ref in _refs_aus_befehlen() if not _is_mounted(ref, mounts)
    )
    assert not fehlend, (
        f"von .claude/commands verwiesen, aber im api-Container nicht gemountet: {fehlend}"
    )


def test_tests_lesen_nur_gemountete_pfade():
    mounts = _api_mounts()
    fehlend = sorted(ref for ref in _refs_aus_tests() if not _is_mounted(ref, mounts))
    assert not fehlend, (
        f"ueber REPO_ROOT geoeffnet, aber im api-Container nicht gemountet: {fehlend}"
    )


def test_die_compose_datei_mountet_sich_selbst():
    """Ohne diesen Mount findet dieser Test die Datei im Container nicht."""
    assert _is_mounted("docker-compose.yml", _api_mounts())


def test_dispatcher_braucht_die_testpfade_nicht():
    """Der dispatcher fuehrt weder pytest noch ruff aus - seine Mounts bleiben klein."""
    hosts = {quelle for quelle, _ in _binds("dispatcher")}
    assert hosts == {"api", "scripts", "db"}


def _teile(pfad: str) -> tuple[str, ...]:
    return PurePosixPath(pfad).parts if pfad not in (".", "") else ()


def _ist_unter(quelle: str, ordner: str) -> bool:
    teile, oben = _teile(quelle), _teile(ordner)
    return teile[: len(oben)] == oben


def _beruehrt(quelle: str, geschuetzt: str) -> bool:
    """Wahr, wenn einer der beiden Pfade im anderen liegt - in welcher Richtung auch immer.

    Die Richtung nach oben ist die, die leicht vergessen wird: `.` ist der
    gemeinsame Vorfahr von allem und traegt damit `.env` und die Worktrees
    genauso herein wie ein Mount, der direkt auf sie zeigt.
    """
    teile, geschuetzte = _teile(quelle), _teile(geschuetzt)
    tiefe = min(len(teile), len(geschuetzte))
    return teile[:tiefe] == geschuetzte[:tiefe]


def _traegt_geheimnisse(mount: str) -> bool:
    """Wahr, sobald der Mount einen geschuetzten Pfad beruehrt.

    Geschuetzt sind `.env` und `.claude/` - dort liegen im Haupt-Checkout die
    Worktrees mit ihren eigenen `.env`-Dateien. Geprueft wird die Beruehrung in
    beide Richtungen, nicht die Gleichheit: `.claude/worktrees` zeigt hinein,
    `.` umfasst alles, und beides traegt dieselben Geheimnisse herein.

    Einzige Ausnahme ist `.claude/commands`, das die Testsuite wirklich liest.
    Was aus dem Repo herauszeigt, ist ohnehin tabu: dort liegen die
    Nachbar-Checkouts, und absolute Pfade sind von hier aus nicht pruefbar.
    """
    if _zeigt_aus_dem_repo(mount) or _nicht_aufloesbar(mount):
        return True
    if PurePosixPath(mount).name == ".env":  # auch tiefer liegende .env
        return True
    if _ist_unter(mount, ".claude/commands"):  # nach der .env-Pruefung, nicht davor
        return False
    return any(_beruehrt(mount, pfad) for pfad in GESCHUETZT)


def _aufgeloest(quelle: str) -> str | None:
    """Der Pfad hinter einem Symlink, repo-relativ - oder None, wenn keiner im Weg ist.

    `posixpath.normpath` arbeitet rein lexikalisch und kann einen Symlink nicht
    sehen: ein eingecheckter Link `public-config -> .env` bleibt `public-config`
    und sieht harmlos aus. Docker folgt ihm beim Mounten trotzdem. Zeigt der Link
    aus dem Repo heraus, kommt `..` zurueck - das faellt ohnehin durch.
    """
    pfad = REPO_ROOT / quelle
    if not pfad.exists():
        return None
    try:
        echt = pfad.resolve()
        return echt.relative_to(REPO_ROOT.resolve()).as_posix() or "."
    except (OSError, ValueError):
        return ".."


def _versteckte_env(quelle: str) -> list[str]:
    """Jede `.env`, die in einem gemounteten Verzeichnis tatsaechlich liegt.

    Der Pfadvergleich allein reicht hier nicht: `.gitignore` faengt `.env` in jeder
    Tiefe ab, eine solche Datei existiert also lokal, ohne je in Git oder CI
    aufzutauchen. Ein Verzeichnis-Mount traegt sie trotzdem in den Container.
    Diese Pruefung sieht deshalb als einzige aufs Dateisystem - sie schlaegt genau
    dort an, wo ein Geheimnis wirklich liegt, und bleibt sonst still.
    """
    ordner = REPO_ROOT / quelle
    if not ordner.is_dir():
        return []
    return sorted(p.relative_to(REPO_ROOT).as_posix() for p in ordner.rglob(".env"))


def test_geheimnisse_bleiben_draussen():
    """CLAUDE.md §8: .env und die Worktrees darunter gehoeren in keinen Container.

    Ueber alle Dienste, nicht nur api: db, dispatcher und n8n laufen auf demselben
    Host, und ein Mount ist dort genauso ein Leck. Ueber Binds und ueber
    `secrets:`/`configs:`, denn beide Wege reichen eine Host-Datei herein. Und ueber alle Binds, nicht nur
    die aus `_api_mounts()`: was an einem anderen Ziel als `/app/<pfad>` haengt,
    faellt fuer die Abdeckung zurecht heraus, traegt ein Geheimnis aber genauso
    herein - `./data:/tmp/data` bringt `data/.env` mit.
    """
    verboten: list[str] = []
    for dienst, definition in sorted(_dienste().items()):
        # extends holt Felder aus einer anderen Datei, die Compose erst beim Start
        # zusammenfuehrt. Geerbte Mounts stehen nicht im rohen YAML, der Waechter
        # waere hier blind - wie bei einer nicht aufloesbaren Variablen gilt
        # deshalb: nicht pruefbar heisst nicht erlaubt.
        if "extends" in definition:
            verboten.append(
                f"{dienst}: erbt per extends, geerbte Mounts sind hier unsichtbar"
            )
            continue
        for quelle in sorted(_bind_quellen(dienst) | _dateiquellen(dienst)):
            if _traegt_geheimnisse(quelle):
                verboten.append(f"{dienst}: {quelle}")
                continue
            echt = _aufgeloest(quelle)
            if echt and echt != quelle and _traegt_geheimnisse(echt):
                verboten.append(f"{dienst}: {quelle} zeigt auf {echt}")
                continue
            verboten += [
                f"{dienst}: {quelle} enthaelt {datei}"
                for datei in _versteckte_env(quelle)
            ]
    assert not verboten, f"Mount traegt fremde Geheimnisse in den Container: {verboten}"
