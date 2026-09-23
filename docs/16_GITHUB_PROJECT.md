# 16 – GitHub-Project

> Das Board auf GitHub ist eine **Ableitung**, keine Handarbeit. Es zeigt den Stand, den Issues und Pull Requests ohnehin schon haben.
> Version 1.0 · 22.09.2026

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

**Das Project ist Nummer 2** (`https://github.com/users/Maeximilion/projects/2`): 121 Eintraege, 77 Issues und 44 Pull Requests, alle Felder gepflegt. Stand 23.09.2026 bestehen daneben Nummer 3 (77 Issues, vollstaendige Teilmenge von 2, kein einziger eigener Eintrag) und Nummer 4 (leer). Beide gehoeren geschlossen; solange sie offen sind, ist die Regel aus Abschnitt 1 nur aufgeschrieben, nicht hergestellt.

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

`.github/workflows/project-maintenance.yml` laeuft einmal taeglich um 03:00 UTC und ruft `scripts/project_report.py` auf. Das Skript liest, prueft und schreibt einen Bericht in die Job-Zusammenfassung. Es aendert von sich aus nichts.

Geprueft wird:

- derselbe Vorgang zweimal auf dem Board
- Eintraege ohne Status
- Eintraege in Arbeit ohne Iteration (bewusst nicht der ganze Backlog, sonst meldet der Bericht taeglich alles)
- `In Progress` seit sieben Tagen ohne Bewegung — wird gemeldet, nicht verschoben
- `Done` aelter als vierzehn Tage — Archiv-Kandidat
- abgeschlossen, Board steht aber nicht auf `Done` - zaehlt `MERGED` mit, denn ein Pull Request erreicht `CLOSED` nie

Archiviert wird nur, wenn der Lauf von Hand mit dem Schalter `apply` gestartet wird. Der taegliche Lauf berichtet ausschliesslich.

### Einrichtung

Der `GITHUB_TOKEN` einer Action darf kontoeigene Projects nicht lesen. Es braucht einmalig:

1. Fine-grained PAT anlegen, Berechtigung **Projects: Read and write**, Laufzeit notieren
2. Im Repo als Secret `PROJECT_TOKEN` hinterlegen
3. Im Repo als Variable `PROJECT_NUMBER` die Nummer aus der Project-URL hinterlegen

Lokal derselbe Lauf:

```bash
PROJECT_TOKEN=<pat> PROJECT_OWNER=<konto> PROJECT_NUMBER=<nummer> python scripts/project_report.py
```

---

## 6. Was Claude Code darf

| | Erlaubt |
|---|---|
| Waehrend `/task`, `/done`, `/bug` | Nichts. Kein `gh project`, weder lesend noch schreibend |
| Beim Anlegen eines Issues | Labels setzen: Art, Stufe, Prioritaet |
| Beim Oeffnen eines Pull Requests | `Closes #N` in die Beschreibung, Branchname mit Issue-Nummer |
| Auf `/project` oder in `/gate` | Board lesen, Bericht erzeugen, nach Rueckfrage aendern |

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
