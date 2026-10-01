"""Latest targeted result per node; preserve failed batches and overlapping runs."""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

root = Path(__file__).parent
reports = []
for path in root.glob("*.xml"):
    tree = ET.parse(path)
    started = next(tree.iter("testsuite")).get("timestamp", "")
    reports.append((started, path, tree))
latest, batches = {}, []
for started, path, tree in sorted(reports, key=lambda entry: (entry[0], entry[1].name)):
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    for case in tree.iter("testcase"):
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
            "report": path.name,
        }
    batches.append({"report": path.name, "started_at": started, **counts})
result = {
    "scope": "targeted only; distinct nodes, latest run by JUnit start time",
    "totals": {
        status: sum(item["status"] == status for item in latest.values())
        for status in ("passed", "failed", "skipped")
    },
    "batches": batches,
    "nodes": latest,
}
(root / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result["totals"]))
print(json.dumps(batches, indent=2))
