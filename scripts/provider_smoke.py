"""Opt-in live model smoke test on invented rows; never run automatically.

This exercises settings interpretation, live planning, offline replay and
recipient verification. Test-operator accept/decline inputs are automated;
they are not the owner's acceptance of real data or a scientific review.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import cli  # noqa: E402
from engine.artifacts.consumer import verify_release  # noqa: E402
from engine.artifacts.hashing import sha256_bytes  # noqa: E402

REQUEST = (
    "Create exactly 100 synthetic rows using fitted_gaussian_disclosed with seed 42 "
    "for a software demo. Keep the supported scalar output."
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", choices=["agy_cli", "meta_cli"], required=True)
    parser.add_argument("--family", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    inputs = args.out / "inputs"
    inputs.mkdir()
    source = inputs / "orders-tabular.csv"
    source.write_text(
        "id,value,label\n"
        + "".join(f"{i},{10.0 + i % 5},{'a' if i % 2 else 'b'}\n" for i in range(20)),
        encoding="utf-8",
    )
    (inputs / "orders-tabular-source.json").write_text(
        json.dumps(
            {
                "dataset_name": "INVENTED software smoke fixture",
                "licence": "CC0-1.0",
                "attribution": "Invented test rows, not observations or customer data.",
            }
        ),
        encoding="utf-8",
    )
    (inputs / "tabular-thresholds.json").write_text(
        '{"mean_relative_tolerance": 0.25}\n', encoding="utf-8"
    )
    roles = [
        "--role-adapter",
        f"planner={args.adapter}",
        "--role-family",
        f"planner={args.family}",
        "--role-model",
        f"planner={args.model}",
        "--role-effort",
        f"planner={args.effort}",
    ]
    receipt = {
        "status": "failed",
        "scope": "live model, invented scalar fixture only",
        "adapter": args.adapter,
        "family": args.family,
        "requested_model": args.model,
        "effort": args.effort,
        "request": REQUEST,
        "source_sha256": sha256_bytes(source.read_bytes()),
        "software_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "operator_decisions": "automated engineering-test inputs; no owner/scientific approval",
        "stages": {},
    }
    started = time.monotonic()

    def call(name, argv, decision="accept", expected=0):
        log = io.StringIO()
        before = time.monotonic()
        with (
            contextlib.redirect_stdout(log),
            contextlib.redirect_stderr(log),
            patch(
                "sys.stdin",
                io.StringIO(decision + "\nAutomated isolated software trial only.\n"),
            ),
        ):
            code = cli.main(argv)
        (args.out / f"{name}.txt").write_text(log.getvalue(), encoding="utf-8")
        receipt["stages"][name] = {
            "exit_code": code,
            "wall_seconds": round(time.monotonic() - before, 3),
        }
        if code != expected:
            raise ValueError(f"{name} failed; inspect {name}.txt")

    def bundle(name):
        matches = list((args.out / name / "runs").glob("*/release.zip"))
        if len(matches) != 1:
            raise ValueError(f"{name}: expected one release, got {len(matches)}")
        release = matches[0]
        verified = verify_release(release)
        with zipfile.ZipFile(release) as archive:
            data = archive.read("data.csv")
            rows = list(csv.DictReader(io.StringIO(data.decode())))
            report = json.loads(archive.read("validation-report.json"))
        if (
            len(rows) != 100
            or report["verdict"] != "pass"
            or any(x["status"] != "pass" for x in report["results"])
        ):
            raise ValueError(f"{name}: wrong row count or non-passing check")
        events = [
            json.loads(x)
            for x in (release.parent / "run.jsonl").read_text().splitlines()
        ]
        receipt["stages"][name].update(
            rows=len(rows),
            data_sha256=sha256_bytes(data),
            checks=report["results"],
            consumer_verified=True,
            release_sha256=verified["zip_sha256"],
            model_events=[
                e for e in events if str(e.get("stage", "")).startswith("model")
            ],
        )
        invocations = [e for e in events if e["stage"] == "model_invocation"]
        if name.startswith("offline") and any(
            e["stage"] == "model_call_started" for e in events
        ):
            raise ValueError("offline replay started a model call")
        if name == "live-planner" and (
            not invocations
            or any(
                e["details"].get("invocation", {}).get("returned_model") != args.model
                for e in invocations
            )
        ):
            raise ValueError("fresh planner did not report the selected identity")
        return sha256_bytes(data)

    try:
        draft = args.out / "draft"
        call(
            "prepare",
            [
                "prepare",
                str(source),
                "--profile",
                "tabular",
                "--request",
                REQUEST,
                "--max-attempts",
                "2",
                "--model-policy",
                "hosted",
                "--out",
                str(draft),
                *roles,
            ],
        )
        plan = json.loads((draft / "plan.json").read_text())
        if plan != {"seed": 42, "n_rows": 100, "strategy": "fitted_gaussian_disclosed"}:
            raise ValueError(
                "request interpretation did not preserve exact seed/rows/strategy"
            )
        receipt["request_receipt"] = json.loads((draft / "request.json").read_text())
        if receipt["request_receipt"]["observed_model"] != args.model:
            raise ValueError("CLI did not report the requested model identity")
        common = [
            "run",
            str(source),
            "--profile",
            "tabular",
            "--max-attempts",
            "2",
            "--spec",
            str(draft / "spec.json"),
            *roles,
        ]
        for name in ["offline-first", "offline-replay"]:
            call(
                name,
                [
                    *common,
                    "--model-policy",
                    "offline",
                    "--plan",
                    str(draft / "plan.json"),
                    "--out",
                    str(args.out / name),
                ],
            )
        if bundle("offline-first") != bundle("offline-replay"):
            raise ValueError("saved-plan data bytes did not reproduce")
        call("live-planner", [*common, "--out", str(args.out / "live-planner")])
        bundle("live-planner")
        call(
            "decline",
            [
                *common,
                "--model-policy",
                "offline",
                "--plan",
                str(draft / "plan.json"),
                "--out",
                str(args.out / "decline"),
            ],
            decision="reject",
            expected=2,
        )
        if list((args.out / "decline").rglob("release.zip")):
            raise ValueError("declined run created a release")
        if sha256_bytes(source.read_bytes()) != receipt["source_sha256"]:
            raise ValueError("source changed")
        receipt["status"] = "pass"
    except Exception as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        receipt["wall_seconds"] = round(time.monotonic() - started, 3)
        (args.out / "receipt.json").write_text(
            json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {
                k: receipt.get(k)
                for k in ["status", "requested_model", "wall_seconds", "error"]
            }
        )
    )
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
