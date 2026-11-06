"""Label formats shared by ontology suggestion validation (#90).

`SAFE_LABEL` is the charset any label in a suggestion must use: it keeps
model or document text such as `DROP TABLE x;` out, while still allowing
ontology labels like `Person` or a Neo4j-style `WORKS_AT`. `RELATION_LABEL`
is the stricter lower_snake_case form required of observed predicates that
are sent to a model or proposed as new relation types.
"""

from __future__ import annotations

import re

MAX_LABEL_LENGTH = 64
SAFE_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
RELATION_LABEL = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")


def is_safe_label(value: str) -> bool:
    return len(value) <= MAX_LABEL_LENGTH and SAFE_LABEL.fullmatch(value) is not None


def is_relation_label(value: str) -> bool:
    return len(value) <= MAX_LABEL_LENGTH and RELATION_LABEL.fullmatch(value) is not None
