"""Phase 3: pre_probate-specific candidate selection in obituary_enricher.

The full candidate-selection lives inside enrich_obituary_data, a 200+ line
function that early-returns when api_key is empty (so we can't run the
branch in a unit test without a live Anthropic key). The pre_probate
branch is load-bearing — it both (a) makes the routing explicit instead
of falling through to the generic owner_name path and (b) promotes
owner_deceased="yes" upfront so a failed obituary lookup still tags the
record as deceased. Source-grep guards against accidental removal during
refactor.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_obituary_enricher_has_explicit_pre_probate_candidate_branch():
    import obituary_enricher
    src = Path(obituary_enricher.__file__).read_text(encoding="utf-8")
    assert 'n.notice_type == "pre_probate" and n.owner_name.strip()' in src, (
        "obituary_enricher.enrich_obituary_data must keep its explicit "
        "pre_probate branch in candidate selection — otherwise the routing "
        "silently falls through to the generic owner_name path and is "
        "fragile to future PR schema changes (e.g., if PR ever populates "
        "tax_owner_name for pre_probate, the tax-name branch would win)."
    )


def test_obituary_enricher_promotes_pre_probate_to_deceased():
    """Pre-probate IS proof of death (PR's deceased-owner signal lives in
    the property record). The candidate-selection block must set
    owner_deceased="yes" upfront so a failed obituary search still tags
    the record as deceased — otherwise pre_probate records with rare
    names upload to DataSift as 'living' and route mail to a dead person.
    """
    import obituary_enricher
    src = Path(obituary_enricher.__file__).read_text(encoding="utf-8")
    # Expect TWO clauses: one for candidate selection (picks the name to
    # search) and one for upfront owner_deceased promotion (parallels the
    # existing probate clause).
    assert src.count('elif n.notice_type == "pre_probate" and n.owner_name.strip():') >= 2, (
        "Expected two pre_probate elif clauses in obituary_enricher: "
        "candidate selection + upfront owner_deceased promotion."
    )
