"""evals/heard.py: the verdict on what a speech-to-text engine heard.

The caller sentences are German because the number rule only reads German.
"""

import pytest

from evals.heard import dish, dish_verdict, judge, number_verdict


@pytest.mark.parametrize(
    ("expect", "heard", "verdict", "got"),
    [
        # Heard as said, in the engine's own spelling.
        ("23", "Nummer 23.", "right", "23"),
        ("23", "Nummer dreiundzwanzig", "right", "23"),
        # A number the guest did not say, taken without a question.
        ("23", "Nummer 24", "wrong", "24"),
        # A sentence that was a question back becomes one clear number.
        ("?", "die 24", "wrong", "24"),
        # Not understood: the name search or a question back, never a dish.
        ("23", "Vielen Dank.", "asked", "name"),
        ("23", "", "asked", "name"),
        ("23", "die 23 und die 24", "asked", "?"),
        # A number that does not exist is said out loud, not taken.
        ("23", "Nummer 23h", "asked", "!23h"),
        # "S12" heard as "ist zwölf": two dishes of the menu, so a question back.
        ("s12", "Zweimal ist zwölf.", "asked", "12|s12?"),
        # "S1" heard as "ist eins": the menu has only the S1, and the search asks
        # whether it is meant (Codex PR #257). A question, no dish taken.
        ("s1", "Ist eins.", "asked", "1|s1?"),
        # "S13" heard as "ist dreizehn": the menu has only the 13, and the search
        # offers it instead of taking it for the S13 that was said.
        ("s13", "Ist dreizehn.", "asked", "13|s13?"),
        # Neither on the menu: no such number, and by its form never taken.
        ("s99", "Ist neunundneunzig.", "asked", "99|s99?"),
    ],
)
def test_number_verdict(expect, heard, verdict, got):
    assert number_verdict(expect, heard) == (verdict, got)


@pytest.mark.parametrize(
    ("name", "heard", "verdict"),
    [
        ("Wan-Tan frittiert", "Ich hätte gern Wantan frittiert.", "right"),
        ("Pho Bo", "ich hätte gern pho bo", "right"),
        ("Gyoza", "Ich hätte gern Gürzer.", "asked"),
        ("Gyoza", "", "asked"),
    ],
)
def test_dish_verdict(name, heard, verdict):
    assert dish_verdict(name, heard) == verdict


def test_a_result_that_names_two_dishes_of_the_menu_is_a_question_not_a_wrong_dish():
    # The eval menu has S1 and SM1: the search asks which one.
    assert number_verdict("23", "Sushi eins") == ("asked", "s1|sm1")
    assert dish("s1|sm1") is None
    # Neither S2 nor SM2 exists: taken by its form, the cautious count.
    assert number_verdict("23", "Sushi zwei") == ("wrong", "s2|sm2")


def test_one_dish_reached_by_two_results_is_right():
    # "Sushi zwoelf" can be S12 or SM12, and the eval menu has only S12.
    assert number_verdict("s12|sm12", "S12") == ("right", "s12")


@pytest.mark.parametrize(
    ("result", "found"),
    [
        ("24", "24"),
        ("s12", "s12"),
        ("s12|sm12", "s12"),
        # A form the rule takes, and no such dish: the search says so.
        ("23d", None),
        ("?", None),
        ("name", None),
        ("!23h", None),
    ],
)
def test_the_dish_of_the_eval_menu_a_result_leads_to(result, found):
    assert dish(result) == found


def test_judge_marks_whether_a_wrong_number_is_a_dish_of_the_eval_menu():
    plan = {
        "sentences": [
            {"id": "n0", "kind": "number", "say": "Nummer 23", "expect": "23"}
        ]
    }
    rows = [
        {"id": "n0", "voice": "v", "ear": "e", "text": text, "seconds": 0.1}
        for text in ("Nummer 24", "Nummer 23d", "Nummer 23", "Vielen Dank.")
    ]
    judged = judge(plan, {"heard": rows})
    assert [(r["verdict"], r["on_card"]) for r in judged] == [
        ("wrong", True),
        ("wrong", False),
        ("right", False),
        ("asked", False),
    ]


def test_judge_keeps_the_row_and_adds_the_verdict():
    plan = {
        "sentences": [
            {"id": "n0", "kind": "number", "say": "Nummer 23", "expect": "23"},
            {
                "id": "d0",
                "kind": "dish",
                "say": "Ich hätte gern Pho Bo.",
                "expect": "Pho Bo",
            },
        ]
    }
    heard = {
        "heard": [
            {"id": "n0", "voice": "v", "ear": "e", "text": "Nummer 24", "seconds": 0.1},
            {
                "id": "d0",
                "voice": "v",
                "ear": "e",
                "text": "Ich hätte gern Pho Bo.",
                "seconds": 0.2,
            },
        ]
    }
    number, dish = judge(plan, heard)
    assert number["verdict"] == "wrong" and number["got"] == "24"
    assert number["say"] == "Nummer 23" and number["seconds"] == 0.1
    assert dish["verdict"] == "right" and dish["kind"] == "dish"


# --- Another menu than the eval menu (the kit's --menu) --------------------------

OWN = [
    {"number": "7", "category": "Vorspeisen"},
    {"number": "S7", "category": "Sushi"},
    {"number": "8", "category": "Vorspeisen"},
    {"number": "35b", "category": "Hauptgerichte"},
]


def own_plan(*says: str) -> dict:
    return {
        "menu": OWN,
        "sentences": [
            {"id": f"c{i}", "kind": "number", "say": say, "expect": None}
            for i, say in enumerate(says)
        ],
    }


def heard_rows(*texts: str, sentence: str = "c0") -> dict:
    return {
        "heard": [
            {"id": sentence, "voice": "v", "ear": "e", "text": text, "seconds": 0.1}
            for text in texts
        ]
    }


def test_with_a_menu_in_the_plan_the_expectation_is_what_was_said_on_that_menu():
    judged = judge(own_plan("Nummer S7"), heard_rows("Nummer S 7", "Nummer es sieben."))
    assert [(r["expect"], r["got"], r["verdict"]) for r in judged] == [
        ("s7", "s7", "right"),
        ("s7", "s7", "right"),
    ]


def test_on_card_is_counted_against_the_menu_of_the_plan_not_the_eval_menu():
    """The eval menu has no 7 and no 8, this one has: a 7 heard as 8 is another
    dish read back, and the 35b heard as 35 names no dish of this menu."""
    judged = judge(own_plan("Nummer 7"), heard_rows("Nummer 8", "Nummer 9"))
    assert [(r["verdict"], r["on_card"]) for r in judged] == [
        ("wrong", True),
        ("wrong", False),
    ]
    judged = judge(own_plan("Nummer 35b"), heard_rows("Nummer 35", "Nummer 35 B"))
    assert [(r["verdict"], r["on_card"]) for r in judged] == [
        ("wrong", False),
        ("right", False),
    ]


def test_the_heard_letter_follows_the_prefixes_of_the_menu_in_the_plan():
    """This menu has 7 and S7: "ist sieben" is a question. On a menu without a
    prefix the same transcript is the plain 7."""
    judged = judge(own_plan("Nummer S7"), heard_rows("Nummer ist sieben"))
    assert [(r["got"], r["verdict"]) for r in judged] == [("7|s7?", "asked")]
    plain = {**own_plan("Nummer 7"), "menu": [OWN[0]]}
    judged = judge(plain, heard_rows("Nummer ist sieben"))
    assert [(r["got"], r["verdict"]) for r in judged] == [("7", "right")]


def test_a_dish_sentence_keeps_its_expectation_with_a_menu_in_the_plan():
    plan = own_plan()
    plan["sentences"] = [
        {
            "id": "d0",
            "kind": "dish",
            "say": "Ich hätte gern Aal Nigiri.",
            "expect": "Aal Nigiri",
        }
    ]
    judged = judge(plan, heard_rows("ich hätte gern aal nigiri", "Aale", sentence="d0"))
    assert [r["verdict"] for r in judged] == ["right", "asked"]
