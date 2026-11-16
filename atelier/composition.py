from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
from typing import Any, Iterator

from .domain import (DomainError, canonical, decimal, digest, integer, require_identifier,
                     utc_now)
from .store import Store


def weight_text(value: Decimal) -> str:
    """Render a target weight without trailing zeros so version digests are stable."""
    return format(value.normalize(), "f")


@dataclass(frozen=True)
class TargetComposition:
    """A versioned target production mix used for direct standardisation.

    ``strata`` maps shift identifiers to target weights.  The weights always sum
    to exactly one; analyses must never rescale them onto the strata they happen
    to observe.
    """

    composition_id: str
    version: int
    title: str
    population: str
    strata: tuple[tuple[str, Decimal], ...]
    min_samples: int | None

    @classmethod
    def from_dict(cls, item: Any, version: int = 1) -> "TargetComposition":
        if not isinstance(item, dict):
            raise DomainError("invalid_composition", "target composition must be an object")
        composition_id = require_identifier(item.get("composition_id"), "composition_id")
        title = item.get("title")
        if not isinstance(title, str) or not 3 <= len(title.strip()) <= 160:
            raise DomainError("invalid_composition", "composition title must have 3-160 characters")
        population = item.get("population")
        if not isinstance(population, str) or not 1 <= len(population.strip()) <= 160:
            raise DomainError("invalid_composition", "population must have 1-160 characters")
        strata = item.get("strata")
        if not isinstance(strata, dict) or not 1 <= len(strata) <= 24:
            raise DomainError("invalid_composition", "composition needs 1-24 strata")
        weights: list[tuple[str, Decimal]] = []
        seen: set[str] = set()
        for shift, raw in strata.items():
            shift_id = require_identifier(shift, "stratum")
            if shift_id in seen:
                raise DomainError("invalid_composition", "stratum identifiers must be unique")
            seen.add(shift_id)
            weight = decimal(raw, f"weight for {shift_id}")
            if weight < 0 or weight > 1:
                raise DomainError("invalid_composition", "stratum weights must be in [0, 1]")
            weights.append((shift_id, weight))
        total = sum((weight for _, weight in weights), Decimal("0"))
        if total != Decimal("1"):
            raise DomainError("invalid_composition",
                              "target composition weights must sum to exactly 1; "
                              "missing strata are not redistributed")
        minimum = item.get("min_samples")
        min_samples = None if minimum is None else integer(minimum, "min_samples", 1, 100000)
        return cls(composition_id, version, title.strip(), population.strip(),
                   tuple(weights), min_samples)

    @property
    def weights(self) -> dict[str, Decimal]:
        return dict(self.strata)

    def to_dict(self) -> dict[str, Any]:
        return {"composition_id": self.composition_id, "version": self.version,
                "title": self.title, "population": self.population,
                "strata": {shift: weight_text(weight) for shift, weight in self.strata},
                "min_samples": self.min_samples}


class CompositionService:
    def __init__(self, store: Store):
        self.store = store

    def create(self, campaign_id: str, composition_id: str, supplied: Any,
               actor: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(composition_id, "composition_id")
        require_identifier(actor, "actor")
        if not isinstance(supplied, dict):
            raise DomainError("invalid_composition", "target composition must be an object")
        if supplied.get("composition_id") not in (None, composition_id):
            raise DomainError("invalid_composition", "payload composition id does not match path")
        body = TargetComposition.from_dict({**supplied, "composition_id": composition_id}, 1)
        with self.store.transaction() as db:
            self.store.get_campaign(campaign_id, db)
            existing = db.execute("SELECT 1 FROM compositions WHERE campaign_id=? AND composition_id=?",
                                  (campaign_id, composition_id)).fetchone()
            if existing:
                raise DomainError("composition_conflict", "composition already exists")
            payload = body.to_dict()
            db.execute("INSERT INTO compositions(campaign_id,composition_id,version,body,content_sha,"
                       "created_at) VALUES(?,?,?,?,?,?)",
                       (campaign_id, composition_id, 1, canonical(payload), digest(payload), utc_now()))
            self.store.audit(db, campaign_id, "composition_created", actor,
                             f"{composition_id}@{body.version}", None, payload)
            return {**payload, "current_version": 1, "current": True}

    def update(self, campaign_id: str, composition_id: str, supplied: Any,
               expected_version: int, actor: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(composition_id, "composition_id")
        require_identifier(actor, "actor")
        expected_version = integer(expected_version, "expected_version", 1, 1000000)
        if not isinstance(supplied, dict):
            raise DomainError("invalid_composition", "target composition must be an object")
        if supplied.get("composition_id") not in (None, composition_id):
            raise DomainError("invalid_composition", "payload composition id does not match path")
        with self.store.transaction() as db:
            self.store.get_campaign(campaign_id, db)
            current = self._current_row(campaign_id, composition_id, db)
            if current["version"] != expected_version:
                raise DomainError("version_conflict", "target composition version changed")
            body = TargetComposition.from_dict({**supplied, "composition_id": composition_id},
                                              expected_version + 1)
            payload = body.to_dict()
            now = utc_now()
            db.execute("INSERT INTO compositions(campaign_id,composition_id,version,body,content_sha,"
                       "created_at) VALUES(?,?,?,?,?,?)",
                       (campaign_id, composition_id, body.version, canonical(payload),
                        digest(payload), now))
            self.store.audit(db, campaign_id, "composition_updated", actor,
                             f"{composition_id}@{body.version}", json.loads(current["body"]), payload)
            return {**payload, "current_version": body.version, "current": True}

    @staticmethod
    def _current_row(campaign_id: str, composition_id: str, db) -> Any:
        row = db.execute("SELECT * FROM compositions WHERE campaign_id=? AND composition_id=?"
                         " ORDER BY version DESC LIMIT 1", (campaign_id, composition_id)).fetchone()
        if row is None:
            raise DomainError("not_found", "target composition does not exist")
        return row

    def current_version(self, campaign_id: str, composition_id: str,
                        db=None) -> int:
        if db is None:
            with self.store.connect() as local:
                return self.current_version(campaign_id, composition_id, local)
        return self._current_row(campaign_id, composition_id, db)["version"]

    def is_current(self, campaign_id: str, composition_id: str, version: int,
                   db=None) -> bool:
        if db is None:
            with self.store.connect() as local:
                return self.is_current(campaign_id, composition_id, version, local)
        return self.current_version(campaign_id, composition_id, db) == version

    def load(self, campaign_id: str, composition_id: str, version: int | None = None,
             db=None) -> tuple[TargetComposition, str]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(composition_id, "composition_id")
        if db is None:
            with self.store.connect() as local:
                return self.load(campaign_id, composition_id, version, local)
        self.store.get_campaign(campaign_id, db)
        if version is None:
            row = self._current_row(campaign_id, composition_id, db)
        else:
            version = integer(version, "composition_version", 1, 1000000)
            row = db.execute("SELECT * FROM compositions WHERE campaign_id=? AND composition_id=?"
                             " AND version=?", (campaign_id, composition_id, version)).fetchone()
            if row is None:
                raise DomainError("not_found", "target composition version does not exist")
        return (TargetComposition.from_dict(json.loads(row["body"]), row["version"]),
                row["content_sha"])

    def describe(self, campaign_id: str, composition_id: str,
                 version: int | None = None) -> dict[str, Any]:
        with self.store.connect() as db:
            current = self._current_row(campaign_id, composition_id, db)
            composition, content_sha = self.load(campaign_id, composition_id, version, db)
            return {**composition.to_dict(), "content_sha": content_sha,
                    "current_version": current["version"],
                    "current": composition.version == current["version"],
                    "created_at": current["created_at"]}

    def list(self, campaign_id: str) -> list[dict[str, Any]]:
        require_identifier(campaign_id, "campaign_id")
        with self.store.connect() as db:
            self.store.get_campaign(campaign_id, db)
            rows = db.execute("SELECT composition_id,version,body,content_sha,created_at"
                              " FROM compositions WHERE campaign_id=?"
                              " ORDER BY composition_id,version", (campaign_id,)).fetchall()
            return [{**json.loads(row["body"]), "content_sha": row["content_sha"],
                     "created_at": row["created_at"]} for row in rows]

    def versions(self, campaign_id: str, composition_id: str) -> list[dict[str, Any]]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(composition_id, "composition_id")
        with self.store.connect() as db:
            self.store.get_campaign(campaign_id, db)
            rows = db.execute("SELECT version,body,content_sha,created_at FROM compositions"
                              " WHERE campaign_id=? AND composition_id=? ORDER BY version",
                              (campaign_id, composition_id)).fetchall()
            if not rows:
                raise DomainError("not_found", "target composition does not exist")
            return [{**json.loads(row["body"]), "content_sha": row["content_sha"],
                     "created_at": row["created_at"]} for row in rows]
