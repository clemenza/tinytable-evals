#!/usr/bin/env python3
"""calibrate_gen2: turn per-trial kill outcomes into issue #64's grouped
calibration report.

Usage:
    python3 calibrate_gen2.py --trials trials.jsonl [--out calibration.json]
                              [--min-trials 5] [--material-gap 0.2] [--strict]

#64's whole point is a *comparison*, not a number: family S (single-table
compositional) is the control arm, families M (multi-table, no JOIN) and T
(transaction x multi-table) are the arms under test, and the question is
whether M/T land in a materially lower kill-rate band than S on
comparably-designed operators. A single global average would hide exactly
that, so this tool refuses to report one on its own: every summary is
per-operator and per-family, and the global figure is printed only
alongside them.

Input is a JSON array, or JSON Lines, of trial records. One record is one
trial of one operator:

    {"operator_id": "fk-referencing-update-check-skipped", "killed": true}
    {"seed": 12345, "killed": false, "profile": "baseline", "trial": 3}

`operator_id` names the operator directly; `seed` instead names the seed a
seed-root was built from, which this tool resolves through
`mutate.select_operator` (the same deterministic mapping
`build_seed_root.py` used) - so a driver that only recorded seeds doesn't
have to re-derive anything. `killed` is the boolean `grade.py` writes into
score.json. Any other keys (profile, model, trial index, pg_adjudicated,
kill_rate, ...) are carried through untouched into the per-operator
breakdown's `records` count and otherwise ignored.

The report also applies #64's own decision rule mechanically, so the JOIN
gate is settled by the data rather than by whoever reads it:

  hypothesis-supported      M and/or T sit at least --material-gap below S
  hypothesis-not-supported  neither does
  insufficient-data         some operator has fewer than --min-trials
                            trials, or a whole family has none

`insufficient-data` is never silently rounded into either verdict, and
--strict makes it a nonzero exit so CI can't mistake "we haven't run the
experiment yet" for "the experiment came back negative". Interpreting a
verdict is still a human call - see docs/gen2-operators.md's JOIN-gate
record, which is where the decision is written down.

stdlib only.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import mutate


def load_trials(path: pathlib.Path) -> list[dict]:
    """Read a JSON array or JSON Lines file of trial records."""
    text = path.read_text().strip()
    if not text:
        return []
    if text.lstrip().startswith("["):
        records = json.loads(text)
        if not isinstance(records, list):
            raise ValueError(f"{path}: expected a JSON array of trial records")
        return records
    records = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: not valid JSON: {exc}") from exc
    return records


def resolve_operator_id(record: dict) -> str:
    """The operator a trial record refers to, by id or via its seed."""
    operator_id = record.get("operator_id")
    if operator_id is None and record.get("seed") is not None:
        operator_id = mutate.select_operator(int(record["seed"])).id
    if operator_id is None:
        raise ValueError(f"trial record has neither operator_id nor seed: {record!r}")
    if operator_id not in {op.id for op in mutate.OPERATORS}:
        raise ValueError(f"unknown operator_id {operator_id!r} (stale against this mutate.py?)")
    return operator_id


def _rate(kills: int, trials: int) -> float | None:
    return None if trials == 0 else round(kills / trials, 4)


def tally(records: list[dict]) -> dict[str, dict]:
    """Per-operator {trials, kills, kill_rate} for every operator in the
    library - including the ones with no trials at all, which is how an
    unrun arm shows up as a gap rather than as an absence.
    """
    per_operator = {
        op.id: {
            "family": op.family,
            "generation": op.generation,
            "trials": 0,
            "kills": 0,
            "kill_rate": None,
        }
        for op in mutate.OPERATORS
    }
    for record in records:
        entry = per_operator[resolve_operator_id(record)]
        entry["trials"] += 1
        if record.get("killed"):
            entry["kills"] += 1
    for entry in per_operator.values():
        entry["kill_rate"] = _rate(entry["kills"], entry["trials"])
    return per_operator


def summarize_family(per_operator: dict[str, dict], family: str) -> dict:
    """Pooled and per-operator-mean kill rate for one family.

    Both are reported because they answer different questions: pooled is
    "of all trials in this arm, how many killed", mean-of-operators is
    "how hard is a typical operator in this arm" (which is the one #64's
    decision rule cares about, since it weights every operator equally
    regardless of how many trials it happened to get).
    """
    members = {oid: e for oid, e in per_operator.items() if e["family"] == family}
    sampled = {oid: e for oid, e in members.items() if e["trials"] > 0}
    trials = sum(e["trials"] for e in members.values())
    kills = sum(e["kills"] for e in members.values())
    rates = [e["kill_rate"] for e in sampled.values()]
    return {
        "label": mutate.FAMILIES[family],
        "operators": len(members),
        "operators_with_trials": len(sampled),
        "trials": trials,
        "kills": kills,
        "pooled_kill_rate": _rate(kills, trials),
        "mean_operator_kill_rate": round(sum(rates) / len(rates), 4) if rates else None,
        "min_operator_kill_rate": min(rates) if rates else None,
        "max_operator_kill_rate": max(rates) if rates else None,
        "operators_below_ceiling": sum(1 for r in rates if r < 1.0),
        "per_operator": {oid: e["kill_rate"] for oid, e in sorted(members.items())},
    }


def decide_join_gate(families: dict[str, dict], undersampled: list[str], material_gap: float) -> dict:
    """#64's decision rule, applied to the family summaries.

    Compares each hypothesis arm's mean operator kill rate against the
    control arm's. "Materially lower" is `material_gap` in absolute kill
    rate (default 0.2, i.e. 20 points) - a threshold, not a significance
    test: with 5 trials per operator this data cannot support one, which
    is itself worth saying out loud rather than dressing up.
    """
    control = families["S"]["mean_operator_kill_rate"]
    gaps = {}
    for family in ("M", "T"):
        arm = families[family]["mean_operator_kill_rate"]
        gaps[family] = None if (control is None or arm is None) else round(control - arm, 4)

    if undersampled or control is None or any(g is None for g in gaps.values()):
        verdict = "insufficient-data"
        rationale = (
            "not every operator has the minimum number of trials (or a whole family has none), "
            "so neither arm's band is established yet"
        )
    elif any(gap >= material_gap for gap in gaps.values()):
        verdict = "hypothesis-supported"
        rationale = (
            f"at least one hypothesis arm sits >= {material_gap} below the control arm's mean "
            f"operator kill rate (S={control})"
        )
    else:
        verdict = "hypothesis-not-supported"
        rationale = (
            f"neither hypothesis arm sits >= {material_gap} below the control arm's mean "
            f"operator kill rate (S={control})"
        )
    return {
        "verdict": verdict,
        "rationale": rationale,
        "material_gap": material_gap,
        "control_mean_kill_rate": control,
        "gap_below_control": gaps,
        "next_step": {
            "hypothesis-supported": (
                "open the separate, deliberately minimal INNER JOIN follow-up issue described in #64 "
                "(two tables, equi-join only, no aliases/outer joins/subqueries)"
            ),
            "hypothesis-not-supported": (
                "decline the JOIN expansion and keep iterating on trigger_complexity, hidden state, "
                "symptom invisibility and spec_span within the existing SQL surface"
            ),
            "insufficient-data": "run the calibration described in docs/gen2-operators.md before deciding",
        }[verdict],
    }


def calibrate(records: list[dict], min_trials: int, material_gap: float) -> dict:
    per_operator = tally(records)
    gen2 = {oid: e for oid, e in per_operator.items() if e["generation"] == "gen2"}
    gen1_sampled = {oid: e for oid, e in per_operator.items() if e["generation"] == "gen1" and e["trials"] > 0}
    undersampled = sorted(oid for oid, e in gen2.items() if e["trials"] < min_trials)
    families = {family: summarize_family(per_operator, family) for family in mutate.FAMILIES}
    gen2_rates = [e["kill_rate"] for e in gen2.values() if e["trials"] > 0]
    gen1_rates = [e["kill_rate"] for e in gen1_sampled.values()]
    return {
        "min_trials": min_trials,
        "total_trials": sum(e["trials"] for e in per_operator.values()),
        "families": families,
        "gen2_overall": {
            "operators": len(gen2),
            "operators_with_trials": len(gen2_rates),
            "below_ceiling": sum(1 for r in gen2_rates if r < 1.0),
            "below_ceiling_share": round(sum(1 for r in gen2_rates if r < 1.0) / len(gen2_rates), 4) if gen2_rates else None,
            "mean_operator_kill_rate": round(sum(gen2_rates) / len(gen2_rates), 4) if gen2_rates else None,
        },
        "gen1_baseline": {
            "operators_with_trials": len(gen1_rates),
            "mean_operator_kill_rate": round(sum(gen1_rates) / len(gen1_rates), 4) if gen1_rates else None,
        },
        "undersampled_operators": undersampled,
        "join_gate": decide_join_gate(families, undersampled, material_gap),
        "per_operator": per_operator,
    }


def _fmt(rate: float | None) -> str:
    return "     -" if rate is None else f"{rate * 100:5.1f}%"


def render(report: dict) -> str:
    lines = []
    lines.append(f"trials: {report['total_trials']}  (minimum per operator: {report['min_trials']})")
    lines.append("")
    lines.append("kill rate by operator family")
    lines.append(f"{'family':<8}{'operators':>10}{'trials':>8}{'pooled':>8}{'mean/op':>9}{'min':>8}{'max':>8}")
    for family, summary in report["families"].items():
        lines.append(
            f"{family:<8}{summary['operators']:>10}{summary['trials']:>8}"
            f"{_fmt(summary['pooled_kill_rate']):>8}{_fmt(summary['mean_operator_kill_rate']):>9}"
            f"{_fmt(summary['min_operator_kill_rate']):>8}{_fmt(summary['max_operator_kill_rate']):>8}"
            f"   {summary['label']}"
        )
    lines.append("")
    for family, summary in report["families"].items():
        lines.append(f"family {family} - {summary['label']}")
        for operator_id, rate in summary["per_operator"].items():
            entry = report["per_operator"][operator_id]
            lines.append(f"  {_fmt(rate)}  {operator_id}  ({entry['kills']}/{entry['trials']} trials)")
        lines.append("")
    overall = report["gen2_overall"]
    lines.append(
        f"Gen2 below the 100% ceiling: {overall['below_ceiling']}/{overall['operators_with_trials']} "
        f"sampled operators ({_fmt(overall['below_ceiling_share']).strip()})"
    )
    baseline = report["gen1_baseline"]
    if baseline["operators_with_trials"]:
        lines.append(
            f"Gen1 baseline over the same run: {_fmt(baseline['mean_operator_kill_rate']).strip()} "
            f"mean kill rate across {baseline['operators_with_trials']} operators"
        )
    if report["undersampled_operators"]:
        lines.append("")
        lines.append(f"under-sampled (< {report['min_trials']} trials): {', '.join(report['undersampled_operators'])}")
    gate = report["join_gate"]
    lines.append("")
    lines.append(f"JOIN gate: {gate['verdict']} - {gate['rationale']}")
    lines.append(f"  gap below control arm (percentage points): " + ", ".join(f"{k}={_fmt(v).strip()}" for k, v in gate["gap_below_control"].items()))
    lines.append(f"  next step: {gate['next_step']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="report Gen2 (#64) kill rates grouped by operator family")
    parser.add_argument("--trials", required=True, type=pathlib.Path, help="JSON array or JSON Lines file of trial records")
    parser.add_argument("--out", type=pathlib.Path, help="also write the full report as JSON here")
    parser.add_argument("--min-trials", type=int, default=5, help="minimum trials per Gen2 operator (#64: 5)")
    parser.add_argument("--material-gap", type=float, default=0.2, help="kill-rate gap below the control arm that counts as 'materially lower'")
    parser.add_argument("--strict", action="store_true", help="exit nonzero when the data is insufficient to decide the JOIN gate")
    args = parser.parse_args()

    try:
        records = load_trials(args.trials)
        report = calibrate(records, min_trials=args.min_trials, material_gap=args.material_gap)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"calibrate_gen2: {exc}", file=sys.stderr)
        return 2

    print(render(report))
    if args.out:
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(f"\nwrote {args.out}")
    if args.strict and report["join_gate"]["verdict"] == "insufficient-data":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
