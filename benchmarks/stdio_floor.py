"""Stdio benchmark floor server."""

from __future__ import annotations

import json
import sys

ANSWER = {"jsonrpc": "2.0", "result": {"structuredContent": {"result": 5}, "content": []}}

if __name__ == "__main__":
    for line in sys.stdin:
        if not line.strip():
            continue
        sys.stdout.write(json.dumps({**ANSWER, "id": json.loads(line).get("id")}) + "\n")
        sys.stdout.flush()
