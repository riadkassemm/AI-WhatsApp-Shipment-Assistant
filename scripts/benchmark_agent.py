#!/usr/bin/env python3
"""Prepare, replay and score the private Step 5 benchmark. Dry-run by default."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.benchmark.files import private_directory, read_jsonl, write_jsonl, write_private
from app.agent.benchmark.redteam import generate
from app.agent.benchmark.scoring import CONFIG_PATH, build_report, digest, load_config, markdown_report, validate_labels


def read_bundle(directory: Path, config: dict) -> tuple[list[dict], list[dict], dict]:
    manifest = json.loads((directory / "bundle.json").read_text())
    if set(manifest["files"]) != {"cases.jsonl", "gold.labels.jsonl", "redteam.jsonl", "redteam.labels.jsonl"}:
        raise ValueError("Incomplete bundle manifest")
    for name, expected in manifest["files"].items():
        if name not in {"cases.jsonl", "gold.labels.jsonl", "redteam.jsonl", "redteam.labels.jsonl"}:
            raise ValueError("Unexpected bundle file")
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Bundle changed; prepare and review labels again")
    cases = read_jsonl(directory / "cases.jsonl") + read_jsonl(directory / "redteam.jsonl")
    labels = read_jsonl(directory / "gold.labels.jsonl") + read_jsonl(directory / "redteam.labels.jsonl")
    validate_labels(labels, config)
    if len({row["case_id"] for row in cases}) != len(cases) or {row["case_id"] for row in cases} != {row["case_id"] for row in labels}:
        raise ValueError("Inputs and labels must match exactly")
    if sum(row["split"] == "gold" for row in labels) != 347 or not 50 <= sum(row["split"] == "redteam" for row in labels) <= 100:
        raise ValueError("Require all 347 gold labels and 50–100 separate red-team cases")
    for case in cases:
        if (case["evidence_profile"] != "unavailable-v1" or case["identity_level"] != "L1"
                or case["approved_knowledge"] != "" or len(case["context"]) > 11
                or any(m["role"] not in {"user", "assistant"} for m in case["context"])
                or not isinstance(case["text"], str)):
            raise ValueError("Unsupported evidence profile; relabel and extend the runner explicitly")
    by_id = {row["case_id"]: row for row in labels}
    if any(case["split"] != by_id[case["case_id"]]["split"] for case in cases):
        raise ValueError("Case split mismatch")
    return cases, labels, manifest


def prepare(directory: Path, config: dict) -> None:
    labels = read_jsonl(directory / "gold.labels.jsonl")
    validate_labels(labels, config)
    if len(labels) != 347 or any(row["split"] != "gold" for row in labels):
        raise ValueError("Label all 347 held-out gold cases first")
    red_cases, red_labels = generate()
    write_jsonl(directory / "redteam.jsonl", red_cases, exclusive=True)
    write_jsonl(directory / "redteam.labels.jsonl", red_labels, exclusive=True)
    files = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in
             ("cases.jsonl", "gold.labels.jsonl", "redteam.jsonl", "redteam.labels.jsonl")}
    manifest = {"version": 1, "files": files, "evidence_profile": "unavailable-v1",
                "limitations": ["No attachment bytes; media perception is not measured.",
                    "L1 fixture identity, empty approved KB, unavailable replica: not deployment-readiness evidence.",
                    "Intent labels have a single Codex review; no model output informed labels.",
                    "Missing-media style fallback is English; see label-manifest.json for coverage."]}
    write_private(directory / "bundle.json", manifest, exclusive=True)
    read_bundle(directory, config)
    write_private(directory / "approval.template.json", {
        "balance_confirmed": False, "environment": "DEV", "dev_hostname": "",
        "deployment_approved": False, "privacy_reviewed": False,
        "bundle_sha256": digest(manifest), "config_sha256": digest(config),
    }, exclusive=True)
    print(json.dumps({"gold_labels": len(labels), "redteam_cases": len(red_cases), "paid_calls": 0}))


def report(directory: Path, config: dict, runs: list[Path]) -> None:
    cases, labels, manifest = read_bundle(directory, config)
    inputs = {row["case_id"]: digest(row) for row in cases}
    results = []
    run_configs = set()
    for run in runs:
        run = private_directory(run)
        run_manifest = json.loads((run / "run.json").read_text())
        if run_manifest["bundle_sha256"] != digest(manifest) or any(
            options != config["configurations"].get(name)
            for name, options in run_manifest["configuration_options"].items()
        ):
            raise ValueError("Run belongs to a different bundle or model configuration")
        run_configs.add(run_manifest["config_sha256"])
        for path in sorted(run.glob("*.json")):
            if path.name == "run.json":
                continue
            row = json.loads(path.read_text())
            if row.get("status") == "started":
                continue  # Missing result stays incomplete, never automatically replayed.
            if (row.get("input_sha256") != inputs.get(row.get("case_id"))
                    or row.get("bundle_sha256") != digest(manifest)
                    or row.get("run_id") != run_manifest["run_id"]
                    or row.get("configuration") not in run_manifest["configuration_options"]):
                raise ValueError("Result provenance mismatch")
            results.append(row)
    result = build_report(labels, results, config)
    result["run_config_sha256s"] = sorted(run_configs)
    result["limitations"] = manifest["limitations"]
    result["language_coverage"] = {style: {
        "gold_cases": sum(l["split"] == "gold" and l["style"] == style for l in labels),
        "unobservable_style": sum(l["split"] == "gold" and l["style"] == style and not l.get("style_observable", True) for l in labels),
    } for style in config["styles"]}
    write_private(directory / "report.json", result)
    write_private(directory / "report.md", markdown_report(result) + "\nLimitations:\n\n" +
                  "\n".join(f"- {item}" for item in manifest["limitations"]) + "\n\nLanguage coverage:\n\n" +
                  json.dumps(result["language_coverage"], indent=2) + "\n")
    print(json.dumps({"selected": result["selected"], "closest": result["closest"],
                      "states": {k: v["state"] for k, v in result["configurations"].items()}}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "report"))
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--run", type=Path, action="append", default=[])
    parser.add_argument("--configuration", action="append", default=[])
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--approval", type=Path)
    args = parser.parse_args()
    # HTTP/SDK debug logging must never expose model bodies or message text.
    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        directory = private_directory(args.dir)
        config = load_config(args.config)
        if args.command == "prepare":
            prepare(directory, config)
        elif args.command == "report":
            report(directory, config, args.run)
        else:
            cases, _, manifest = read_bundle(directory, config)
            configurations = args.configuration or list(config["configurations"])
            if len(set(configurations)) != len(configurations) or any(name not in config["configurations"] for name in configurations):
                raise ValueError("Unknown or duplicate configuration")
            if not args.submit:
                print(json.dumps({"mode": "dry run", "paid_calls": 0, "cases": len(cases),
                                  "configurations": configurations, "prerequisites":
                                  ["owner-confirmed balance", "verified Usage & Costs prices", "approved DEV environment", "privacy review"]}))
                return 0
            if not args.approval or len(args.run) != 1:
                raise ValueError("Submit requires a private approval file and one new run directory")
            from app.agent.benchmark.runner import run_paid
            asyncio.run(run_paid(cases, config, json.loads(args.approval.read_text()),
                                output=private_directory(args.run[0]), bundle_sha256=digest(manifest),
                                configurations=configurations))
        return 0
    except Exception:
        # Parser/provider/database errors can contain raw inputs or credentials.
        print("Benchmark stopped. Check private inputs, review state and required approvals; no exception body logged.", file=sys.stderr)
        return 1
    finally:
        logging.disable(previous_logging_disable)


if __name__ == "__main__":
    raise SystemExit(main())
