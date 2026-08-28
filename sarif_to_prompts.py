import json, sys, re
from pathlib import Path

def load_rules(run):
    rules = {}
    for r in run["tool"]["driver"].get("rules", []):
        rules[r["id"]] = r
    return rules

def get_text(field):
    if isinstance(field, dict):
        return field.get("text", "")
    return field or ""

def format_flow(result):
    flows = result.get("codeFlows")
    if not flows:
        return ""
    steps = flows[0]["threadFlows"][0]["locations"]
    lines = ["", "**Data flow (source → sink):**"]
    for i, step in enumerate(steps, 1):
        loc = step["location"]
        phys = loc["physicalLocation"]
        uri = phys["artifactLocation"]["uri"]
        line = phys["region"]["startLine"]
        msg = get_text(loc.get("message", {}))
        lines.append(f"{i}. `{uri}:{line}` — {msg}")
    return "\n".join(lines)

def make_prompt(result, rule, idx):
    loc = result["locations"][0]["physicalLocation"]
    uri = loc["artifactLocation"]["uri"]
    region = loc.get("region", {})
    start = region.get("startLine")
    end = region.get("endLine", start)
    snippet = get_text(region.get("snippet", {}))
    msg = get_text(result.get("message", {}))
    severity = rule.get("properties", {}).get("problem.severity", "unspecified")
    cwe = [t for t in rule.get("properties", {}).get("tags", []) if "cwe" in t.lower()]
    help_text = get_text(rule.get("fullDescription", {})) or get_text(rule.get("shortDescription", {}))

    prompt = f"""# CodeQL finding {idx}: {rule.get('id')}

**File:** `{uri}` (lines {start}-{end})
**Severity:** {severity}
**CWE:** {', '.join(cwe) if cwe else 'n/a'}

**Rule description:** {help_text}

**Specific alert message:** {msg}

**Code at location:**
```python
{snippet}
```
{format_flow(result)}

**Task:** Verify first then Fix this specific issue with the smallest change that removes the
vulnerability/defect, without changing unrelated behavior. Explain the fix
in one or two sentences before showing the diff.
"""
    return prompt

def main(sarif_path, out_dir):
    data = json.loads(Path(sarif_path).read_text())
    out = Path(out_dir)
    out.mkdir(exist_ok=True)
    counter = 0
    for run in data["runs"]:
        rules = load_rules(run)
        for result in run.get("results", []):
            counter += 1
            rule = rules.get(result["ruleId"], {})
            prompt = make_prompt(result, rule, counter)
            fname = out / f"issue_{counter:03d}_{re.sub(r'[^a-zA-Z0-9]+', '_', result['ruleId'])[:40]}.md"
            fname.write_text(prompt)
    print(f"Wrote {counter} prompt files to {out}/")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "codeql_prompts")