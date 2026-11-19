"""What the public pages say, as data.

Only what the product does (#892): no figure, no customer, no price, no launch
date. The example calls are invented and marked as examples on the page.
"""

from dataclasses import dataclass

# The path of the playable tablet demo once it exists (#892, its own task).
# None: the page shows no "Jetzt ausprobieren" and no demo section, so no dead
# button stands in for it. The place for the demo is marked in landing.html.
DEMO_PATH: str | None = None


@dataclass(frozen=True)
class Call:
    id: str
    label: str
    tag_a: str
    tag_b: str
    # Guest, agent, guest, agent: the third sentence is the guest's yes.
    says: tuple[str, str, str, str]


CALLS = (
    Call(
        "tisch",
        "Reservierung",
        "Tisch für 4",
        "morgen 19:00",
        (
            "Guten Abend, ich hätte gern einen Tisch für vier, morgen um sieben.",
            "Gern. Vier Personen, morgen um 19 Uhr. Soll ich das so eintragen?",
            "Ja, bitte.",
            "Eingetragen. Vielen Dank und bis morgen.",
        ),
    ),
    Call(
        "abholung",
        "Abholung",
        "Abholung",
        "2 x Nr. 25",
        (
            "Guten Tag, ich möchte etwas zum Abholen bestellen. Zweimal die 25.",
            "Zweimal Nummer 25 zur Abholung. Stimmt das so?",
            "Ja, stimmt.",
            "Ist notiert. Bis gleich.",
        ),
    ),
    Call(
        "lieferung",
        "Lieferung",
        "Lieferung",
        "1 x Nr. 13",
        (
            "Hallo, einmal die 13 in die Gartenstraße 5, bitte.",
            "Die Gartenstraße liegt in unserem Liefergebiet. Einmal Nummer 13 dorthin, richtig?",
            "Ja, genau.",
            "Ist notiert. Vielen Dank.",
        ),
    ),
)

KINDS = (
    "Restaurant",
    "Imbiss",
    "Café oder Bistro",
    "Lieferdienst",
    "Systemgastronomie",
    "Etwas anderes",
)
