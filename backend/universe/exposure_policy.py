"""Reject explicit leveraged/inverse objectives from provider reference names.

Buying a share without margin does not make a geared fund unleveraged. This
check is independent of stock/ETF classification, which may itself be missing.
Generic 'short term' in a bond fund name is not an inverse objective.
"""

import re
from decimal import Decimal
from typing import Any


def geared_exposure(metadata: dict[str, Any]) -> bool:
    name = str(metadata.get("asset_name") or "")
    if re.search(
        r"\b(?:leveraged|inverse)\b|\bproshares\s+(?:ultra(?:pro|short)?|short)\b|\bdaily\b.*\bbear\b",
        name,
        re.IGNORECASE,
    ):
        return True
    for match in re.finditer(r"(?<!\w)([+-]?\d+(?:\.\d+)?)[x×](?!\w)", name, re.IGNORECASE):
        if Decimal(match.group(1)) != 1:
            return True
    return False
