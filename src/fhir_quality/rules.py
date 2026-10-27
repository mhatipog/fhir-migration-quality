"""FHIR R4 *subset* structural checks and explicit pipeline-quality policies.

Never substitute these rules for the official HL7 Java validator.
"""
from __future__ import annotations
import datetime as dt
import json
import math
from decimal import Decimal, InvalidOperation
import re
from dataclasses import dataclass, field
from collections.abc import Iterator, Mapping
from urllib.parse import urlsplit
from typing import Any
from .models import Issue

ID_RE = re.compile(r"^[A-Za-z0-9.\-]{1,64}$")
TYPE_RE = re.compile(r"^[A-Z][A-Za-z0-9]{0,63}$")
RELATIVE_RE = re.compile(r"^([A-Z][A-Za-z0-9]+)/([A-Za-z0-9.\-]{1,64})(?:/_history/([A-Za-z0-9.\-]{1,64}))?$")
PARTIAL_DATE_RE = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?$")
FHIR_DATETIME_RE = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))?$")


class DuplicateJSONKey(ValueError):
    pass


def strict_json_loads(text: str) -> Any:
    def _unique(pairs: list[tuple[str, Any]]) -> dict:
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise DuplicateJSONKey("Duplicate JSON object key")
            obj[key] = value
        return obj
    def _reject_constant(constant: str):
        raise ValueError("Non-finite JSON constant")
    def _safe_float(raw: str) -> float:
        # CPython's JSON decoder normally rounds all decimals to binary floats.
        # Never silently rewrite a clinical measurement during NDJSON normalization.
        # If its decimal value cannot survive a str(float) round trip, quarantine.
        try:
            precise = Decimal(raw)
            value = float(raw)
            if not math.isfinite(value) or Decimal(str(value)) != precise:
                raise ValueError("JSON decimal is not safe to normalize as a float")
            return value
        except (InvalidOperation, OverflowError):
            raise ValueError("Invalid JSON decimal") from None
    return json.loads(text, object_pairs_hook=_unique, parse_constant=_reject_constant, parse_float=_safe_float)


def _valid_date(value: str, *, date_time: bool = False) -> bool:
    pattern = FHIR_DATETIME_RE if date_time else PARTIAL_DATE_RE
    if not isinstance(value, str) or not pattern.fullmatch(value):
        return False
    try:
        if len(value) == 4:
            return 1 <= int(value) <= 9999
        if len(value) == 7:
            dt.date.fromisoformat(value + "-01")
        elif "T" not in value:
            dt.date.fromisoformat(value)
        else:
            stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                return False
        return True
    except (ValueError, OverflowError):
        return False


def _instant(value: Any) -> dt.datetime | None:
    """Parse an R4 instant (a full, timezone-aware timestamp), if valid."""
    if not isinstance(value, str) or "T" not in value or not _valid_date(value, date_time=True):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _datetime_order_violation(start: Any, end: Any) -> bool:
    """Compare instants where possible, and complete dates otherwise.

    Partial dates are intentionally not ordered: neither relative timezone nor
    day-precision can support a definitive within-day chronological claim.
    """
    first, second = _instant(start), _instant(end)
    if first and second:
        return first > second
    if isinstance(start, str) and isinstance(end, str) and len(start) == len(end) == 10:
        first_date, second_date = _date_prefix(start), _date_prefix(end)
        return bool(first_date and second_date and first_date > second_date)
    return False


def _date_prefix(value: Any) -> dt.date | None:
    """Only compare complete dates. Partial dates have ambiguous ordering."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def iter_references(obj: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Visit reference strings in source order without recursive Python calls.

    Keep JSON Pointer paths for issue reports. We only queue nested containers;
    a large leaf-heavy Observation otherwise spends most of its validation time
    allocating generators and joining paths for non-reference scalar values.
    """
    pending = [(obj, path)]
    while pending:
        value, pointer = pending.pop()
        if isinstance(value, dict):
            ref = value.get("reference")
            if isinstance(ref, str):
                yield pointer + "/reference", ref
            for key, child in reversed(tuple(value.items())):
                if key != "reference" and isinstance(child, (dict, list)):
                    escaped = key.replace("~", "~0").replace("/", "~1")
                    pending.append((child, pointer + "/" + escaped))
        elif isinstance(value, list):
            for i in range(len(value) - 1, -1, -1):
                child = value[i]
                if isinstance(child, (dict, list)):
                    pending.append((child, pointer + "/" + str(i)))


def iter_reference_values(obj: Any) -> Iterator[str]:
    """Lightweight reference scan for dependency indexing (no paths required)."""
    pending = [obj]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            ref = value.get("reference")
            if isinstance(ref, str):
                yield ref
            pending.extend(child for key, child in value.items()
                           if key != "reference" and isinstance(child, (dict, list)))
        elif isinstance(value, list):
            pending.extend(child for child in value if isinstance(child, (dict, list)))


def _coding_set(codeable: Any) -> list[tuple[str, str]]:
    if not isinstance(codeable, dict) or not isinstance(codeable.get("coding"), list):
        return []
    return [(x.get("system"), x.get("code")) for x in codeable["coding"]
            if isinstance(x, dict) and isinstance(x.get("system"), str)
            and isinstance(x.get("code"), str)]


@dataclass(frozen=True)
class Policy:
    # These rules are *local ETL policy*, not globally mandated FHIR semantics.
    require_patient_reference: bool = True
    check_patient_consistency: bool = True
    require_observation_value: bool = False
    reference_scope: str = "closed"  # closed (must exist), open (unknown -> warning)
    complete_resource_types: frozenset[str] = field(default_factory=frozenset)
    trusted_reference_bases: tuple[str, ...] = ()
    allowed_observation_codes: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def __post_init__(self):
        if self.reference_scope not in ("closed", "open"):
            raise ValueError("reference_scope must be 'closed' or 'open'")
        if not isinstance(self.complete_resource_types, (set, frozenset)) or not all(
            isinstance(t, str) and TYPE_RE.fullmatch(t) for t in self.complete_resource_types
        ):
            raise ValueError("complete_resource_types must be a set of resource type names")
        # Only explicit, HTTPS, normalized FHIR service roots are eligible for
        # mapping absolute links to local resourceType/id. Never infer a trust
        # root from a resource or a user-supplied reference itself.
        if not isinstance(self.trusted_reference_bases, tuple):
            raise ValueError("trusted_reference_bases must be a tuple")
        for base in self.trusted_reference_bases:
            if not isinstance(base, str) or not base or base.endswith("/"):
                raise ValueError("trusted reference base must be a nonempty HTTPS URL without trailing slash")
            parsed = urlsplit(base)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or not parsed.netloc or "\\" in base
                    or parsed.port == 0 or "//" in parsed.path):
                raise ValueError("trusted reference base must be a plain HTTPS service root")


def local_issues(resource: Any, policy: Policy | None = None) -> list[Issue]:
    p = policy or Policy()
    result: list[Issue] = []
    def add(code: str, path: str, message: str, severity: str = "error"):
        result.append(Issue(code, severity, path, message))

    if not isinstance(resource, dict):
        return [Issue("FHIR_OBJECT_REQUIRED", "error", "", "FHIR resource must be a JSON object")]
    rtype = resource.get("resourceType")
    if not isinstance(rtype, str) or not TYPE_RE.fullmatch(rtype):
        add("FHIR_RESOURCE_TYPE", "/resourceType", "Missing or invalid resourceType")
    rid = resource.get("id")
    # FHIR id is not required on all resources; the *bulk migration pipeline* needs one.
    if not isinstance(rid, str) or not ID_RE.fullmatch(rid):
        add("DQ_ID_REQUIRED", "/id", "A valid logical id is required for migration indexing")
    if rtype == "Patient":
        birth = resource.get("birthDate")
        if birth is not None and not _valid_date(birth):
            add("FHIR_PATIENT_BIRTHDATE", "/birthDate", "Invalid FHIR date")
        for k in ("name", "identifier"):
            if k in resource and not isinstance(resource[k], list):
                add("FHIR_EXPECTED_ARRAY", "/" + k, "Expected array for repeated field")
    elif rtype == "Encounter":
        if not isinstance(resource.get("status"), str) or resource.get("status") not in {"planned", "arrived", "triaged", "in-progress", "onleave", "finished", "cancelled", "entered-in-error", "unknown"}:
            add("FHIR_ENCOUNTER_STATUS", "/status", "Missing or invalid R4 encounter status")
        if not isinstance(resource.get("class"), dict) or not resource["class"].get("code") or not resource["class"].get("system"):
            add("FHIR_ENCOUNTER_CLASS", "/class", "Encounter.class requires a Coding")
        period = resource.get("period")
        if isinstance(period, dict):
            for k in ("start", "end"):
                if k in period and not _valid_date(period[k], date_time=True):
                    add("FHIR_ENCOUNTER_PERIOD", "/period/" + k, "Invalid FHIR dateTime")
            if _datetime_order_violation(period.get("start"), period.get("end")):
                add("DQ_PERIOD_ORDER", "/period", "End precedes start")
        elif period is not None:
            add("FHIR_ENCOUNTER_PERIOD", "/period", "Expected Period object")
    elif rtype == "Observation":
        if not isinstance(resource.get("status"), str) or resource.get("status") not in {"registered", "preliminary", "final", "amended", "corrected", "cancelled", "entered-in-error", "unknown"}:
            add("FHIR_OBSERVATION_STATUS", "/status", "Missing or invalid R4 observation status")
        if not isinstance(resource.get("code"), dict) or not (resource["code"].get("coding") or resource["code"].get("text")):
            add("FHIR_OBSERVATION_CODE", "/code", "Observation.code requires a CodeableConcept")
        if p.require_patient_reference and not isinstance(resource.get("subject"), dict):
            add("DQ_OBSERVATION_SUBJECT", "/subject", "Observation requires subject for this migration policy")
        if p.require_observation_value and not any(k.startswith("value") and len(k) > 5 and k[5].isupper() for k in resource) and "dataAbsentReason" not in resource and "component" not in resource:
            add("DQ_OBSERVATION_VALUE", "", "Observation has neither value nor absence reason")
        if "effectiveDateTime" in resource and not _valid_date(resource["effectiveDateTime"], date_time=True):
            add("FHIR_OBSERVATION_DATETIME", "/effectiveDateTime", "Invalid R4 dateTime")
        if "issued" in resource and _instant(resource["issued"]) is None:
            add("FHIR_OBSERVATION_ISSUED", "/issued", "issued requires an R4 instant with timezone")
        # FHIR choice fields allow a single variant of value[x] and effective[x].
        values = [k for k in resource if k.startswith("value") and len(k) > 5 and k[5].isupper()]
        effective = [k for k in resource if k.startswith("effective") and len(k) > 9 and k[9].isupper()]
        if len(values) > 1:
            add("FHIR_OBSERVATION_MULTIPLE_VALUES", "", "Observation value[x] has multiple representations")
        if len(effective) > 1:
            add("FHIR_OBSERVATION_MULTIPLE_EFFECTIVES", "", "Observation effective[x] has multiple representations")
        if values and "dataAbsentReason" in resource:
            add("FHIR_OBSERVATION_VALUE_WITH_ABSENT_REASON", "/dataAbsentReason", "A value and dataAbsentReason must not coexist")
        for sys, code in _coding_set(resource.get("code")):
            permitted = p.allowed_observation_codes.get(sys)
            if permitted is not None and code not in permitted:
                add("DQ_CODE_NOT_IN_ALLOWLIST", "/code/coding", "Code absent from locally configured allowlist")
        quantity = resource.get("valueQuantity")
        if quantity is not None and not isinstance(quantity, dict):
            add("FHIR_QUANTITY_SHAPE", "/valueQuantity", "Quantity must be an object")
        if isinstance(quantity, dict):
            if "value" in quantity and (not isinstance(quantity["value"], (int, float)) or isinstance(quantity["value"], bool)):
                add("FHIR_QUANTITY_VALUE", "/valueQuantity/value", "Quantity value must be numeric")
            if quantity.get("system") == "http://unitsofmeasure.org" and not isinstance(quantity.get("code"), str):
                add("DQ_UCUM_CODE_MISSING", "/valueQuantity/code", "UCUM system is present without a coded unit", "warning")
    # Do not claim unimplemented FHIR profile, invariant, or terminology checks.
    return result


def relative_key(reference: str) -> tuple[str, str] | None:
    m = RELATIVE_RE.fullmatch(reference)
    return (m.group(1), m.group(2)) if m and m.group(3) is None else None


def is_versioned_reference(reference: str) -> bool:
    """A relative R4 version-specific reference; a simple latest-version index cannot verify it."""
    match = RELATIVE_RE.fullmatch(reference)
    return bool(match and match.group(3) is not None)
