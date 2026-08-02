"""Patch mechanics: parsing model replies, splicing symbols, rendering diffs."""

import pytest

from bugfixer.domain.patching import (
    build_patch,
    extract_code,
    parse_proposal,
    parse_review,
    render_diff,
    replace_symbol,
)

FILE = "def double(n):\n    return n + 2\n"


# --- parsing --------------------------------------------------------------


def test_extract_code_takes_the_last_fenced_block():
    """Models often show the original before the correction."""
    text = "Here is the current code:\n```python\nold()\n```\nFixed:\n```python\nnew()\n```"

    assert extract_code(text) == "new()"


def test_extract_code_tolerates_an_unfenced_reply():
    assert extract_code("def f():\n    return 1") == "def f():\n    return 1"


def test_parse_proposal_reads_the_structured_fields():
    reply = (
        "FILE: pricing.py\n"
        "SYMBOL: apply_discount\n"
        "RATIONALE: percent was subtracted as an absolute amount\n"
        "```python\n"
        "def apply_discount(total, percent):\n"
        "    return total * (1 - percent / 100)\n"
        "```\n"
    )

    proposal = parse_proposal(reply)

    assert proposal.path == "pricing.py"
    assert proposal.symbol == "apply_discount"
    assert "percent" in proposal.rationale
    assert proposal.new_source.startswith("def apply_discount")


@pytest.mark.parametrize(
    "reply",
    [
        "```python\ndef f():\n    pass\n```",  # no FILE/SYMBOL
        "FILE: a.py\nSYMBOL: f\n```python\n```",  # empty body
        "I could not determine a fix.",  # prose only
    ],
)
def test_parse_proposal_rejects_unusable_replies(reply):
    assert parse_proposal(reply) is None


def test_parse_review_reads_confidence_and_verdict():
    review = parse_review("CONFIDENCE: 0.82\nVERDICT: apply\nREASON: matches the assertion")

    assert review.confidence == pytest.approx(0.82)
    assert review.apply is True
    assert "assertion" in review.reason


def test_parse_review_defaults_to_rejecting_an_unclear_reply():
    """An unparseable verdict must never read as approval."""
    review = parse_review("I think this looks fine?")

    assert review.apply is False
    assert review.confidence == 0.0


def test_parse_review_clamps_out_of_range_confidence():
    assert parse_review("CONFIDENCE: 7\nVERDICT: apply").confidence == 1.0


# --- splicing -------------------------------------------------------------


def test_replace_symbol_swaps_only_the_named_lines():
    source = "HEADER = 1\n\n\ndef double(n):\n    return n + 2\n\n\ndef other():\n    return 0\n"

    patched = replace_symbol(source, 4, 5, "def double(n):\n    return n * 2\n")

    assert "HEADER = 1" in patched
    assert "def other():" in patched
    assert "return n * 2" in patched
    assert "return n + 2" not in patched


def test_replace_symbol_reindents_a_method_returned_at_column_zero():
    """A model usually returns the method unindented; it must land in the class."""
    source = "class Service:\n    def run(self):\n        return 1\n"

    patched = replace_symbol(source, 2, 3, "def run(self):\n    return 2")

    assert patched == "class Service:\n    def run(self):\n        return 2\n"


def test_replace_symbol_accepts_a_correctly_indented_replacement():
    source = "class Service:\n    def run(self):\n        return 1\n"

    patched = replace_symbol(source, 2, 3, "    def run(self):\n        return 2")

    assert patched == "class Service:\n    def run(self):\n        return 2\n"


def test_replace_symbol_preserves_crlf_line_endings():
    """A patch must not rewrite every line of a CRLF file."""
    source = "def double(n):\r\n    return n + 2\r\n"

    patched = replace_symbol(source, 1, 2, "def double(n):\n    return n * 2")

    assert "\r\n" in patched
    assert "\n\n" not in patched.replace("\r\n", "")
    assert patched.startswith("def double(n):\r\n")


@pytest.mark.parametrize("start,end", [(0, 2), (1, 99), (3, 1)])
def test_replace_symbol_rejects_impossible_ranges(start, end):
    with pytest.raises(ValueError):
        replace_symbol(FILE, start, end, "def double(n):\n    return n * 2")


# --- diffs ----------------------------------------------------------------


def test_render_diff_is_git_style():
    after = "def double(n):\n    return n * 2\n"

    diff = render_diff("calc.py", FILE, after)

    assert diff.startswith("--- a/calc.py")
    assert "+++ b/calc.py" in diff
    assert "-    return n + 2" in diff
    assert "+    return n * 2" in diff


def test_build_patch_reports_when_nothing_changed():
    proposal = parse_proposal(
        "FILE: calc.py\nSYMBOL: double\n```python\ndef double(n):\n    return n + 2\n```"
    )

    patch = build_patch(proposal, FILE, 1, 2)

    assert not patch.changes_anything
    assert patch.diff == ""


def test_build_patch_produces_before_after_and_diff():
    proposal = parse_proposal(
        "FILE: calc.py\nSYMBOL: double\n```python\ndef double(n):\n    return n * 2\n```"
    )

    patch = build_patch(proposal, FILE, 1, 2)

    assert patch.changes_anything
    assert patch.before == FILE
    assert "return n * 2" in patch.after
    assert "+    return n * 2" in patch.diff
