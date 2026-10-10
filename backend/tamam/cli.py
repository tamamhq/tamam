"""The `tamam` command: check your setup, then run a test and get an evidence-backed verdict.

    tamam doctor                                   # is this Mac ready?
    tamam run --fixture shoply --case checkout     # a passing run on the demo app
    tamam run --fixture shoply --case checkout --buggy   # the same test catching a bug
    tamam run --app MyApp.app --bundle com.me.app "Open Settings and verify Dark mode is on"

No server, database or dashboard: `run` goes through the same harness and verifier the
dashboard uses, so the verdict is the real one (never just the agent's word).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "ios" / "build"
FIXTURE_BUNDLE = "dev.tamam.shoply"
PREFERRED_DEVICE = "iPhone 16 Pro"   # what the demo suite is measured on, when installed
EXIT = {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2, "USAGE": 3}


class UsageError(Exception):
    """A setup or command-line problem: exit 3, never confused with a FAIL (1)."""


def _build_hint() -> str:
    script = FIXTURE_DIR.parent / "build.sh"
    try:
        return os.path.relpath(script)
    except ValueError:
        return str(script)


def available_iphones() -> list[str]:
    """iPhone simulators this Mac can run, newest iOS runtime first."""
    code, txt = _run(["xcrun", "simctl", "list", "devices", "available", "-j"])
    if code != 0:
        return []
    try:
        runtimes = json.loads(txt)["devices"]
    except (ValueError, KeyError):
        return []
    def version(rt: str) -> tuple[int, ...]:
        return tuple(int(n) for n in rt.rsplit("iOS-", 1)[-1].split("-") if n.isdigit())
    names: list[str] = []
    for rt in sorted((r for r in runtimes if "iOS" in r), key=version, reverse=True):
        names += [d["name"] for d in runtimes[rt] if d["name"].startswith("iPhone") and d["name"] not in names]
    return names


def pick_device(requested: str | None, available: list[str]) -> str:
    if requested:
        if available and requested not in available:
            raise UsageError(f"No {requested!r} simulator on this Mac. Try one of: {', '.join(available)}")
        return requested
    if PREFERRED_DEVICE in available or not available:
        return PREFERRED_DEVICE
    return available[0]


# ── doctor ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""


def _run(cmd: list[str], timeout: float = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def checks() -> list[Check]:
    from tamam.config import get_settings
    s = get_settings()
    out: list[Check] = []
    mac = platform.system() == "Darwin"
    out.append(Check("macOS", mac, platform.platform(), "Tamam drives the iOS Simulator, which needs a Mac."))
    arm = platform.machine() == "arm64"
    out.append(Check("Apple Silicon", arm, platform.machine(),
                     "The demo app is built for arm64 simulators; Intel Macs are not supported yet."))
    code, txt = _run(["xcodebuild", "-version"])
    out.append(Check("Xcode", code == 0, txt.splitlines()[0] if code == 0 and txt else "",
                     "Install Xcode from the App Store, then: sudo xcode-select -s /Applications/Xcode.app"))
    code, txt = _run(["xcrun", "simctl", "list", "runtimes"])
    has_ios = code == 0 and "iOS" in txt
    out.append(Check("iOS Simulator runtime", has_ios, "",
                     "Xcode > Settings > Components: install an iOS Simulator runtime."))
    appium = shutil.which("appium")
    out.append(Check("Appium", appium is not None, appium or "",
                     "npm install -g appium"))
    if appium:
        code, txt = _run(["appium", "driver", "list", "--installed", "--json"], timeout=60)
        out.append(Check("Appium XCUITest driver", code == 0 and "xcuitest" in txt, "",
                         "appium driver install xcuitest"))
    try:
        with urllib.request.urlopen(s.appium_server_url.rstrip("/") + "/status", timeout=3) as r:
            up = r.status == 200
    except Exception:
        up = False
    out.append(Check("Appium server running", up, s.appium_server_url,
                     "Start it in another terminal: appium   (the first run also builds "
                     "WebDriverAgent, which takes 1-2 minutes)"))
    out.append(Check("Tesseract (OCR)", shutil.which("tesseract") is not None, "",
                     "brew install tesseract"))
    key = bool(os.environ.get("ANTHROPIC_API_KEY") or s.anthropic_api_key)
    out.append(Check("Anthropic API key", key, "", "export ANTHROPIC_API_KEY=sk-ant-..."))
    phones = available_iphones()
    out.append(Check("iPhone simulator", bool(phones), pick_device(None, phones) if phones else "",
                     "Xcode > Settings > Components: install an iOS Simulator runtime."))
    built = (FIXTURE_DIR / "Correct" / "Shoply.app").exists()
    out.append(Check("Demo app (Shoply) built", built, str(FIXTURE_DIR) if built else "",
                     _build_hint()))
    return out


def doctor() -> int:
    results = checks()
    for c in results:
        mark = "✓" if c.ok else "✗"
        line = f"  {mark} {c.name}" + (f"  ({c.detail})" if c.detail else "")
        print(line)
        if not c.ok and c.fix:
            print(f"      → {c.fix}")
    bad = [c for c in results if not c.ok]
    print("\nReady." if not bad else f"\n{len(bad)} thing(s) to fix before `tamam run`.")
    return 0 if not bad else EXIT["USAGE"]


# ── run ─────────────────────────────────────────────────────────────────────────────────


def _fixture_case(case_id: str, buggy: bool, device: str, max_steps: int):
    from tamam.evals.models import EvalCase, Target
    from tamam.evals.scenarios import SPECS
    spec = next((s for s in SPECS if s["did"] == case_id), None)
    if spec is None:
        names = ", ".join(s["did"] for s in SPECS)
        raise UsageError(f"Unknown fixture case {case_id!r}. Try one of: {names}")
    app = FIXTURE_DIR / ("Buggy" if buggy else "Correct") / "Shoply.app"
    if not app.exists():
        raise UsageError(f"The demo app isn't built yet. Run {_build_hint()}")
    target = Target(platform="ios", executor_id="ios_agent", app_build_ref=str(app),
                    app_bundle_id=FIXTURE_BUNDLE, device=device)
    return EvalCase(id=case_id, category=spec["cat"], difficulty=spec["diff"], objective=spec["obj"],
                    expected="fail" if buggy else "pass", target=target, max_steps=max_steps)


def _app_case(app: str, bundle: str, objective: str, device: str, max_steps: int):
    from tamam.evals.models import EvalCase, Target
    target = Target(platform="ios", executor_id="ios_agent", app_build_ref=app,
                    app_bundle_id=bundle, device=device)
    return EvalCase(id="adhoc", category="adhoc", difficulty="medium", objective=objective,
                    expected="pass", target=target, max_steps=max_steps)


def report(result) -> dict:  # noqa: ANN001
    ev = getattr(result, "evidence", None)
    usage = getattr(result, "usage", {}) or {}
    return {
        "verdict": {"passed": "PASS", "failed": "FAIL"}.get(str(getattr(result.outcome, "value", result.outcome)),
                                                            "INCONCLUSIVE"),
        "reason": getattr(result, "reason", ""),
        "steps": len(ev.steps) if ev else 0,
        "seconds": round(usage.get("duration_s") or 0),
        "cost_usd": usage.get("total_cost"),
        "video": getattr(ev, "video_path", None) if ev else None,
        "final_screenshot": getattr(ev, "final_screenshot_path", None) if ev else None,
    }


def run(args: argparse.Namespace) -> int:
    from tamam.config import get_settings
    from tamam.evals.runner import execute_case
    if not (os.environ.get("ANTHROPIC_API_KEY") or get_settings().anthropic_api_key):
        raise UsageError("No Anthropic API key. Run: export ANTHROPIC_API_KEY=sk-ant-...")
    device = pick_device(args.device, available_iphones())
    if args.fixture:
        if args.fixture != "shoply":
            raise UsageError("The only bundled demo app is 'shoply'.")
        case = _fixture_case(args.case, args.buggy, device, args.max_steps)
    else:
        if not (args.app and args.bundle and args.test):
            raise UsageError("Give --fixture shoply, or --app, --bundle and the test in quotes.")
        case = _app_case(args.app, args.bundle, args.test, device, args.max_steps)
    print(f"Testing: {case.objective}\n")
    result = asyncio.run(execute_case(case))
    r = report(result)
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        cost = f"${r['cost_usd']:.2f}" if isinstance(r["cost_usd"], (int, float)) else "n/a"
        print(f"{r['verdict']} — {r['reason']}")
        print(f"  {r['steps']} steps · {r['seconds']} s · {cost}")
        for k in ("video", "final_screenshot"):
            if r[k]:
                print(f"  {k.replace('_', ' ')}: {r[k]}")
    return EXIT[r["verdict"]]


def main(argv: list[str] | None = None) -> int:
    from tamam.evals.scenarios import SPECS
    ap = argparse.ArgumentParser(
        prog="tamam", description="The AI QA tester that proves every pass.",
        epilog="Exit codes: 0 PASS, 1 FAIL, 2 inconclusive, 3 setup or usage problem.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="check this Mac is ready to run tests")
    r = sub.add_parser("run", help="run one test and print an evidence-backed verdict")
    r.add_argument("test", nargs="?", help="the test, in plain English (with --app)")
    r.add_argument("--fixture", help="run a test on a bundled demo app: shoply")
    r.add_argument("--case", default="cart-total", metavar="ID",
                   help="which demo test (default: cart-total). One of: "
                        + ", ".join(s["did"] for s in SPECS))
    r.add_argument("--buggy", action="store_true", help="use the demo app's buggy build")
    r.add_argument("--app", help="path to your simulator .app")
    r.add_argument("--bundle", help="your app's bundle id")
    r.add_argument("--device", help=f"simulator model (default: {PREFERRED_DEVICE} if installed, "
                                    "else the newest iPhone)")
    r.add_argument("--max-steps", type=int, default=30, help="stop after this many actions (default 30)")
    r.add_argument("--json", action="store_true", help="print the result as JSON")
    r.add_argument("--verbose", action="store_true", help="show the harness's logs while it runs")
    args = ap.parse_args(argv)
    import logging
    # Quiet by default: a first run should show the verdict, not the harness's internals.
    logging.basicConfig(level=logging.INFO if getattr(args, "verbose", False) else logging.ERROR, force=True)
    try:
        return doctor() if args.cmd == "doctor" else run(args)
    except UsageError as exc:
        print(f"tamam: {exc}", file=sys.stderr)
        return EXIT["USAGE"]


if __name__ == "__main__":
    sys.exit(main())
