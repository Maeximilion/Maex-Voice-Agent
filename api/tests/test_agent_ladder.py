"""agent/ladder.py: Verständnis-Leiter als Zustandsmaschine (docs/05 §2)."""

from api.agent.ladder import UnderstandingLadder


def test_frisches_feld_startet_auf_stufe_1():
    ladder = UnderstandingLadder()
    assert ladder.level_for("party_size") == 1
    assert ladder.should_end_call("party_size") is False


def test_zwei_fehlversuche_gehen_eine_stufe_tiefer():
    ladder = UnderstandingLadder()
    ladder.record_failure("party_size")
    assert ladder.level_for("party_size") == 1  # erster Fehlversuch ändert noch nichts

    level = ladder.record_failure("party_size")
    assert level == 2
    assert ladder.level_for("party_size") == 2


def test_drei_stufenwechsel_ohne_erfolg_beenden_den_anruf():
    ladder = UnderstandingLadder()
    for _ in range(6):  # 2 Fehlversuche je Stufe * 3 Stufenwechsel
        ladder.record_failure("reserved_for")

    assert ladder.should_end_call("reserved_for") is True


def test_zwei_stufenwechsel_beenden_den_anruf_noch_nicht():
    ladder = UnderstandingLadder()
    for _ in range(4):
        ladder.record_failure("reserved_for")

    assert ladder.should_end_call("reserved_for") is False
    assert ladder.level_for("reserved_for") == 3


def test_erfolg_setzt_das_feld_komplett_zurueck():
    ladder = UnderstandingLadder()
    ladder.record_failure("guest_name")
    ladder.record_failure("guest_name")
    assert ladder.level_for("guest_name") == 2

    ladder.record_success("guest_name")

    assert ladder.level_for("guest_name") == 1
    assert ladder.should_end_call("guest_name") is False


def test_felder_sind_unabhaengig_voneinander():
    ladder = UnderstandingLadder()
    ladder.record_failure("party_size")
    ladder.record_failure("party_size")

    assert ladder.level_for("party_size") == 2
    assert ladder.level_for("guest_name") == 1


def test_stufe_steigt_nicht_ueber_das_maximum():
    ladder = UnderstandingLadder()
    for _ in range(40):
        ladder.record_failure("reserved_for")

    assert ladder.level_for("reserved_for") == 7


def test_active_levels_zeigt_nur_erhoehte_felder():
    ladder = UnderstandingLadder()
    ladder.record_failure("guest_name")  # ein Fehlversuch, noch Stufe 1
    ladder.record_failure("party_size")
    ladder.record_failure("party_size")  # zwei Fehlversuche, jetzt Stufe 2

    assert ladder.active_levels() == {"party_size": 2}
