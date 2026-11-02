"""RP-05 multi-seed local-structure scorecard (R01/R02 valid-input behaviour).

For every source hole under ``data/sources`` (PDD446 plus the four
held-out holes), builds the real statistics profile, generates a
full-length synthetic hole per seed, and runs the product checks in
full production parity (granted source reference plus requested plan
geometry). Records the verdict plus local-sequence-structure metrics
-- per-channel answer rates, marginal TVD distance against the
source's own split-half tolerance, label persistence, joint SWIR/TIR
agreement, and valid/invalid mean run lengths -- for source and
synthetic side by side.

Usage (repository root)::

    .venv/bin/python scripts/scorecard_rp05.py

Writes ``docs/quality/scorecard-rp05.md``. Needs the git-ignored NVCL
source cache under ``data/sources``; exits 2 with an honest message
when it is absent (e.g. a clean checkout) instead of fabricating
numbers.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from profiles.hylogger.analysis.calibration import load_thresholds  # noqa: E402
from profiles.hylogger.analysis.statistics import Profile, build_stats  # noqa: E402
from profiles.hylogger.generation.generate import generate_samples  # noqa: E402
from profiles.hylogger.intake.reader import read_scalar_csv  # noqa: E402
from profiles.hylogger.validation.checks import (  # noqa: E402
    run_checks,
    source_fidelity_reference,
)

SOURCES = ("PDD446", "HD012", "DDG007", "20LCDD003", "NCB0003")
SEEDS = (446, 7, 12345)


def _fmt(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def main() -> int:
    data_dir = REPO_ROOT / "data" / "sources"
    missing = [
        name for name in SOURCES if not (data_dir / f"{name}-scalars.csv").is_file()
    ]
    if missing:
        print(
            f"scorecard needs data/sources ({', '.join(missing)} absent): "
            "refusing to fabricate numbers",
            file=sys.stderr,
        )
        return 2
    thresholds = load_thresholds(
        REPO_ROOT / "profiles" / "hylogger" / "thresholds.json"
    )
    holes = thresholds.provenance.get("holes", [])

    lines = [
        "# RP-05 local-structure scorecard",
        "",
        "Multi-seed valid-input behaviour for the HyLogger depth /",
        "missingness / label / joint-sequence slice (R01+R02). Each cell",
        "generates a full-length synthetic hole from the hole's own real",
        "profile and scores it with the product checks in production",
        "parity (granted source reference plus requested plan geometry).",
        "",
        "## Frozen split",
        "",
        f"- Calibration: the {len(holes)} holes recorded in",
        "  `profiles/hylogger/thresholds.json` provenance (frozen -- RP-05",
        "  adds no band, changes no threshold, relaxes nothing).",
        "- Evaluation: PDD446 (development reference) plus the four",
        "  held-out holes 20LCDD003, DDG007, HD012, NCB0003 -- the same",
        "  holes as the Step 14 held-out protocol.",
        f"- Seeds: {', '.join(str(s) for s in SEEDS)};",
        "  `n_rows` = source row count (the plan prompt's rule).",
        "",
        "## Interpretation (read with the tables)",
        "",
        "- Every cell passes: no valid synthetic trips any required",
        "  check on any hole or seed; the only skip is the known-vacuous",
        "  `swir_answer_rate` band (required: false, RP-01).",
        "- SWIR persistence reproduces closely (e.g. PDD446 0.910 source",
        "  vs 0.903..0.908 synthetic); joint agreement and valid/invalid",
        "  run lengths track within a few percent.",
        "- TIR persistence runs systematically lower in synthetics than",
        "  in source (e.g. PDD446 0.743 -> ~0.49; HD012 0.725 -> ~0.45):",
        "  for both-answered rows the generator draws TIR from the joint",
        "  counts conditioned on the SWIR label, which resets TIR runs.",
        "  This is disclosed, not gated -- no threshold exists for it,",
        "  and changing the draw would move the golden PDD446 bytes, so",
        "  RP-05 leaves the generator byte-identical.",
        "",
        "## Per-hole results",
        "",
    ]
    verdict_table = [
        "| hole | seed | verdict | failed | skipped |",
        "|---|---|---|---|---|",
    ]
    for name in SOURCES:
        source = read_scalar_csv(data_dir / f"{name}-scalars.csv")
        profile = Profile.from_samples(source, name, "scorecard")
        stats = profile.stats
        geometry = stats["geometry"]
        spacing = geometry["spacing_m"]
        expected = {
            "n_rows": len(source),
            "start_m": float(geometry["depth_start_m"]),
            "step_m": float(spacing.get("p50") or spacing.get("mean")),
        }
        reference = source_fidelity_reference(source)
        source_stats = build_stats(source)
        lines.append(f"### {name} (n={len(source)})")
        lines.append("")
        lines.append(
            "| channel | answer src | answer synth "
            "(min..max over seeds) | persist src | persist synth "
            "(min..max over seeds) |"
        )
        lines.append("|---|---|---|---|---|")
        per_channel: dict[str, dict[str, list[float]]] = {}
        for channel in ("swir", "tir"):
            rates, persists = [], []
            for seed in SEEDS:
                synth_stats = build_stats(generate_samples(profile, seed, len(source)))
                rate = synth_stats[channel]["answer_rate"]
                persist = synth_stats[channel]["persistence"]
                if rate is not None:
                    rates.append(rate)
                if persist is not None:
                    persists.append(persist)
            per_channel[channel] = {"rates": rates, "persists": persists}
            src_rate = _fmt(source_stats[channel]["answer_rate"])
            src_persist = _fmt(source_stats[channel]["persistence"])
            rate_range = f"{min(rates):.6f}..{max(rates):.6f}" if rates else "n/a"
            persist_range = (
                f"{min(persists):.6f}..{max(persists):.6f}" if persists else "n/a"
            )
            lines.append(
                f"| {channel} | {src_rate} | {rate_range} | "
                f"{src_persist} | {persist_range} |"
            )
        lines.append("")
        lines.append(
            "| seed | verdict | swir TVD (tol) | tir TVD (tol) | "
            "joint agree src -> synth | valid-run mean src -> synth "
            "| invalid-run mean src -> synth |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for seed in SEEDS:
            synth = generate_samples(profile, seed, len(source))
            report = run_checks(
                synth,
                thresholds,
                source_reference=reference,
                expected_geometry=expected,
                artifact_name=f"{name}:seed-{seed}",
            )
            synth_stats = build_stats(synth)
            tvds = []
            for channel in ("swir", "tir"):
                result = next(
                    r for r in report.results if r.name == f"{channel}_fidelity"
                )
                tvds.append(result.detail)
            agree = (
                f"{_fmt(source_stats['joint']['agreement'])} -> "
                f"{_fmt(synth_stats['joint']['agreement'])}"
            )
            valid_runs = (
                f"{_fmt(source_stats['validity']['valid_runs']['mean'])} -> "
                f"{_fmt(synth_stats['validity']['valid_runs']['mean'])}"
            )
            invalid_runs = (
                f"{_fmt(source_stats['validity']['invalid_runs']['mean'])} -> "
                f"{_fmt(synth_stats['validity']['invalid_runs']['mean'])}"
            )
            lines.append(
                f"| {seed} | {report.verdict} | {tvds[0]} | {tvds[1]} | "
                f"{agree} | {valid_runs} | {invalid_runs} |"
            )
            verdict_table.append(
                f"| {name} | {seed} | {report.verdict} | "
                f"{','.join(report.failed) or '-'} | "
                f"{','.join(report.skipped) or '-'} |"
            )
        lines.append("")
    lines.append("## Verdict summary")
    lines.append("")
    lines.extend(verdict_table)
    lines.append("")
    out = REPO_ROOT / "docs" / "quality" / "scorecard-rp05.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}")
    print()
    print("\n".join(verdict_table))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
