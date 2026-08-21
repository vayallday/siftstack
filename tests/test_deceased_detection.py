"""Tests for notice_parser.detect_deceased_indicator().

The helper was promoted out of the archived TN tax enricher into
notice_parser.py when that archive was removed — the logic is pure
owner-name regex and applies to any source (PropertyRadar owner-of-record
names carry the same LIFE EST / PERSONAL REP / ET AL markers).
"""

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, _SRC)

from notice_parser import detect_deceased_indicator


def test_personal_rep():
    assert detect_deceased_indicator("SMITH JOHN PERSONAL REPRESENTATIVE") == "personal_rep"
    assert detect_deceased_indicator("DOE JANE PERSONAL REP") == "personal_rep"
    assert detect_deceased_indicator("Personal Representative Of The Estate") == "personal_rep"


def test_life_estate():
    assert detect_deceased_indicator("JONES ROBERT (LIFE EST)") == "life_estate"
    assert detect_deceased_indicator("WILLIAMS MARY LIFE EST") == "life_estate"
    assert detect_deceased_indicator("Smith John Life Estate") == "life_estate"


def test_care_of():
    assert detect_deceased_indicator("JONES ROBERT % SMITH JANE") == "care_of"
    assert detect_deceased_indicator("DOE JOHN %DOE JANE") == "care_of"


def test_et_al():
    assert detect_deceased_indicator("SMITH JOHN ET AL") == "et_al"
    assert detect_deceased_indicator("DOE JANE ET  AL") == "et_al"


def test_trustee():
    assert detect_deceased_indicator("SMITH JOHN TRUSTEE") == "trustee"
    assert detect_deceased_indicator("DOE JANE TRUSTEE OF THE DOE TRUST") == "trustee"


def test_trustee_business_false_positive():
    """Business entities with 'trustee' should NOT be flagged."""
    assert detect_deceased_indicator("FIRST TENNESSEE BANK TRUSTEE") == ""
    assert detect_deceased_indicator("ABC HOLDINGS LLC TRUSTEE") == ""
    assert detect_deceased_indicator("REAL ESTATE DEVELOPMENT CORP TRUSTEE") == ""
    assert detect_deceased_indicator("EASTSIDE REAL ESTATE AND DEVELOPMENT GROUP LLC") == ""


def test_no_indicator():
    assert detect_deceased_indicator("SMITH JOHN") == ""
    assert detect_deceased_indicator("DOE JANE AND DOE JOHN") == ""
    assert detect_deceased_indicator("NORMAL OWNER NAME") == ""


def test_empty_input():
    assert detect_deceased_indicator("") == ""
    assert detect_deceased_indicator("   ") == ""
    assert detect_deceased_indicator(None) == ""


def test_priority_order():
    """When multiple indicators present, highest priority wins."""
    # personal_rep beats life_estate
    assert detect_deceased_indicator("SMITH LIFE EST PERSONAL REP") == "personal_rep"
    # life_estate beats care_of
    assert detect_deceased_indicator("JONES LIFE EST % SMITH") == "life_estate"
    # care_of beats et_al
    assert detect_deceased_indicator("DOE % SMITH ET AL") == "care_of"


def test_real_data_examples():
    """Real owner names from Knox County tax API."""
    assert detect_deceased_indicator("HALL BRENDA J (LIFE EST)") == "life_estate"
    assert detect_deceased_indicator("YOUNG CHARLES E SR (LIFE EST)") == "life_estate"
    assert detect_deceased_indicator("BLALOCK GARY W % BLALOCK MISTY D") == "care_of"
    assert detect_deceased_indicator("MCGHEE JAMES M ET AL") == "et_al"
    assert detect_deceased_indicator("EASTSIDE REAL ESTATE AND DEVELOPMENT GROUP LLC") == ""


if __name__ == "__main__":
    passed = 0
    failed = 0
    for name, func in list(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                passed += 1
                print(f"  PASS  {name}")
            except AssertionError as e:
                failed += 1
                print(f"  FAIL  {name}: {e}")
            except Exception as e:
                failed += 1
                print(f"  ERROR {name}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
