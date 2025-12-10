#!/usr/bin/env python3
"""
Simple Health Score calculator (starter).
Outputs JSON summary and prints final score.
"""
import os, sys, json, subprocess, xml.etree.ElementTree as ET, requests, math, glob, re
from pathlib import Path

REPO = os.getenv("GITHUB_REPOSITORY")
TOKEN = os.getenv("GITHUB_TOKEN")
COVERAGE_PATH = os.getenv("COVERAGE_PATH")  # optional

# --- weights
WEIGHTS = {
    "structure": 0.15,
    "tests": 0.25,
    "dead_code": 0.10,
    "security": 0.20,
    "docs": 0.15,
    "ci": 0.15
}

def score_structure():
    root = Path('.')
    required = ["README.md", "LICENSE", ".github", "src", "setup.py", "pyproject.toml"]
    found = 0
    for f in required:
        if (root / f).exists():
            found += 1
    value = found / len(required)
    return value, {"found_count": found, "required": len(required)}

def score_tests():
    # Try coverage XML (coverage.py) at given path or common location
    cov_files = []
    if COVERAGE_PATH:
        cov_files.append(Path(COVERAGE_PATH))
    cov_files.extend([Path("coverage.xml"), Path("htmlcov/coverage.xml")])
    coverage_pct = None
    for p in cov_files:
        if p.exists():
            try:
                tree = ET.parse(p)
                root = tree.getroot()
                # coverage.py xml: <coverage line-rate="0.8" /> or <coverage ... lines-valid...>
                if "line-rate" in root.attrib:
                    coverage_pct = float(root.attrib["line-rate"]) * 100
                else:
                    cov = root.find(".//coverage")
                    if cov is not None and "line-rate" in cov.attrib:
                        coverage_pct = float(cov.attrib["line-rate"]) * 100
                break
            except Exception:
                pass
    # fallback: run pytest --maxfail=1 -q and coverage run if tests present (best-effort)
    if coverage_pct is None:
        # check for tests dir
        if Path("tests").exists() or any(glob.glob("test_*") + glob.glob("*/tests/*")):
            try:
                subprocess.run(["coverage", "run", "-m", "pytest", "-q"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["coverage", "xml", "-o", "coverage.xml"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                tree = ET.parse("coverage.xml")
                coverage_pct = float(tree.getroot().attrib.get("line-rate", "0"))*100
            except Exception:
                coverage_pct = 0.0
        else:
            coverage_pct = 0.0
    value = min(max(coverage_pct/100.0, 0.0), 1.0)
    return value, {"coverage_pct": round(coverage_pct,2)}

def score_dead_code():
    # Try vulture if installed
    try:
        out = subprocess.check_output(["vulture", "." , "--min-confidence", "0"], stderr=subprocess.DEVNULL).decode()
        # vulture lists items - best-effort: count lines flagged vs total python lines
        flagged = len([l for l in out.splitlines() if l.strip() and ":" in l])
        py_files = list(glob.glob("**/*.py", recursive=True))
        total_lines = 0
        for f in py_files:
            try:
                total_lines += sum(1 for _ in open(f, 'r', encoding='utf8', errors='ignore'))
            except:
                pass
        dead_ratio = (flagged * 10) / max(total_lines,1)  # heuristic: one finding ~ 10 lines
        dead_ratio = min(dead_ratio, 1.0)
    except Exception:
        dead_ratio = 0.0
    value = 1.0 - dead_ratio
    return value, {"dead_ratio": round(dead_ratio,3)}

def score_security():
    # Try bandit if installed
    penalty = 0.0
    try:
        out = subprocess.check_output(["bandit", "-r", "." , "-f", "json"], stderr=subprocess.DEVNULL)
        data = json.loads(out)
        for issue in data.get("results", []):
            sev = issue.get("issue_severity", "").lower()
            if sev == "low":
                penalty += 0.5
            elif sev == "medium":
                penalty += 2.0
            elif sev == "high":
                penalty += 5.0
    except Exception:
        # if bandit not available, do a light heuristic: look for use of exec/eval
        try:
            code = Path('.').read_text(encoding='utf8', errors='ignore')
            if "eval(" in code or "exec(" in code:
                penalty += 2.0
        except:
            pass
    max_penalty = 10.0
    value = max(0.0, 1.0 - (penalty / max_penalty))
    return value, {"penalty": penalty}

def score_docs():
    # README presence + basic sections
    readme = Path("README.md")
    score = 0.0
    details = {}
    if readme.exists():
        text = readme.read_text(encoding='utf8', errors='ignore').lower()
        for item in ["installation", "usage", "license", "contributing"]:
            details[item] = item in text
            if details[item]:
                score += 1
        score = score / 4.0
    else:
        score = 0.0
        details = {}
    # docstrings heuristic: fraction of py files with triple quotes at top
    py_files = list(glob.glob("**/*.py", recursive=True))
    if py_files:
        has_doc = sum(1 for f in py_files if open(f,'r',errors='ignore').read(200).count('"""')>0 or open(f,'r',errors='ignore').read(200).count("'''")>0)
        doc_ratio = has_doc / len(py_files)
    else:
        doc_ratio = 0
    final = 0.6 * score + 0.4 * doc_ratio
    return final, {"readme_sections": details, "doc_ratio": round(doc_ratio,2)}

def score_ci():
    # Use GitHub Actions API to fetch runs for default workflow on repo
    if not TOKEN or not REPO:
        return 0.0, {"note":"no token/repo"}
    owner, repo = REPO.split("/")
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs"
    headers = {"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github+json"}
    params = {"per_page": 30}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        data = r.json()
        runs = data.get("workflow_runs", [])
        if not runs:
            return 0.0, {"total_runs":0}
        total = len(runs)
        success = sum(1 for run in runs if run.get("conclusion") == "success")
        value = success / total
        return value, {"total_runs": total, "success": success}
    except Exception as e:
        return 0.0, {"error": str(e)}

def combine(scores):
    total = 0.0
    for k,v in WEIGHTS.items():
        total += v * scores[k][0]
    return total * 100.0

def main():
    out = {}
    out['structure'] = score_structure()
    out['tests'] = score_tests()
    out['dead_code'] = score_dead_code()
    out['security'] = score_security()
    out['docs'] = score_docs()
    out['ci'] = score_ci()
    score = combine(out)
    out['final_score'] = round(score,2)
    print(json.dumps(out, indent=2))
    # write summary file for the composite action to pick up
    with open("health_score_result.json", "w") as f:
        json.dump(out, f)
    # print a human-friendly summary
    print(f"\nRepository Health Score: {out['final_score']}/100")
    sys.exit(0)

if __name__ == "__main__":
    main()
