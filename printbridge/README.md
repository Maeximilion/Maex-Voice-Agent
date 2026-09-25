# Druckbrücke

Holt Küchenbons vom Server und druckt sie auf dem Bondrucker im Lokal (T-4.6).

Der Server steht im Rechenzentrum und erreicht den Drucker im Lokal nicht. Die Brücke läuft auf einem Rechner im Lokal, fragt alle zwei Sekunden nach neuen Bons, druckt sie und meldet zurück. Sie ruft nur hinaus: im Router muss kein Port offen sein.

Was passiert, wenn etwas ausfällt:

| Ausfall | Folge |
|---|---|
| Drucker aus, Papier leer, Deckel offen | Brücke meldet den Fehler, die Karte im Tablet wird sofort rot, "Nochmal senden" |
| Brücke oder Internet im Lokal weg | nach 60 s ohne Abholung wird die Karte rot (Wächter auf dem Server), Alarm |
| Rückmeldung geht verloren | der Bon kommt nach 60 s wieder, die Brücke erkennt ihn und druckt keinen zweiten Zettel |
| Team korrigiert, bevor der erste Bon gedruckt ist | nur der neue Stand wird gedruckt, nie ein alter nach einem neuen |

## Voraussetzungen

- Python 3.12 auf dem Rechner, der die Brücke ausführt
- Netzwerkdrucker (Epson mit Netzwerkkarte): keine weiteren Pakete
- Drucker per USB am Windows-Rechner: `pip install pywin32`

Welcher Anschluss? Unter Windows: Drucker und Scanner, Rechtsklick auf den Drucker, Druckereigenschaften, Reiter Anschlüsse. `USB001` heißt USB, `IP_…` oder `EpsonNet…` heißt Netzwerk.

## Einstellungen

Als Umgebungsvariablen:

| Name | Beispiel | Bedeutung |
|---|---|---|
| `MAEX_SERVER_URL` | `https://agent.example.com` | Server, nur https (außer localhost) |
| `MAEX_KITCHEN_TOKEN` | lang und zufällig | gleicher Wert wie `KITCHEN_BRIDGE_TOKEN` auf dem Server |
| `MAEX_TENANT_ID` | UUID | Betrieb, dessen Bons gedruckt werden |
| `MAEX_PRINTER` | `tcp:192.168.1.50` oder `windows:EPSON TM-T20II Küche` | Drucker |
| `MAEX_PRINTER_WIDTH` | `48` | Zeichen je Zeile (80-mm-Papier, Schrift A) |
| `MAEX_STATE_FILE` | `C:\maex\printbridge_state.json` | Druckprotokoll, nur Bestell-ids |
| `MAEX_INTERVAL_SECONDS` | `2` | Abfrageabstand |

## Inbetriebnahme

Erst ein Probebon, ohne Server und ohne echte Bestellung:

```bash
python -m printbridge --test
```

Dann ein einzelner Durchlauf und danach der Dauerbetrieb:

```bash
python -m printbridge --once
```

```bash
python -m printbridge
```

Unter Windows startet die Aufgabenplanung die Brücke bei der Anmeldung ("Beim Start", "Bei Fehler neu starten").

Der Kassenbetrieb läuft weiter: Kasse und Brücke teilen sich den Drucker, die Aufträge kommen nacheinander.
