"""Summarize overlapping targeted batches without double-counting test nodes."""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

root = Path(__file__).parent
latest = {}
batches = []
retired = "test_native_mixed_write_preserves_read_before_and_after"
for report in sorted(root.glob("*.xml"), key=lambda path: path.stat().st_mtime_ns):
    cases = list(ET.parse(report).iter("testcase"))
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    for case in cases:
        status = ("failed" if case.find("failure") is not None or case.find("error") is not None
                  else "skipped" if case.find("skipped") is not None else "passed")
        counts[status] += 1
        if case.attrib["name"] != retired:
            latest[f"{case.attrib['classname']}::{case.attrib['name']}"] = {
                "status": status, "report": report.name,
            }
    batches.append({"report": report.name, **counts})
result = {
    "scope": "targeted tests only; overlapping nodes counted once by latest result",
    "retired_assertion": retired,
    "totals": {status: sum(row["status"] == status for row in latest.values())
               for status in ("passed", "failed", "skipped")},
    "batches": batches,
    "nodes": latest,
}
(root / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result["totals"]))
for report in root.glob("*.xml"):
    for case in ET.parse(report).iter("testcase"):
        skipped = case.find("skipped")
        if skipped is not None:
            print(f"skip: {case.attrib['name']}: {skipped.attrib.get('message', '')}")
