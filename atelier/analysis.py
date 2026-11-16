from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, localcontext
import json
from typing import Any

from .composition import CompositionService, TargetComposition, weight_text
from .domain import DomainError, canonical, digest, require_identifier, utc_now
from .quality import metric_health, summarize_quality, validate_window
from .store import Store


def statistics(values: list[Decimal]) -> dict[str, str | int] | None:
    if not values:
        return None
    with localcontext() as context:
        context.prec = 32
        count = len(values)
        mean = sum(values) / Decimal(count)
        variance = sum((value - mean) ** 2 for value in values) / Decimal(count)
        ordered = sorted(values)
        if count % 2:
            median = ordered[count // 2]
        else:
            median = (ordered[count // 2 - 1] + ordered[count // 2]) / 2
        return {"count": count, "mean": str(mean), "median": str(median),
                "minimum": str(ordered[0]), "maximum": str(ordered[-1]),
                "variance": str(variance)}


def group_statistics(spec, samples: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
    for sample in samples:
        if sample["status"] != "active":
            continue
        for name, value in sample["measurements"].items():
            grouped[(sample["group_id"], name)].append(Decimal(value))
    result = {}
    for group in spec.groups:
        result[group] = {rule.name: statistics(grouped[(group, rule.name)]) for rule in spec.rules}
    return result


def contrasts(spec, groups: dict[str, Any]) -> dict[str, Any]:
    baseline = groups[spec.baseline]
    result = {}
    for group in spec.groups:
        if group == spec.baseline:
            continue
        metrics = {}
        for rule in spec.rules:
            reference = baseline[rule.name]
            candidate = groups[group][rule.name]
            if reference is None or candidate is None:
                metrics[rule.name] = {"difference": None, "relative_percent": None}
                continue
            mean_a = Decimal(reference["mean"])
            mean_b = Decimal(candidate["mean"])
            difference = mean_b - mean_a
            relative = None if mean_a == 0 else str(difference / abs(mean_a) * Decimal(100))
            metrics[rule.name] = {"difference": str(difference), "relative_percent": relative}
        result[group] = metrics
    return result


def _mean(values: list[Decimal]) -> Decimal:
    with localcontext() as context:
        context.prec = 32
        return sum(values) / Decimal(len(values))


def _stratum_id(shift: str | None) -> str:
    return "unassigned" if shift is None else shift


def standardize(spec, samples: list[dict[str, Any]], composition: TargetComposition,
                pooled_groups: dict[str, Any]) -> dict[str, Any]:
    """Direct standardisation of each group onto the frozen target composition.

    A stratum that carries a target weight but has no usable samples in *any*
    comparison group is reported as missing and makes the result ineligible.
    Weights are never renormalised over the strata that happen to be present,
    and layers without a target weight are reported separately rather than
    silently absorbed.
    """
    values: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
    for sample in samples:
        if sample["status"] != "active":
            continue
        for name, value in sample["measurements"].items():
            values[(sample["group_id"], sample.get("shift_id"), name)].append(Decimal(value))

    order = [shift for shift, _ in composition.strata]
    declared = set(order)
    observed: dict[str | None, int] = defaultdict(int)
    for sample in samples:
        if sample["status"] != "active":
            continue
        observed[sample.get("shift_id")] += 1

    layer_counts = {}
    for shift in order:
        layer_counts[_stratum_id(shift)] = {
            group: sum(1 for sample in samples
                       if sample["status"] == "active" and sample.get("shift_id") == shift
                       and sample["group_id"] == group)
            for group in spec.groups}

    # A weighted stratum is usable only when every comparison group has an
    # observation; otherwise its contrast is not identifiable and the missing
    # weight would have to be spread over other layers.
    missing: list[dict[str, Any]] = []
    for shift, weight in composition.strata:
        if weight == 0:
            continue
        absent = [group for group in spec.groups if layer_counts[_stratum_id(shift)][group] == 0]
        if absent:
            missing.append({"stratum": _stratum_id(shift), "weight": weight_text(weight),
                            "groups_without_samples": absent})

    # Strata present in the ledger but absent from the target composition cannot
    # be given a weight without inventing one; report them so the reviewer sees
    # the offset instead of having it hidden inside a pooled average.
    unweighted = []
    for shift in observed:
        if shift is None or shift not in declared:
            unweighted.append({"stratum": _stratum_id(shift), "samples": observed[shift],
                               "reason": "no_attribution" if shift is None else "not_in_composition"})

    # One observation per group per weighted layer is the floor for a usable
    # contrast; a composition may raise that floor explicitly.
    minimum = composition.min_samples if composition.min_samples is not None else 1
    insufficient = []
    for shift, weight in composition.strata:
        if weight == 0:
            continue
        for group in spec.groups:
            count = layer_counts[_stratum_id(shift)][group]
            if count < minimum:
                insufficient.append({"stratum": _stratum_id(shift), "group": group,
                                     "samples": count, "required": minimum})

    per_layer: dict[str, Any] = {}
    for shift, weight in composition.strata:
        entry: dict[str, Any] = {"weight": weight_text(weight), "groups": {}}
        for group in spec.groups:
            group_entry: dict[str, Any] = {}
            for rule in spec.rules:
                cell = values.get((group, shift, rule.name), [])
                group_entry[rule.name] = statistics(cell)
            entry["groups"][group] = group_entry
        per_layer[_stratum_id(shift)] = entry

    standardized: dict[str, Any] = {}
    for rule in spec.rules:
        group_means: dict[str, str | None] = {}
        components: dict[str, dict[str, str | None]] = {}
        for group in spec.groups:
            total = Decimal("0")
            complete = True
            pieces: dict[str, str | None] = {}
            for shift, weight in composition.strata:
                cell = values.get((group, shift, rule.name), [])
                if not cell:
                    pieces[_stratum_id(shift)] = None
                    if weight != 0:
                        complete = False
                    continue
                mean = _mean(cell)
                total += weight * mean
                pieces[_stratum_id(shift)] = str(mean)
            group_means[group] = str(total) if complete else None
            components[group] = pieces
        baseline_mean = group_means[spec.baseline]
        layer_contrasts = {}
        for group in spec.groups:
            if group == spec.baseline:
                continue
            if group_means[group] is None or baseline_mean is None:
                layer_contrasts[group] = {"difference": None, "relative_percent": None,
                                          "standardized_mean": group_means[group]}
                continue
            difference = Decimal(group_means[group]) - Decimal(baseline_mean)
            relative = (None if Decimal(baseline_mean) == 0
                        else str(difference / abs(Decimal(baseline_mean)) * Decimal(100)))
            layer_contrasts[group] = {"difference": str(difference),
                                      "relative_percent": relative,
                                      "standardized_mean": group_means[group]}
        standardized[rule.name] = {"group_means": group_means, "components": components,
                                   "contrasts": layer_contrasts}

    eligible = not missing and not insufficient and all(
        item["group_means"][spec.baseline] is not None for item in standardized.values())
    coverage = _observed_composition(order, layer_counts, samples, spec)
    shift_effect = _composition_effect(spec, pooled_groups, standardized, composition,
                                       coverage)
    return {
        "composition_id": composition.composition_id,
        "composition_version": composition.version,
        "population": composition.population,
        "weights": {_stratum_id(shift): weight_text(weight) for shift, weight in composition.strata},
        "spec_version": spec.version,
        "min_samples_per_layer": minimum,
        "layers": {_stratum_id(shift): {
            "weight": weight_text(weight),
            "sample_ids_by_group": _sample_ids(samples, spec, shift),
        } for shift, weight in composition.strata},
        "layer_counts": layer_counts,
        "missing_weighted_strata": missing,
        "unweighted_strata": unweighted,
        "insufficient_layers": insufficient,
        "observed_composition": coverage,
        "per_layer": per_layer,
        "standardized": standardized,
        "composition_effect": shift_effect,
        "eligible": eligible,
        "reasons": _eligibility_reasons(missing, insufficient, standardized, spec),
    }


def _sample_ids(samples: list[dict[str, Any]], spec, shift: str) -> dict[str, list[str]]:
    return {group: sorted(sample["sample_id"] for sample in samples
                          if sample["status"] == "active" and sample.get("shift_id") == shift
                          and sample["group_id"] == group)
            for group in spec.groups}


def _composition_effect(spec, pooled_groups, standardized, composition,
                        coverage) -> dict[str, Any]:
    """Split each pooled contrast into within-layer effect and mix effects.

    ``pooled = within_layer + composition_mix_effect + pooling_residual``.
    The decomposition is produced only when every weighted layer is observed;
    a missing layer leaves the gap explicit instead of reallocating its weight
    to the layers that remain.
    """
    weighted_counts = coverage["weighted_layers"]
    required = [shift for shift, weight in composition.strata if weight != 0]
    observed_total = sum(sum(weighted_counts[shift].values()) for shift in required)
    observed_weights: dict[str, Decimal] = {}
    if observed_total:
        for shift in required:
            observed_weights[shift] = (Decimal(sum(weighted_counts[shift].values()))
                                       / Decimal(observed_total))
    complete = len(observed_weights) == len(required)
    result: dict[str, Any] = {}
    for rule in spec.rules:
        entry: dict[str, Any] = {}
        for group in spec.groups:
            if group == spec.baseline:
                continue
            within_mean = standardized[rule.name]["group_means"][group]
            baseline_within = standardized[rule.name]["group_means"][spec.baseline]
            within = (str(Decimal(within_mean) - Decimal(baseline_within))
                      if within_mean is not None and baseline_within is not None else None)
            observed_means: dict[str, str | None] = {}
            for side in (spec.baseline, group):
                total = Decimal("0")
                ok = complete
                for shift, _ in composition.strata:
                    cell = standardized[rule.name]["components"][side].get(shift)
                    if cell is None:
                        ok = False
                        break
                    total += observed_weights[shift] * Decimal(cell)
                observed_means[side] = str(total) if ok else None
            if observed_means[spec.baseline] is not None and observed_means[group] is not None:
                observed_difference = (Decimal(observed_means[group])
                                       - Decimal(observed_means[spec.baseline]))
            else:
                observed_difference = None
            reference = pooled_groups[spec.baseline][rule.name]
            candidate = pooled_groups[group][rule.name]
            pooled = None
            if reference is not None and candidate is not None:
                pooled = Decimal(candidate["mean"]) - Decimal(reference["mean"])
            mix = (str(observed_difference - Decimal(within))
                   if within is not None and observed_difference is not None else None)
            residual = (str(pooled - observed_difference)
                        if pooled is not None and observed_difference is not None else None)
            entry[group] = {
                "within_layer_difference": within,
                "observed_mix_difference": (str(observed_difference)
                                            if observed_difference is not None else None),
                "composition_mix_effect": mix,
                "pooling_residual": residual,
                "pooled_difference": str(pooled) if pooled is not None else None,
            }
        result[rule.name] = entry
    return {"observed_weights": {shift: str(weight) for shift, weight in observed_weights.items()},
            "target_weights": {shift: weight_text(weight) for shift, weight in composition.strata},
            "complete": complete,
            "metrics": result}


def _observed_composition(order, layer_counts, samples, spec) -> dict[str, Any]:
    active = sum(1 for sample in samples if sample["status"] == "active")
    weighted = {_stratum_id(shift): layer_counts[_stratum_id(shift)] for shift in order}
    weighted_total = sum(sum(counts.values()) for counts in weighted.values())
    return {"weighted_layers": weighted, "weighted_samples": weighted_total,
            "unweighted_samples": active - weighted_total, "active_samples": active,
            "by_group": {group: sum(layer_counts[_stratum_id(shift)][group] for shift in order)
                         for group in spec.groups}}


def _eligibility_reasons(missing, insufficient, standardized, spec) -> list[str]:
    reasons = []
    if missing:
        reasons.append("missing_weighted_stratum")
    if insufficient:
        reasons.append("insufficient_layer_samples")
    if any(item["group_means"][spec.baseline] is None for item in standardized.values()):
        reasons.append("standardized_baseline_incomplete")
    return reasons


def excluded_samples(spec, samples: list[dict[str, Any]], start: str, end: str,
                     rejections: list[dict[str, Any]], composition: TargetComposition | None,
                     minimum: int) -> list[dict[str, Any]]:
    """Enumerate every sample the stratified analysis did not use, with a reason."""
    weighted = ({shift for shift, _ in composition.strata} if composition is not None else set())
    reasons: dict[str, list[str]] = defaultdict(list)
    used: set[str] = set()
    for sample in samples:
        if sample["status"] != "active":
            reasons[sample["sample_id"]].append("void")
            continue
        if composition is None:
            used.add(sample["sample_id"])
            continue
        if sample.get("shift_id") not in weighted:
            reasons[sample["sample_id"]].append(
                "no_attribution" if sample.get("shift_id") is None else "not_in_composition")
            continue
        used.add(sample["sample_id"])
    if composition is not None:
        counts: dict[tuple[str, str], int] = defaultdict(int)
        for sample in samples:
            if sample["sample_id"] in used:
                counts[(sample.get("shift_id"), sample["group_id"])] += 1
        for shift, weight in composition.strata:
            if weight == 0:
                continue
            for group in spec.groups:
                if counts[(shift, group)] < minimum:
                    for sample in samples:
                        if (sample["sample_id"] in used and sample.get("shift_id") == shift
                                and sample["group_id"] == group):
                            reasons[sample["sample_id"]].append("insufficient_layer_samples")
    excluded = [{"sample_id": sample["sample_id"], "group_id": sample["group_id"],
                 "shift_id": sample.get("shift_id"), "status": sample["status"],
                 "reasons": sorted(set(reasons[sample["sample_id"]]))}
                for sample in samples if sample["sample_id"] in reasons]
    for rejection in rejections:
        excluded.append({"sample_id": rejection.get("sample_id"), "batch_id": rejection.get("batch_id"),
                         "event_time": rejection.get("event_time"), "status": "rejected",
                         "reasons": ["ingest_rejection:" + rejection["reason"]]})
    return excluded


class Analyzer:
    def __init__(self, store: Store):
        self.store = store
        self.compositions = CompositionService(store)

    def build(self, campaign_id: str, analysis_id: str, start: str, end: str,
              actor: str, composition_id: str | None = None,
              composition_version: int | None = None) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(analysis_id, "analysis_id")
        require_identifier(actor, "actor")
        with self.store.transaction() as db:
            spec = self.store.get_spec(campaign_id, db=db)
            start, end = validate_window(spec, start, end)
            composition = None
            composition_sha = None
            if composition_id is not None:
                require_identifier(composition_id, "composition_id")
                composition, composition_sha = self.compositions.load(
                    campaign_id, composition_id, composition_version, db)
            request = {"campaign_id": campaign_id, "start": start, "end": end,
                       "spec_version": spec.version,
                       "composition_id": composition.composition_id if composition else None,
                       "composition_version": composition.version if composition else None,
                       "composition_sha": composition_sha}
            request_sha = digest(request)
            current = self.store.get_campaign(campaign_id, db)
            old = db.execute("SELECT request_sha,source_revision,body FROM analyses"
                             " WHERE campaign_id=? AND analysis_id=?", (campaign_id, analysis_id)).fetchone()
            if old:
                if old["request_sha"] != request_sha:
                    raise DomainError("analysis_conflict", "analysis id names a different request")
                return {**json.loads(old["body"]), "replayed": True,
                        "current": old["source_revision"] == current["revision"]}
            samples = self.store.list_samples(campaign_id, start=start, end=end, db=db)
            rejection_rows = db.execute("SELECT batch_id,reason,detail,event_time,sample_id FROM rejections"
                                        " WHERE campaign_id=? AND event_time>=? AND event_time<?"
                                        " ORDER BY batch_id,ordinal", (campaign_id, start, end)).fetchall()
            rejections = [dict(row) for row in rejection_rows]
            quality = summarize_quality(spec, samples, rejections, start, end)
            groups = group_statistics(spec, samples)
            result = {"campaign_id": campaign_id, "analysis_id": analysis_id,
                      "analysis_kind": "stratified" if composition else "pooled",
                      "request": request, "source_revision": current["revision"],
                      "quality": quality, "metric_health": metric_health(spec, samples),
                      "groups": groups, "contrasts": contrasts(spec, groups),
                      "created_at": utc_now(), "replayed": False, "current": True}
            if composition is not None:
                standardized = standardize(spec, samples, composition, groups)
                minimum = standardized["min_samples_per_layer"]
                result["target_composition"] = {
                    "composition_id": composition.composition_id,
                    "version": composition.version,
                    "content_sha": composition_sha,
                    "population": composition.population,
                    "weights": standardized["weights"],
                    "min_samples_per_layer": minimum,
                    "is_current_version": self.compositions.is_current(
                        campaign_id, composition.composition_id, composition.version, db),
                }
                result["layers"] = standardized["layers"]
                result["layer_counts"] = standardized["layer_counts"]
                result["per_layer"] = standardized["per_layer"]
                result["standardized"] = standardized["standardized"]
                result["observed_composition"] = standardized["observed_composition"]
                result["composition_effect"] = standardized["composition_effect"]
                result["missing_weighted_strata"] = standardized["missing_weighted_strata"]
                result["unweighted_strata"] = standardized["unweighted_strata"]
                result["insufficient_layers"] = standardized["insufficient_layers"]
                result["stratified_eligible"] = standardized["eligible"]
                result["stratification_reasons"] = standardized["reasons"]
                weighted_shifts = set(standardized["weights"])
                result["analyzed_sample_ids"] = {
                    group: sorted(sample["sample_id"] for sample in samples
                                  if sample["status"] == "active"
                                  and sample["group_id"] == group
                                  and sample.get("shift_id") in weighted_shifts)
                    for group in spec.groups}
                result["excluded_samples"] = excluded_samples(
                    spec, samples, start, end, rejections, composition, minimum)
                result["quality"]["stratified_passed"] = (
                    result["quality"]["passed"] and standardized["eligible"])
                result["quality"]["stratification_gates"] = [
                    f"missing_weighted_stratum:{item['stratum']}"
                    for item in standardized["missing_weighted_strata"]]
                result["quality"]["stratification_gates"] += [
                    f"insufficient_layer_samples:{item['stratum']}:{item['group']}"
                    for item in standardized["insufficient_layers"]]
                result["quality"]["passed"] = (result["quality"]["passed"]
                                               and standardized["eligible"])
            result_sha = digest(result)
            result["result_sha"] = result_sha
            db.execute("INSERT INTO analyses(campaign_id,analysis_id,request_sha,source_revision,body,"
                       "result_sha,created_at) VALUES(?,?,?,?,?,?,?)",
                       (campaign_id, analysis_id, request_sha, current["revision"], canonical(result),
                        result_sha, result["created_at"]))
            self.store.audit(db, campaign_id, "analysis_created", actor, analysis_id, None,
                             {"result_sha": result_sha, "source_revision": current["revision"],
                              "analysis_kind": result["analysis_kind"]})
            return result

    def shift_impact(self, campaign_id: str, analysis_id: str) -> dict[str, Any]:
        """List corrections that happened after an analysis was frozen."""
        with self.store.connect() as db:
            analysis = self.store.get_analysis(campaign_id, analysis_id, db)
            body = analysis["body"]
            rows = db.execute("SELECT sample_id,before_shift,after_shift,actor,reason,at"
                              " FROM shift_corrections WHERE campaign_id=? AND at>?"
                              " ORDER BY id", (campaign_id, analysis["created_at"])).fetchall()
            current = self.store.get_campaign(campaign_id, db)
            return {"campaign_id": campaign_id, "analysis_id": analysis_id,
                    "source_revision": analysis["source_revision"],
                    "current_revision": current["revision"],
                    "current": analysis["source_revision"] == current["revision"],
                    "composition_id": body.get("request", {}).get("composition_id"),
                    "composition_version": body.get("request", {}).get("composition_version"),
                    "later_shift_corrections": [dict(row) for row in rows],
                    "must_reanalyse": bool(rows) or analysis["source_revision"] != current["revision"]}

    def status(self, campaign_id: str, analysis_id: str) -> dict[str, Any]:
        with self.store.connect() as db:
            analysis = self.store.get_analysis(campaign_id, analysis_id, db)
            campaign = self.store.get_campaign(campaign_id, db)
            request = analysis["body"].get("request", {})
            composition_current = None
            if request.get("composition_id"):
                composition_current = self.compositions.is_current(
                    campaign_id, request["composition_id"], request["composition_version"], db)
            return {"analysis_id": analysis_id, "source_revision": analysis["source_revision"],
                    "current_revision": campaign["revision"],
                    "current": analysis["source_revision"] == campaign["revision"],
                    "result_sha": analysis["result_sha"],
                    "analysis_kind": analysis["body"].get("analysis_kind", "pooled"),
                    "composition_id": request.get("composition_id"),
                    "composition_version": request.get("composition_version"),
                    "composition_current": composition_current}

    def list_analyses(self, campaign_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.store.connect() as db:
            self.store.get_campaign(campaign_id, db)
            rows = db.execute("SELECT analysis_id,source_revision,result_sha,created_at,body"
                              " FROM analyses WHERE campaign_id=? ORDER BY created_at DESC LIMIT ?",
                              (campaign_id, limit)).fetchall()
            items = []
            for row in rows:
                request = json.loads(row["body"]).get("request", {})
                items.append({"analysis_id": row["analysis_id"],
                              "source_revision": row["source_revision"],
                              "result_sha": row["result_sha"], "created_at": row["created_at"],
                              "analysis_kind": json.loads(row["body"]).get("analysis_kind", "pooled"),
                              "composition_id": request.get("composition_id"),
                              "composition_version": request.get("composition_version")})
            return items
