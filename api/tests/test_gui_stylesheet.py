"""Rules of docs/06 section 1 that the operations stylesheet itself must keep.

Found by the UI review of 15.11.2026: the two buttons of a red bar and a compact
card were 44 px high with 16 px text from a token that does not exist, card edges
were barely visible, and quick taps on the steppers could zoom the page.
"""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "gui" / "static"
CSS = (STATIC / "app.css").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    """Body of the first rule whose selector list is exactly ``selector``."""
    return CSS.split(selector + "{", 1)[1].split("}", 1)[0]


def _root() -> dict[str, str]:
    return dict(re.findall(r"--([\w-]+):\s*([^;]+);", _rule(":root")))


def _contrast(a: str, b: str) -> float:
    def lum(hex_colour: str) -> float:
        c = [int(hex_colour.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        lin = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    high, low = sorted((lum(a), lum(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_every_token_the_stylesheet_uses_is_defined():
    """A missing token falls back silently: --fs-base gave 16 px text."""
    used = set(re.findall(r"var\(--([\w-]+)", CSS))

    assert used - set(_root()) == set()


@pytest.mark.parametrize(
    "selector",
    [".red-bar button,.order.compact button", ".red-bar b", ".order.compact .time"],
)
def test_red_bar_and_compact_card_keep_tablet_text(selector: str):
    assert "font-size:var(--fs-body)" in _rule(selector)


def test_red_bar_and_compact_card_buttons_are_full_targets():
    """The most urgent tap on the board ("Nochmal senden"): 60 px like the rest."""
    rule = _rule(".red-bar button,.order.compact button")

    assert "min-height:var(--tap)" in rule
    assert not re.search(r"min-height:\s*\d+px", rule)


def test_compact_card_wraps_its_button_instead_of_overflowing():
    """At 1024 px "In Kasse eingegeben" ran out of the card and the column scrolled
    sideways; the name gives way first (zero basis), then the button wraps."""
    assert "flex-wrap:wrap" in _rule(".order.compact summary")
    assert "flex:1 1 0" in _rule(".order.compact summary .name")


def test_card_edges_stand_out_from_card_and_page():
    """Edges and dividers at least 2:1, and still quieter than a button border."""
    root = _root()
    line, strong = root["line"], root["line-strong"]

    assert _contrast(line, root["surface"]) >= 2.0
    assert _contrast(line, root["bg"]) >= 2.0
    assert _contrast(line, root["surface"]) < _contrast(strong, root["surface"])


def test_tap_targets_do_not_wait_for_a_double_tap_zoom():
    """Quick taps on minus and plus must not zoom; the zero specificity of :where
    lets the floor plan keep touch-action:none on what it drags."""
    rule = _rule(":where(.betrieb) :where(button,a.button,summary,input,select)")

    assert "touch-action:manipulation" in rule
    floor_plan = (STATIC / "raumplan.css").read_text(encoding="utf-8")
    assert floor_plan.count("touch-action: none") == 3


def test_every_form_control_shows_the_same_focus_ring():
    """#829-17: a focused select or textarea kept the browser's thin ring."""
    rule = re.search(
        r"\n([^{}\n]*:focus-visible[^{}]*)\{([^}]*outline:3px[^}]*)\}", CSS
    )
    assert rule is not None
    selectors, body = rule.groups()
    listed = {s.strip().removesuffix(":focus-visible") for s in selectors.split(",")}
    assert {"button", "a", "input", "select", "textarea", "summary"} <= listed
    assert "var(--accent)" in body
