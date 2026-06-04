"""Richmond OPP / EnerGov code-case enricher.

Per-address lookup against Richmond's Tyler EnerGov Self-Service API. Attaches
active code violations and permit activity to NoticeData records whose property
is in Richmond City. Direct JSON HTTP — no Playwright at runtime.

See memory: richmond-opp-energov for the discovered API contract (recon
performed 2026-05-25 via `test_richmond_opp_api.py`).

Operator instructions for OPP address search (from the email):
    "enter the address number, direction (if applicable) and name,
     EXCLUDING (address) suffixes – ST, RD, AV, WAY, etc."

So we strip the street suffix from the input address before sending it as
the API `Keyword`. The API uses fuzzy keyword matching, NOT exact-address
matching ("102 E Broad" returns BOTH "102 E Broad St" AND "102 E Broad Rock
Rd"), so we post-filter the results to confirm the response actually matches
the input house number + direction + street name.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import time
import urllib.request
from dataclasses import dataclass
from typing import Iterable
from urllib.error import HTTPError, URLError

from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# ── API constants (verified 2026-05-25) ──────────────────────────────

API_URL = (
    "https://energov.richmondgov.com/energov_prod/selfservice/api/energov/search/search"
)

TENANT_HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
    "tenantid": "1",
    "tenantname": "richmondvaprod",
    "tyler-tenanturl": "richmondvaprod",
    "tyler-tenant-culture": "en-US",
    "referer": (
        "https://energov.richmondgov.com/EnerGov_Prod/SelfService/richmondvaprod"
    ),
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
}

HTTP_TIMEOUT_SECONDS = 25
RATE_LIMIT_SECONDS = 0.4  # ~150 requests/min — well below any public-API threshold

# Recent-permit cutoff — permits older than this are not interesting as a
# distress/improvement signal.
RECENT_PERMIT_YEARS = 2


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL_CONTEXT = _ssl_context()


# ── Address handling ────────────────────────────────────────────────

# Suffixes the operator says to strip from the OPP search input.
_SUFFIX_RE = re.compile(
    r"\s+(?:"
    r"St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Ln|Lane|Blvd|Boulevard|"
    r"Way|Cir|Circle|Ct|Court|Pl|Place|Pkwy|Parkway|Ter|Terrace|"
    r"Tpke|Turnpike|Trl|Trail|Hwy|Highway|Loop|Run|Pike|Sq|Square|"
    r"Plaza|Pt|Point|Cv|Cove|Bnd|Bend|Mews|Walk|Park|Pk|Row"
    r")\.?\s*$",
    re.IGNORECASE,
)

_HEAD_RE = re.compile(
    r"^(?P<num>\d+(?:\s*\d+/\d+)?)"      # house number, allowing "1101 1/2"
    r"(?:\s+(?P<dir>[NSEW])\.?)?"          # optional direction letter
    r"\s+(?P<street>[A-Za-z][\w'.-]*)"     # first street-name word
    r"(?P<rest>.*)$"
)


def _normalize_for_opp_keyword(address: str) -> str:
    """Strip the trailing street suffix per operator instructions.

    Input  "10 E Baker St"     →  "10 E Baker"
    Input  "1102 N 25th St"    →  "1102 N 25th"
    Input  "655 New York Ave"  →  "655 New York"
    """
    return _SUFFIX_RE.sub("", address.strip()).strip()


@dataclass
class _AddressParts:
    house: str
    direction: str  # uppercase or ""
    street_first: str  # uppercase

    @classmethod
    def parse(cls, address: str) -> "_AddressParts | None":
        m = _HEAD_RE.match(address.strip())
        if not m:
            return None
        return cls(
            house=re.sub(r"\s+", " ", m.group("num").strip()),
            direction=(m.group("dir") or "").upper(),
            street_first=m.group("street").upper(),
        )


def _entity_matches_input(entity: dict, target: _AddressParts) -> bool:
    """Confirm an EntityResult actually corresponds to the input address.

    The OPP API does fuzzy keyword matching, so "102 E Broad" returns BOTH
    "102 E Broad St" and "102 E Broad Rock Rd". We accept only entities
    whose decomposed Address matches house + direction (if specified)
    + first street word from the input.
    """
    addr = entity.get("Address") or {}
    if not addr:
        return False

    a1 = (addr.get("AddressLine1") or "").strip()
    if a1 != target.house:
        return False

    a2_upper = (addr.get("AddressLine2") or "").strip().upper()
    if not a2_upper:
        return False
    # Must start with the same street name word (allow "25TH" vs "25th").
    if not a2_upper.startswith(target.street_first):
        return False

    if target.direction:
        pd = (addr.get("PreDirection") or "").strip().upper()
        if pd and pd != target.direction:
            return False

    return True


# ── Record classification ───────────────────────────────────────────

# CaseType substrings that mark a record as a code enforcement case.
_CODE_TYPE_HINTS = (
    "code",                # "Site Inspection - Code"
    "maintenance",         # "Defective Maintenance", "Property Maintenance"
    "compliance",          # "Zoning Code Compliance"
    "code violation",
)

# CaseStatus substrings that mark a code case as currently active.
_ACTIVE_STATUS_HINTS = (
    "in violation",
    "open",
    "re-inspection",
    "reinspection",
    "notice issued",
)


def _is_code_case(entity: dict) -> bool:
    ct = (entity.get("CaseType") or "").lower()
    return any(h in ct for h in _CODE_TYPE_HINTS)


def _is_active_status(entity: dict) -> bool:
    status = (entity.get("CaseStatus") or "").lower()
    return any(h in status for h in _ACTIVE_STATUS_HINTS)


def _is_recent_permit(entity: dict, cutoff_iso: str) -> bool:
    """Recent permit = NOT a code case AND has an ApplyDate within cutoff."""
    if _is_code_case(entity):
        return False
    apply_date = entity.get("ApplyDate") or ""
    return bool(apply_date and apply_date >= cutoff_iso)


# ── Richmond detection ──────────────────────────────────────────────


def _is_richmond_city(notice: NoticeData) -> bool:
    """Return True only for properties in Richmond City (not Henrico/Chesterfield)."""
    if (notice.state or "").upper() != "VA":
        return False
    county = (notice.county or "").lower()
    city = (notice.city or "").lower()

    # Hard-exclude neighboring counties even when they say "Richmond"
    if any(k in county for k in ("henrico", "chesterfield", "hanover")):
        return False

    if "richmond city" in county or county == "richmond":
        return True
    if city == "richmond" and not county:
        return True
    return False


# ── HTTP client ─────────────────────────────────────────────────────


# Captured from a live Playwright session — every nested *Criteria object is
# null/default, only Keyword + paging are populated per-call. Trimmed slightly
# for readability but preserves the API's required structure.
_BASE_PAYLOAD: dict = {
    "Keyword": "",
    "ExactMatch": True,
    "SearchModule": 1,
    "FilterModule": 1,
    "SearchMainAddress": False,
    "PlanCriteria": {
        "PlanNumber": None, "PlanTypeId": None, "PlanWorkclassId": None,
        "PlanStatusId": None, "ProjectName": None, "ApplyDateFrom": None,
        "ApplyDateTo": None, "ExpireDateFrom": None, "ExpireDateTo": None,
        "CompleteDateFrom": None, "CompleteDateTo": None, "Address": None,
        "Description": None, "SearchMainAddress": False, "ContactId": None,
        "ParcelNumber": None, "TypeId": None, "WorkClassIds": None,
        "ExcludeCases": None, "EnableDescriptionSearch": False,
        "PageNumber": 0, "PageSize": 0, "SortBy": None, "SortAscending": False,
    },
    "PermitCriteria": {
        "PermitNumber": None, "PermitTypeId": None, "PermitWorkclassId": None,
        "PermitStatusId": None, "ProjectName": None, "IssueDateFrom": None,
        "IssueDateTo": None, "Address": None, "Description": None,
        "ExpireDateFrom": None, "ExpireDateTo": None, "FinalDateFrom": None,
        "FinalDateTo": None, "ApplyDateFrom": None, "ApplyDateTo": None,
        "SearchMainAddress": False, "ContactId": None, "TypeId": None,
        "WorkClassIds": None, "ParcelNumber": None, "ExcludeCases": None,
        "EnableDescriptionSearch": False, "PageNumber": 0, "PageSize": 0,
        "SortBy": None, "SortAscending": False,
    },
    "InspectionCriteria": {
        "Keyword": None, "ExactMatch": False, "Complete": None,
        "InspectionNumber": None, "InspectionTypeId": None,
        "InspectionStatusId": None, "RequestDateFrom": None,
        "RequestDateTo": None, "ScheduleDateFrom": None, "ScheduleDateTo": None,
        "Address": None, "SearchMainAddress": False, "ContactId": None,
        "TypeId": [], "WorkClassIds": [], "ParcelNumber": None,
        "DisplayCodeInspections": False, "ExcludeCases": [],
        "ExcludeFilterModules": [], "HiddenInspectionTypeIDs": None,
        "PageNumber": 0, "PageSize": 0, "SortBy": None, "SortAscending": False,
    },
    "CodeCaseCriteria": {
        "CodeCaseNumber": None, "CodeCaseTypeId": None,
        "CodeCaseStatusId": None, "ProjectName": None, "OpenedDateFrom": None,
        "OpenedDateTo": None, "ClosedDateFrom": None, "ClosedDateTo": None,
        "Address": None, "ParcelNumber": None, "Description": None,
        "SearchMainAddress": False, "RequestId": None, "ExcludeCases": None,
        "ContactId": None, "EnableDescriptionSearch": False, "PageNumber": 0,
        "PageSize": 0, "SortBy": None, "SortAscending": False,
    },
    "RequestCriteria": {
        "RequestNumber": None, "RequestTypeId": None, "RequestStatusId": None,
        "ProjectName": None, "EnteredDateFrom": None, "EnteredDateTo": None,
        "DeadlineDateFrom": None, "DeadlineDateTo": None,
        "CompleteDateFrom": None, "CompleteDateTo": None, "Address": None,
        "ParcelNumber": None, "SearchMainAddress": False, "PageNumber": 0,
        "PageSize": 0, "SortBy": None, "SortAscending": False,
    },
    "BusinessLicenseCriteria": {
        "LicenseNumber": None, "LicenseTypeId": None, "LicenseClassId": None,
        "LicenseStatusId": None, "BusinessStatusId": None, "LicenseYear": None,
        "ApplicationDateFrom": None, "ApplicationDateTo": None,
        "IssueDateFrom": None, "IssueDateTo": None, "ExpirationDateFrom": None,
        "ExpirationDateTo": None, "SearchMainAddress": False,
        "CompanyTypeId": None, "CompanyName": None, "BusinessTypeId": None,
        "Description": None, "CompanyOpenedDateFrom": None,
        "CompanyOpenedDateTo": None, "CompanyClosedDateFrom": None,
        "CompanyClosedDateTo": None, "LastAuditDateFrom": None,
        "LastAuditDateTo": None, "ParcelNumber": None, "Address": None,
        "TaxID": None, "DBA": None, "ExcludeCases": None, "TypeId": None,
        "WorkClassIds": None, "ContactId": None, "PageNumber": 0, "PageSize": 0,
        "SortBy": None, "SortAscending": False,
    },
    "ProfessionalLicenseCriteria": {
        "LicenseNumber": None, "HolderFirstName": None, "HolderMiddleName": None,
        "HolderLastName": None, "HolderCompanyName": None, "LicenseTypeId": None,
        "LicenseClassId": None, "LicenseStatusId": None, "IssueDateFrom": None,
        "IssueDateTo": None, "ExpirationDateFrom": None,
        "ExpirationDateTo": None, "ApplicationDateFrom": None,
        "ApplicationDateTo": None, "Address": None, "MainParcel": None,
        "SearchMainAddress": False, "ExcludeCases": None, "TypeId": None,
        "WorkClassIds": None, "ContactId": None, "PageNumber": 0, "PageSize": 0,
        "SortBy": None, "SortAscending": False,
    },
    "LicenseCriteria": {
        "LicenseNumber": None, "LicenseTypeId": None, "LicenseClassId": None,
        "LicenseStatusId": None, "BusinessStatusId": None,
        "ApplicationDateFrom": None, "ApplicationDateTo": None,
        "IssueDateFrom": None, "IssueDateTo": None, "ExpirationDateFrom": None,
        "ExpirationDateTo": None, "SearchMainAddress": False,
        "CompanyTypeId": None, "CompanyName": None, "BusinessTypeId": None,
        "Description": None, "CompanyOpenedDateFrom": None,
        "CompanyOpenedDateTo": None, "CompanyClosedDateFrom": None,
        "CompanyClosedDateTo": None, "LastAuditDateFrom": None,
        "LastAuditDateTo": None, "ParcelNumber": None, "Address": None,
        "TaxID": None, "DBA": None, "ExcludeCases": None, "TypeId": None,
        "WorkClassIds": None, "ContactId": None, "HolderFirstName": None,
        "HolderMiddleName": None, "HolderLastName": None, "MainParcel": None,
        "EnableDescriptionSearchForBLicense": False,
        "EnableDescriptionSearchForPLicense": False,
        "EnableDescriptionSearchForOperationalPermit": False,
        "IsOperationalPermit": False, "PageNumber": 0, "PageSize": 0,
        "SortBy": None, "SortAscending": False,
    },
    "ProjectCriteria": {
        "ProjectNumber": None, "ProjectName": None, "Address": None,
        "ParcelNumber": None, "StartDateFrom": None, "StartDateTo": None,
        "ExpectedEndDateFrom": None, "ExpectedEndDateTo": None,
        "CompleteDateFrom": None, "CompleteDateTo": None, "Description": None,
        "SearchMainAddress": False, "ContactId": None, "TypeId": None,
        "ExcludeCases": None, "EnableDescriptionSearch": False, "PageNumber": 0,
        "PageSize": 0, "SortBy": None, "SortAscending": False,
    },
    "ExcludeCases": None,
    "HiddenInspectionTypeIDs": None,
    "PageNumber": 1,
    "PageSize": 50,
    "SortBy": None,
    "SortAscending": True,
}


def _search_keyword(keyword: str, page_size: int = 50) -> dict:
    """POST a single keyword search; return decoded JSON (raises on HTTP error)."""
    payload = dict(_BASE_PAYLOAD)
    payload["Keyword"] = keyword
    payload["PageSize"] = page_size
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, method="POST", headers=TENANT_HEADERS)
    with urllib.request.urlopen(req, context=_SSL_CONTEXT, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())


# ── Top-level enrichment ────────────────────────────────────────────


def enrich_notices(notices: list[NoticeData], rate_limit_seconds: float = RATE_LIMIT_SECONDS) -> None:
    """Enrich Richmond City NoticeData records with OPP code-case data.

    For each Richmond record:
      - Strip street suffix → use as OPP Keyword
      - POST /search/search
      - Post-filter results to records whose address matches the input
      - Count active code violations and recent permits
      - Write fields onto the NoticeData in-place
    Records not in Richmond are skipped (no API call).
    """
    targets = [n for n in notices if _is_richmond_city(n) and n.address]
    if not targets:
        logger.info("  Richmond OPP: no Richmond City records to enrich")
        return

    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=365 * RECENT_PERMIT_YEARS)
    cutoff_iso = cutoff.isoformat()

    logger.info("  Richmond OPP: enriching %d Richmond record(s)", len(targets))

    active_count = 0
    error_count = 0

    for n in targets:
        parts = _AddressParts.parse(n.address)
        if not parts:
            logger.debug("    skip: cannot parse address %r", n.address)
            n.opp_checked = "skip_bad_address"
            continue

        keyword = _normalize_for_opp_keyword(n.address)
        if not keyword:
            n.opp_checked = "skip_bad_address"
            continue

        try:
            data = _search_keyword(keyword)
        except HTTPError as e:
            logger.warning("    HTTP %d for %r: %s", e.code, n.address, e.read()[:200].decode(errors="replace"))
            n.opp_checked = "http_error"
            error_count += 1
            time.sleep(rate_limit_seconds)
            continue
        except (URLError, TimeoutError, OSError) as e:
            logger.warning("    network error for %r: %s", n.address, e)
            n.opp_checked = "network_error"
            error_count += 1
            time.sleep(rate_limit_seconds)
            continue

        entities = (data.get("Result") or {}).get("EntityResults") or []
        matched = [e for e in entities if _entity_matches_input(e, parts)]

        # Compute fields
        active_violations = [e for e in matched if _is_code_case(e) and _is_active_status(e)]
        all_code_cases = [e for e in matched if _is_code_case(e)]
        recent_permits = [e for e in matched if _is_recent_permit(e, cutoff_iso)]

        # Use the most-recent ScheduleDate or RequestDate for "latest" date
        def _latest_dt(e: dict) -> str:
            return e.get("ScheduleDate") or e.get("RequestDate") or e.get("ApplyDate") or ""
        active_violations.sort(key=_latest_dt, reverse=True)

        n.opp_active_violation_count = str(len(active_violations))
        n.opp_total_code_case_count = str(len(all_code_cases))
        n.opp_recent_permit_count = str(len(recent_permits))
        n.opp_active_violation_cases = "|".join(
            (e.get("CaseNumber") or "") for e in active_violations[:5]
        )
        n.opp_latest_violation_status = (
            active_violations[0].get("CaseStatus", "") if active_violations else ""
        )
        n.opp_latest_violation_date = (
            (_latest_dt(active_violations[0]) or "")[:10] if active_violations else ""
        )
        # MainParcel from OPP — useful even when there are no active cases
        for e in matched:
            parcel = e.get("MainParcel")
            if parcel:
                n.opp_parcel_id = parcel
                break

        n.opp_checked = "yes"
        if active_violations:
            active_count += 1

        time.sleep(rate_limit_seconds)

    logger.info(
        "  Richmond OPP: %d active violation, %d error, %d total queried",
        active_count, error_count, len(targets),
    )


# ── CLI smoke test ──────────────────────────────────────────────────


def _cli() -> None:
    """Quick CLI: pass an address, print what OPP returns."""
    import argparse
    import sys

    p = argparse.ArgumentParser(description="Richmond OPP single-address smoke test")
    p.add_argument("address", help='e.g. "10 E Baker St"')
    args = p.parse_args()

    n = NoticeData(
        address=args.address,
        city="Richmond",
        state="VA",
        county="Richmond City",
    )
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    enrich_notices([n])
    for f in (
        "opp_checked",
        "opp_active_violation_count",
        "opp_total_code_case_count",
        "opp_recent_permit_count",
        "opp_active_violation_cases",
        "opp_latest_violation_status",
        "opp_latest_violation_date",
        "opp_parcel_id",
    ):
        print(f"  {f}: {getattr(n, f, '')!r}")


if __name__ == "__main__":
    _cli()
