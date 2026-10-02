"""RBI regional offices, parsed from the ``drRegionalOffice`` dropdown."""

from __future__ import annotations

import re

from imda.models import Dataset, Office, Source
from imda.sources.base import ParseError
from imda.sources.rbi.aspnet import extract_select_options

OFFICE_SELECT = "drRegionalOffice"
ALL_OFFICES_VALUE = "0"

OFFICE_STATE: dict[str, str] = {
    "agartala": "Tripura",
    "ahmedabad": "Gujarat",
    "aizawl": "Mizoram",
    "belapur": "Maharashtra",
    "bengaluru": "Karnataka",
    "bhopal": "Madhya Pradesh",
    "bhubaneswar": "Odisha",
    "chandigarh": "Chandigarh",
    "chennai": "Tamil Nadu",
    "dehradun": "Uttarakhand",
    "gangtok": "Sikkim",
    "guwahati": "Assam",
    "hyderabad": "Telangana",
    "imphal": "Manipur",
    "itanagar": "Arunachal Pradesh",
    "jaipur": "Rajasthan",
    "jammu": "Jammu and Kashmir",
    "kanpur": "Uttar Pradesh",
    "kochi": "Kerala",
    "kohima": "Nagaland",
    "kolkata": "West Bengal",
    "lucknow": "Uttar Pradesh",
    "mumbai": "Maharashtra",
    "nagpur": "Maharashtra",
    "new-delhi": "Delhi",
    "panaji": "Goa",
    "patna": "Bihar",
    "raipur": "Chhattisgarh",
    "ranchi": "Jharkhand",
    "shillong": "Meghalaya",
    "shimla": "Himachal Pradesh",
    "srinagar": "Jammu and Kashmir",
    "thiruvananthapuram": "Kerala",
    "vijayawada": "Andhra Pradesh",
}


def slugify(label: str) -> str:
    """Kebab-case a label: ``"New Delhi"`` becomes ``"new-delhi"``."""
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def parse_offices(html: str) -> list[Office]:
    """Return every office in the dropdown (the ``Select All`` entry is skipped)."""
    options = extract_select_options(
        html, OFFICE_SELECT, source=Source.RBI, dataset=Dataset.OFFICES
    )
    offices: list[Office] = []
    for value, label in options:
        if value == ALL_OFFICES_VALUE:
            continue
        if not value.isdigit():
            raise ParseError(Source.RBI, Dataset.OFFICES, f"non-numeric office id {value!r}")
        slug = slugify(label)
        offices.append(
            Office(rbi_id=int(value), slug=slug, name=label, state=OFFICE_STATE.get(slug))
        )
    if not offices:
        raise ParseError(Source.RBI, Dataset.OFFICES, "no offices in dropdown")
    return offices
