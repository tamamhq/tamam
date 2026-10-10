#!/bin/sh
# Regression gate — run before merging ANY harness change. Unit tests, then every Shoply eval:
# the agent itself, graduation, replay (false passes must stay 0) and the hand-off.
# Prints this run's summary next to the last one, so a drop is visible at a glance.
#
#   scripts/regression.sh            # full gate (~55 min on the simulator)
#   scripts/regression.sh --quick    # unit tests + replay eval only (~25 min)
#   scripts/regression.sh replay handoff   # only these parts (after a fix that touches them)
#   scripts/regression.sh --full     # everything + the stability gate (45 unchanged replays, ~25 min more)
#
# Needs: Appium + a booted simulator, the fixture built (fixtures/ios/build.sh).
set -u
cd "$(dirname "$0")/.."
FIX="$(cd ../fixtures/ios/build && pwd)"
export TAMAM_FIXTURE_CORRECT_APP="$FIX/Correct/Shoply.app"
export TAMAM_FIXTURE_BUGGY_APP="$FIX/Buggy/Shoply.app"
OUT="artifacts/regression/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
PREV=$(ls -d artifacts/regression/*/ 2>/dev/null | grep -v "$OUT" | tail -1)
fail=0

ONLY="$*"
FULL=""
case "$ONLY" in --quick) ONLY="unit replay" ;; --full) ONLY=""; FULL=1 ;; esac
want() { [ -z "$ONLY" ] || echo " $ONLY " | grep -q " $1 "; }
# Opt-in parts: run only when named, or with --full.
want_extra() { [ -n "$FULL" ] || echo " $ONLY " | grep -q " $1 "; }

step() {  # step <name> <summary-grep> <command...>
  name=$1; pat=$2; shift 2
  want "$name" || return 0
  echo "== $name"
  "$@" >"$OUT/$name.log" 2>&1 || fail=1
  grep -E "$pat" "$OUT/$name.log" | tail -3 | tee -a "$OUT/summary.txt"
}

step unit "passed|failed" uv run pytest -q
step replay "FALSE PASSES|SENSIBLE|^(BAD|FALSE|GRAD BAD)" uv run python scripts/replay_eval.py
want replay && { grep -q "FALSE PASSES on buggy: 0 " "$OUT/replay.log" || fail=1; }
step agent "^OVERALL|→ 0/" uv run python scripts/run_evals.py --repeats 1 --no-real
# A buggy build the agent PASSED is the worst result there is. Known ones are listed with
# their issue in scripts/known_false_passes.txt (one case id per line); any other fails the gate.
if want agent; then
  for c in $(grep -E -e "-bug +expect=fail .*'passed'" "$OUT/agent.log" | awk '{print $1}'); do
    if grep -q "^$c " scripts/known_false_passes.txt 2>/dev/null; then
      echo "KNOWN FALSE PASS: $c ($(grep "^$c " scripts/known_false_passes.txt | cut -d' ' -f2-))" | tee -a "$OUT/summary.txt"
    else
      echo "NEW FALSE PASS: $c" | tee -a "$OUT/summary.txt"; fail=1
    fi
  done
  # A correct build that did not pass (a false alarm, or an inconclusive the harness caused) is
  # a regression too — e.g. a new evidence rule refusing a legitimate pass. Known agent limits
  # are listed in scripts/known_inconclusive.txt; they are allowed ONLY when every run ended
  # inconclusive ('error') — a listed case that FAILS a correct build still fails the gate.
  for c in $(grep -E -e "-ok +expect=pass .*→ 0/" "$OUT/agent.log" | awk '{print $1}'); do
    line=$(grep -E -e "^ *$c +expect=pass" "$OUT/agent.log" | tail -1)
    if grep -q "^$c " scripts/known_inconclusive.txt 2>/dev/null \
        && ! echo "$line" | grep -q "'failed'\|'passed'"; then
      echo "KNOWN INCONCLUSIVE: $c ($(grep "^$c " scripts/known_inconclusive.txt | cut -d' ' -f2-))" | tee -a "$OUT/summary.txt"
    else
      echo "CORRECT BUILD NOT PASSED: $c" | tee -a "$OUT/summary.txt"; fail=1
    fi
  done
fi
# Graduation scoring comes from the replay eval's recordings (SENSIBLE / GRAD BAD lines).
want replay && grep -q "^GRAD BAD" "$OUT/replay.log" && fail=1
step handoff "HANDOFF|^BAD" uv run python scripts/handoff_eval.py
want handoff && { grep -q "HANDOFF all OK" "$OUT/handoff.log" || fail=1; }

if want_extra stability; then
  echo "== stability"
  uv run python scripts/stability_eval.py >"$OUT/stability.log" 2>&1 || fail=1
  grep -E "^STABILITY|^FLK" "$OUT/stability.log" | tee -a "$OUT/summary.txt"
  grep -q "STABILITY GATE: PASS" "$OUT/stability.log" || fail=1
fi

if [ -n "$PREV" ] && [ -f "$PREV/summary.txt" ]; then
  echo; echo "== previous ($PREV)"; cat "$PREV/summary.txt"
fi
echo; [ $fail -eq 0 ] && echo "REGRESSION GATE: PASS ($OUT)" || echo "REGRESSION GATE: FAIL ($OUT)"
exit $fail
