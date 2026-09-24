# 16 – GitHub-Project

> Das Board auf GitHub ist eine **Ableitung**, keine Handarbeit. Es zeigt den Stand, den Issues und Pull Requests ohnehin schon haben.
> Version 1.1 · 24.09.2026

---

## 1. Die Regel in einem Satz

**Waehrend der Arbeit fasst niemand das Board an.** Der Status bewegt sich durch Ereignisse, die GitHub selbst ausloest; alles andere faellt einmal taeglich in einem Bericht auf.

Warum das so ist: Ein Board, das mitten in einer Aufgabe von Hand oder vom Agenten beschrieben wird, sammelt Dopplungen und Rauschen. Jede Aenderung kostet Kontext und bringt keinen Meter Code voran. Und ein Eintrag, der auf `Done` steht, bevor etwas gemerged wurde, sagt schlicht die Unwahrheit.

---

## 2. Ein Project, kein zweites

**Es gibt genau ein Project.** Mehrere Boards fuer ein Repo und eine Person erzeugen nur Dopplungen: derselbe Vorgang steht an zwei Stellen und beide gehen auseinander.

Was wie ein zweites Project aussieht, ist immer eine **View**. Views kosten nichts, teilen dieselben Daten und lassen sich nicht widersprechen:

| View | Typ | Zeigt |
|---|---|---|
| Board | Board nach Status | Der Arbeitsfluss: Todo, In progress, Done |
| Aktuelle Iteration | Tabelle | Was gerade dran ist |
| Backlog | Tabelle, nach Prioritaet | Offen, noch keiner Iteration zugeordnet |
| Roadmap | Roadmap | Die Stufen ueber die Zeit. Monat oder Quartal ist eine Einstellung dieser View, kein eigenes Project |
| In Review | Tabelle | Pull Requests, die auf Review warten |

Wenn eine Sicht fehlt: eine View anlegen, niemals ein Project.

**Das Project ist Nummer 2, `Maex Voice-Agent`** (`https://github.com/users/Maeximilion/projects/2`). Am 24.09.2026 zusammengefuehrt: Nummer 3 war eine vollstaendige Teilmenge von 2 (77 Issues, kein einziger eigener Eintrag), Nummer 1 und 4 waren leer. Alle drei sind geschlossen, nicht geloescht - sie lassen sich wieder oeffnen, falls je etwas fehlt.

---

## 3. Felder

| Feld | Typ | Woher der Wert kommt |
|---|---|---|
| Status | Single Select | Eingebaute Workflows (Abschnitt 4). Nie von Hand |
| Stufe | Single Select `stufe-0` bis `stufe-5` | Label des Issues, beim Anlegen gesetzt |
| Art | Single Select `feature`, `chore`, `docs`, `test`, `refactor` | Label des Issues, genau eines |
| Prioritaet | Single Select `hoch`, `mittel`, `niedrig` | Label `priority: *` |
| Iteration | Iteration | Bei der Planung gesetzt, nicht waehrend der Arbeit |

**Status steht nur im Feld, nie zusaetzlich als Label.** Zwei Orte fuer dieselbe Wahrheit gehen immer auseinander. Die Labels `status: in-progress` und `status: needs-review` sind deshalb abgeschafft.

Einzige Ausnahme: `status: blocked`. `docs/07_WORKPACKAGES.md` fuehrt "blockiert" als Aufgabenstatus, und diese Datei ist die Quelle der Wahrheit. Das Label spiegelt sie, damit ein blockiertes Arbeitspaket auch ohne Board sichtbar ist.

---

## 4. Wie der Status sich bewegt

Einmalig in den Project-Einstellungen unter "Workflows" eingeschaltet, danach ohne Zutun:

| Ereignis | Folge |
|---|---|
| Issue oder Pull Request geoeffnet | Eintrag wird aufgenommen, Status `Todo` |
| Pull Request gemerged | Status `Done` |
| Issue geschlossen | Status `Done` |
| Eintrag wieder geoeffnet | Status `In Progress` |

Damit der Merge das verknuepfte Issue mitnimmt, traegt **jede** Pull-Request-Beschreibung ein Schluesselwort: `Closes #12`. Ohne das schliesst der Merge nur den Pull Request und das Issue bleibt offen stehen.

`Done` entsteht ausschliesslich hier. Nicht vorher, nicht von Hand, nicht durch Claude Code.

---

## 5. Die taegliche Pflege

`.github/workflows/project-maintenance.yml` laeuft einmal taeglich um 03:00 UTC und ruft `scripts/project_report.py --fix` auf. Der Lauf korrigiert genau eine Sorte Fehler selbst und meldet alles andere in der Job-Zusammenfassung.

**Korrigiert wird:** was GitHub abgeschlossen hat (Issue `CLOSED`, Pull Request `MERGED`), das Board aber nicht auf `Done` fuehrt. Das ist die Luecke, die die eingebauten Workflows hinterlassen, denn sie greifen nur bei neuen Ereignissen und nie rueckwirkend. Die Korrektur ist idempotent und zieht das Board nur zur Wahrheit hin, nie davon weg.

**Nur gemeldet wird** alles, was eine Entscheidung braucht:

- offene Issues und Pull Requests des Repos, die auf dem Board fehlen - falls die automatische Aufnahme etwas verpasst hat. Verglichen wird die URL, nicht die Nummer
- offen, steht aber auf `Done` - ein wieder geoeffneter Eintrag, den der Workflow nicht zurueckgesetzt hat
- derselbe Vorgang zweimal auf dem Board
- Eintraege ohne Status
- Eintraege in Arbeit ohne Iteration (bewusst nicht der ganze Backlog, sonst meldet der Bericht taeglich alles)
- `In Progress` seit sieben Tagen ohne Bewegung — wird gemeldet, nicht verschoben

**Archiviert wird nie automatisch.** Fuer ein Portfolio ist sichtbare, erledigte Arbeit in der Roadmap ein Wert und kein Ballast. Wer aufraeumen will, startet den Lauf von Hand mit dem Schalter `archive`; dann verschwinden `Done`-Eintraege, die seit vierzehn Tagen unbewegt sind - aber nur abgeschlossene. Ein offener Eintrag auf `Done` wird nie archiviert, und was derselbe Lauf eben erst auf `Done` gesetzt hat, gilt als frisch bewegt.

### Einrichtung

Der `GITHUB_TOKEN` einer Action darf kontoeigene Projects nicht lesen. Es braucht einmalig:

1. Token anlegen, entweder
   - klassisch unter `https://github.com/settings/tokens/new`, nur Scope **`project`** (einfachster Weg), oder
   - fine-grained, Resource owner = eigenes Konto, unter **Account permissions** (nicht Repository permissions) **Projects: Read and write**
2. Im Repo als Secret `PROJECT_TOKEN` hinterlegen
3. Im Repo als Variable `PROJECT_NUMBER` die Nummer aus der Project-URL hinterlegen

Beides ist seit 24.09.2026 eingerichtet (`PROJECT_NUMBER` = 2). Das Repo fuer den Abgleich offener Eintraege liest der Lauf in der Action aus `GITHUB_REPOSITORY`; lokal kommt es aus `PROJECT_REPO`. Fehlt beides, sagt der Bericht ausdruecklich, dass er nicht nach fehlenden Eintraegen gesucht hat. Laeuft der Token ab, faellt der taegliche Lauf rot aus - das ist gewollt, ein stilles Aussetzen waere schlimmer.

Lokal derselbe Lauf:

```bash
PROJECT_TOKEN=$(gh auth token) PROJECT_OWNER=Maeximilion PROJECT_NUMBER=2 PROJECT_REPO=Maeximilion/Maex-Voice-Agent python scripts/project_report.py
```

---

## 6. Was Claude Code darf

| | Erlaubt |
|---|---|
| Waehrend `/task`, `/done`, `/bug` | Nichts. Kein `gh project`, weder lesend noch schreibend |
| Beim Anlegen eines Issues | Labels setzen: Art, Stufe, Prioritaet |
| Beim Oeffnen eines Pull Requests | `Closes #N` in die Beschreibung, Branchname mit Issue-Nummer |
| Auf `/project` oder in `/gate` | Board lesen und pflegen. Mechanische Korrekturen (Status nach Merge, Dopplungen, fehlende Eintraege) direkt, Ermessensfragen (Iteration, Archiv, neue Views) als Vorschlag |

Grundlage ist Maxis stehende Freigabe vom 24.09.2026: Claude raeumt das Board selbst auf und haelt es aktuell - aber nur ausserhalb der Arbeit an einer Aufgabe. Loeschen von Projects, Eintraegen oder Feldern bleibt ausgenommen.

Die Verbindung zwischen Arbeit und Board entsteht ueber Schluesselwoerter und Branchnamen, nicht ueber API-Aufrufe. Branchname traegt die Nummer: `task/T-4.3-search-menu` oder `fix/12-kurzbeschreibung`.

---

## 7. Verwandte Dateien

| Datei | Bezug |
|---|---|
| `docs/07_WORKPACKAGES.md` | Quelle der Wahrheit fuer Aufgaben und Abhaengigkeiten. Das Board spiegelt sie |
| `docs/01_STATUS.md` | Wo das Projekt steht. Nicht das Board |
| `scripts/project_report.py` | Die taegliche Pruefung |
| `.github/workflows/project-maintenance.yml` | Der taegliche Lauf |
| `.claude/skills/github-project/SKILL.md` | Dieselbe Regel fuer Claude Code |
