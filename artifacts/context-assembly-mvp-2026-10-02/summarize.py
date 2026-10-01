"""Count the latest result of each targeted test, preserving failed batches."""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

root = Path(__file__).parent
latest = {}
batches = []
for report in sorted(root.glob("*.xml"), key=lambda path: path.stat().st_mtime_ns):
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    for case in ET.parse(report).iter("testcase"):
        status = (
            "failed"
            if case.find("failure") is not None or case.find("error") is not None
            else "skipped"
            if case.find("skipped") is not None
            else "passed"
        )
        counts[status] += 1
        latest[f"{case.attrib['classname']}::{case.attrib['name']}"] = {
            "status": status,
            "report": report.name,
        }
    batches.append({"report": report.name, **counts})
result = {
    "scope": "targeted tests only; latest result per distinct node",
    "totals": {
        status: sum(item["status"] == status for item in latest.values())
        for status in ("passed", "failed", "skipped")
    },
    "batches": batches,
    "nodes": latest,
}
(root / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result["totals"]))
