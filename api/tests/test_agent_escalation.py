"""agent/escalation.py: Sofort-Auslöser aus docs/05 §4, geprüft vor dem Modell."""

from api.agent.escalation import check


def test_beschwerde_wird_erkannt():
    assert check("Ich möchte mich beschweren, das war eine Katastrophe") == "complaint"


def test_mensch_wunsch_wird_erkannt():
    assert check("Kann ich bitte mit einem Menschen sprechen?") == "human_requested"


def test_storno_wird_erkannt():
    assert check("Ich möchte meine Reservierung stornieren") == "cancellation"


def test_storno_als_substantiv_wird_erkannt():
    """Die kanonische Kundenformulierung aus docs/05 §4 ("Storno meiner
    Reservierung") traf bisher kein Stichwort, nur die Verbformen (Codex-Review
    PR #102, P2)."""
    assert check("Storno meiner Reservierung, bitte") == "cancellation"


def test_unauffaelliger_text_loest_nichts_aus():
    assert check("Ich hätte gerne einen Tisch für vier Personen") is None


def test_erkennung_ist_gross_klein_unabhaengig():
    assert check("BESCHWERDE!") == "complaint"


def test_leerer_text_loest_nichts_aus():
    assert check("") is None
