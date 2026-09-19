#!/usr/bin/env python3
"""
Convert VPC discovery Lambda output (scanneddata.json) into a static-data
Python variable, matching the format of
app/utils/static_data/vpc_resource_data/aspora.py (CORE_STAGE, CORE_PROD, ...).

Handles all shapes the Lambda output comes in:
  - the full invoke response: {"statusCode": 200, "body": "{...}"}
  - a JSON-encoded string of the canvas response (double-encoded)
  - the canvas response dict itself

Usage:
  python3 scripts/convert_scanned_data.py scanneddata.json --var-name CORE_QA
  python3 scripts/convert_scanned_data.py scanneddata.json --var-name CORE_QA \
      --append app/utils/static_data/vpc_resource_data/aspora.py
"""
import argparse
import ast
import json
import sys

INDENT = "  "


def load_canvas_data(path: str) -> dict:
    """Unwrap the Lambda output into the plain canvas response dict."""
    with open(path) as f:
        data = json.load(f)

    # Double-encoded: file is a JSON string containing JSON
    if isinstance(data, str):
        data = json.loads(data)

    # Full Lambda invoke response: unwrap "body"
    if isinstance(data, dict) and "body" in data and "statusCode" in data:
        body = data["body"]
        data = json.loads(body) if isinstance(body, str) else body

    if not isinstance(data, dict) or "geoLocations" not in data:
        raise ValueError(
            "Input does not look like a canvas response "
            f"(top-level keys: {list(data)[:5] if isinstance(data, dict) else type(data)})"
        )
    return data


def to_python_literal(value, level: int = 0) -> str:
    """Render a JSON-compatible value as pretty Python source."""
    pad = INDENT * level
    child_pad = INDENT * (level + 1)

    if isinstance(value, dict):
        if not value:
            return "{}"
        items = [
            f"{child_pad}{json.dumps(str(k))}: {to_python_literal(v, level + 1)}"
            for k, v in value.items()
        ]
        return "{\n" + ",\n".join(items) + f",\n{pad}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        items = [f"{child_pad}{to_python_literal(v, level + 1)}" for v in value]
        return "[\n" + ",\n".join(items) + f",\n{pad}]"
    if value is True:
        return "True"
    if value is False:
        return "False"
    if value is None:
        return "None"
    if isinstance(value, str):
        return json.dumps(value)  # double-quoted, escapes handled
    return repr(value)  # int / float


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="scanned data JSON file (Lambda output)")
    parser.add_argument("--var-name", default="CORE_QA", help="Python variable name (default: CORE_QA)")
    parser.add_argument("--append", metavar="FILE", help="append the block to FILE instead of printing")
    args = parser.parse_args()

    data = load_canvas_data(args.input)
    block = f"{args.var_name} = {to_python_literal(data)}\n"

    # Sanity check: generated source must parse and round-trip to the same data
    parsed = ast.literal_eval(block.split("=", 1)[1].strip())
    if parsed != data:
        raise AssertionError("Round-trip mismatch — generated literal does not equal source data")

    summary = ", ".join(
        f"{len(v)} {k}" for k, v in data.items() if isinstance(v, list) and v
    )

    if args.append:
        with open(args.append) as f:
            existing = f.read()
        if f"{args.var_name} =" in existing or f"{args.var_name}=" in existing:
            sys.exit(f"error: {args.var_name} already exists in {args.append} — pick another --var-name")
        with open(args.append, "a") as f:
            if not existing.endswith("\n"):
                f.write("\n")
            f.write(f"\n\n# {args.var_name.lower().replace('_', ' ')} — generated from {args.input}\n")
            f.write(block)
        print(f"Appended {args.var_name} to {args.append} ({summary})")
    else:
        print(block)
        print(f"# {summary}", file=sys.stderr)


if __name__ == "__main__":
    main()
