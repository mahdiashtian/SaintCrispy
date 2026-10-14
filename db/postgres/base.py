"""Native PostgreSQL model descriptors; migrations own schema changes."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TableDefinition:
    name: str
    schema: str
