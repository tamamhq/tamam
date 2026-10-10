"""Hooks — every app quirk the iOS loop handles, as a small composable part.

The loop itself is only observe → decide → act. Everything else (clearing alerts,
tidying the keyboard, annotating an action's effect, learning icon meanings, escalating
to vision, refusing a premature `done`) is a Hook at one of four boundaries:

    before_observe(ctx)          — prepare the device before the screen is read
    after_observe(ctx)           — enrich ctx.space / ctx.history / ctx.screenshot
    after_action(ctx, action, note) -> note   — annotate what an action did
    on_done(ctx, done) -> str | None          — a STOP GATE: return a reason to refuse

Same idea as Claude Code / Codex hooks and pi extensions: the core stays small and a
new quirk is a new hook, not another branch inside the loop. Hooks run in list order
and share state through StepContext. A hook must never raise into the loop — the
runner isolates each one, so a broken hook degrades to a no-op, not a dead run.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from tamam.executors.ios.brain import Back, BackgroundApp, Done, Swipe, Tap, TapPoint, Type, Wait
from tamam.executors.core.contracts import ActionSpace
from tamam.executors.ios.perceiver import app_dialog_title, is_opaque_label, revealed_label
from tamam.harness.checks import required_actions

logger = logging.getLogger(__name__)

STUCK_WINDOW = 4  # recent actions over which repetition counts as "stuck"


@dataclass
class StepContext:
    """Everything one run's hooks can read or write. The loop owns the lifecycle; hooks
    own their own fields (declared here so the shared state is visible in one place)."""

    objective: str
    driver: Any
    history: list[str] = field(default_factory=list)
    step_num: int = 0
    space: ActionSpace | None = None
    shot_path: str | None = None           # this step's raw screenshot (evidence)
    screenshot: bytes | None = None        # what the MODEL sees this step (None = text only)
    annotated_path: str | None = None      # the set-of-marks image, persisted as evidence
    last_action: Any = None
    performed: set[str] = field(default_factory=set)       # action kinds carried out
    action_counts: Counter = field(default_factory=Counter)  # fingerprint → times seen
    recent_fps: list[str] = field(default_factory=list)
    # Effect tracking (written by EffectAnnotation, read by VisionEscalation).
    prev_hash: str | None = None
    # Index in `history` of the last ACTION's note — its effect is annotated there, not on
    # whatever was appended since (an alert report, a gate refusal, a hint).
    action_note_idx: int | None = None
    no_progress_streak: int = 0
    stuck: bool = False
    # Opaque-icon learning.
    probing: Any = None
    probe_age: int = 0
    learned: dict[str, str] = field(default_factory=dict)
    memory: str = ""                       # the agent's own carried-forward notes
    # The last action TIMED OUT: the one-command-at-a-time session is still chewing on it,
    # so every extra command queues behind it and burns its own full timeout. Device-prep
    # hooks that are not essential skip themselves until an observation succeeds.
    session_busy: bool = False
    # Per-step timing of each hook boundary ("before_observe:accept_alerts" → seconds).
    hook_times: dict[str, float] = field(default_factory=dict)
    # Stop-gate bookkeeping: gate name → times it has refused `done`.
    refusals: Counter = field(default_factory=Counter)
    # The run's typed-text redaction policy (see redact.py): everything the run emits or
    # stores passes through it, since typed text can be a password.
    redactor: Any = None


class Hook:
    """No-op base: override only the boundaries you need."""

    name = "hook"

    async def before_observe(self, ctx: StepContext) -> None:
        return None

    async def after_observe(self, ctx: StepContext) -> None:
        return None

    async def after_action(self, ctx: StepContext, action: Any, note: str) -> str:
        return note

    async def on_done(self, ctx: StepContext, done: Done) -> str | None:
        return None


class Hooks:
    """Runs a hook list with per-hook isolation."""

    def __init__(self, hooks: list[Hook]) -> None:
        self.hooks = list(hooks)

    async def before_observe(self, ctx: StepContext) -> None:
        for h in self.hooks:
            await self._timed(ctx, h, "before_observe", h.before_observe(ctx))

    async def after_observe(self, ctx: StepContext) -> None:
        for h in self.hooks:
            await self._timed(ctx, h, "after_observe", h.after_observe(ctx))

    async def after_action(self, ctx: StepContext, action: Any, note: str) -> str:
        for h in self.hooks:
            got = await self._timed(ctx, h, "after_action", h.after_action(ctx, action, note))
            if isinstance(got, str):
                note = got
        return note

    async def _timed(self, ctx: StepContext, hook: Hook, where: str, coro):
        t0 = time.monotonic()
        try:
            return await self._safe(hook, where, coro)
        finally:
            dt = time.monotonic() - t0
            if dt >= 0.05:  # only hooks that actually cost something
                key = f"{where}:{hook.name}"
                ctx.hook_times[key] = round(ctx.hook_times.get(key, 0) + dt, 2)

    async def on_done(self, ctx: StepContext, done: Done) -> str | None:
        """The first gate that refuses wins; its refusal is counted so a gate can bound
        itself (like Claude Code's stop_hook_active — a gate must never block forever)."""
        for h in self.hooks:
            reason = await self._safe(h, "on_done", h.on_done(ctx, done))
            if reason:
                ctx.refusals[h.name] += 1
                return reason
        return None

    @staticmethod
    async def _safe(hook: Hook, where: str, coro):
        try:
            return await coro
        except Exception:
            logger.warning("hook %s.%s failed; skipping it this step", hook.name, where,
                           exc_info=True)
            return None


# ── device preparation ──────────────────────────────────────────────────────


class AcceptAlerts(Hook):
    """Clear a blocking dialog — and REPORT it, since the harness acted on the agent's
    behalf. Two paths, both on the record:
      • iOS system dialogs (permission prompts) are accepted by the perceiver during the
        read; this hook puts its notes into the history (after_observe).
      • a dialog wedging the session: only when the last action TIMED OUT. An
        unconditional check each step is a device query that walks the whole app tree:
        on a real app with an infinite feed, each full walk paged in more rows.
    The app's OWN dialogs are otherwise left to the agent — accepting them blindly is how
    a "Couldn't save" error vanished before it was seen (defect #15 in the fixture)."""

    name = "accept_alerts"

    async def after_observe(self, ctx: StepContext) -> None:
        for note in (getattr(ctx.space, "harness_notes", None) or []):
            ctx.history.append(note)

    async def before_observe(self, ctx: StepContext) -> None:
        if not ctx.session_busy:
            return
        try:
            got = await ctx.driver.dismiss_alert()
        except Exception:
            return
        text = got if isinstance(got, str) and got else ("(alert)" if got is True else None)
        if text:
            ctx.history.append(f'[harness] an alert appeared and was accepted '
                               f'automatically: "{text[:160]}"')


class HideKeyboard(Hook):
    """An on-screen keyboard adds ~80x the accessibility tree and nothing the agent uses.
    Put it away before the snapshot unless the agent is mid-typing; after a `type`, put
    it away straight away (it stays up and makes the NEXT observe pathologically slow)."""

    name = "hide_keyboard"

    def __init__(self) -> None:
        self._gave_up = False

    async def before_observe(self, ctx: StepContext) -> None:
        if ctx.session_busy:
            return  # a queued keyboard call would cost a full timeout for nothing
        # Only when the LAST read showed a keyboard: a blind hide is a device query that
        # walks the tree, paid on every step for a keyboard that is usually not there.
        keyboard_up = ctx.space is not None and ctx.space.keyboard
        if not keyboard_up:
            self._gave_up = False  # a new keyboard later gets a fresh try
            return
        if self._gave_up or isinstance(ctx.last_action, (Type, TapPoint)):
            return
        if await _hide_keyboard(ctx.driver) is False:
            # This keyboard has none of the dismiss keys. Retrying cost ~6s a step for
            # nothing for several steps, so leave it until it goes away.
            self._gave_up = True

    async def after_action(self, ctx: StepContext, action: Any, note: str) -> str:
        if isinstance(action, Type) and not ctx.session_busy:
            await _hide_keyboard(ctx.driver)
        return note


async def _hide_keyboard(driver: Any) -> bool | None:
    """True/False from the driver (dismissed or not); None if it could not be asked."""
    fn = getattr(driver, "hide_keyboard", None)
    if fn is None:
        return None
    try:
        return await fn()
    except Exception:
        return False


# ── observation enrichment ──────────────────────────────────────────────────


class LearnIcons(Hook):
    """Icon-only controls name their ASSET, not their meaning, and many reveal their real
    name only while selected. Learn by doing: after tapping one, read what it turned out
    to be, and tell the agent so it never guesses that control again."""

    name = "learn_icons"
    PATIENCE = 3  # screens to wait for the reveal (a popup often lands in between)

    async def after_observe(self, ctx: StepContext) -> None:
        space = ctx.space
        if ctx.probing is not None:
            found = revealed_label(ctx.probing, space.elements)
            if found and found != ctx.probing.label:
                ctx.learned[ctx.probing.label] = found
                logger.info("learned icon %r = %r", ctx.probing.label, found)
                ctx.probing, ctx.probe_age = None, 0
            else:
                ctx.probe_age += 1
                if ctx.probe_age > self.PATIENCE:
                    ctx.probing, ctx.probe_age = None, 0
        if ctx.learned:
            space.text += (
                "\n\nIcon meanings you have already established this run by "
                "selecting them (do NOT re-test these):\n"
                + "\n".join(f'  {k} = "{v}"' for k, v in sorted(ctx.learned.items()))
            )

    async def after_action(self, ctx: StepContext, action: Any, note: str) -> str:
        if isinstance(action, Tap):
            el = ctx.space.by_ref(action.ref) if ctx.space else None
            if el is not None and is_opaque_label(el.label):
                ctx.probing, ctx.probe_age = el, 0
        return note


class EffectAnnotation(Hook):
    """Tell the agent what its last action DID, by comparing screens (no extra device
    call). The NoProgress guardrail trips on a run of unchanged screens; this note says
    why, so the agent self-corrects first. After a tap, name what is now selected —
    the ground truth for icon-only controls whose names are opaque — and what a switch
    is now set to, read from the app (a switch that snaps back reads "still off")."""

    name = "effect_annotation"

    def __init__(self) -> None:
        self._prev_toggles: dict[str, bool] = {}
        self._prev_by_ref: dict[str, Any] = {}

    async def after_observe(self, ctx: StepContext) -> None:
        space = ctx.space
        i = ctx.action_note_idx
        if i is not None and i < len(ctx.history) and ctx.prev_hash is not None:
            if space.source_hash != ctx.prev_hash:
                ctx.no_progress_streak = 0
                ctx.history[i] += (" — screen changed (may be cosmetic; confirm "
                                   "your goal actually advanced before continuing)")
            else:
                ctx.no_progress_streak += 1
                note = " — screen did not change"
                if isinstance(ctx.last_action, Type) and (not (ctx.last_action.ref or "").strip()
                                                          or "found by its outline" in ctx.history[i]):
                    # Typed into a field the tree does not show (a flattened form): the tree
                    # cannot change, so it says nothing about the text (the text HAD
                    # landed, and this note told the agent it had not).
                    note = (" — the element list cannot show this field, so check the "
                            "SCREENSHOT for the text you typed" if "never show a password field" not in
                            ctx.history[i] else "")
                elif isinstance(ctx.last_action, Type):
                    note += (" (the field did NOT accept the text — it may not be an "
                             "editable input; do not retype it, try another element)")
                ctx.history[i] += note
                ctx.history[i] += self._no_progress_warning(ctx)
            if isinstance(ctx.last_action, Tap):
                active = [e.label for e in space.elements if e.selected and e.label]
                if active:
                    labels = ", ".join(f'"{a}"' for a in active[:4])
                    ctx.history[i] += f" — now selected: {labels}"
            if isinstance(ctx.last_action, (Tap, TapPoint)):
                ctx.history[i] += self._toggle_effect(ctx.last_action, space)
        ctx.action_note_idx = None  # each action's effect is annotated exactly once
        ctx.prev_hash = space.source_hash
        self._prev_toggles = _toggles(space)
        self._prev_by_ref = {e.ref: e for e in space.elements}

    @staticmethod
    def _no_progress_warning(ctx: StepContext) -> str:
        """Two unchanged screens before the NoProgress guardrail ends the run, ask the agent to
        conclude: varied retries (a longer hold, a drag after a swipe) never trip the
        repetition warning, and a run cut off by the guardrail is inconclusive — even when the
        app plainly ignored a correct action, which is the bug the test is there to catch."""
        from tamam.config import get_settings
        limit = get_settings().no_progress_limit
        if ctx.no_progress_streak != max(2, limit - 2):
            return ""
        return (f" — the screen has not changed after your last {ctx.no_progress_streak} actions, "
                f"and the run stops as inconclusive after {limit}. Decide now: if you performed "
                "the step reliably (an element by its ref) and the app ignored it, that is an "
                "app failure — conclude with done(passed=false) citing those steps. If you are "
                "not sure you performed it right (a guessed tap_point, a gesture you are unsure "
                "of), conclude with done(passed=false, blocked=\"missing_action\"). Only keep "
                "going if you have a genuinely different way to do the step")

    def _toggle_effect(self, action: Any, space: Any) -> str:
        now = _toggles(space)
        changed = [f'"{k}" {"on" if v else "off"}' for k, v in now.items()
                   if k in self._prev_toggles and self._prev_toggles[k] != v]
        if changed:
            return " — switch now " + ", ".join(changed[:4])
        target = self._prev_by_ref.get(getattr(action, "ref", None) or "")
        if target is not None and target.toggled is not None and target.label in now:
            return f' — switch "{target.label}" is still {"on" if now[target.label] else "off"}'
        return ""


def _toggles(space: Any) -> dict[str, bool]:
    """Labelled two-state controls on a screen → their on/off state (from the app)."""
    return {e.label: e.toggled for e in getattr(space, "elements", [])
            if getattr(e, "toggled", None) is not None and e.label}


class VisionEscalation(Hook):
    """Decide whether the model sees a screenshot this step, and build it. Vision fires on
    step 1, when the policy says the tree is weak, or when STUCK — the screen isn't
    advancing, or the agent keeps repeating an action in the RECENT window. A grounder
    may add targets the tree missed; then set-of-marks is drawn over the result."""

    name = "vision"

    def __init__(self, policy: Any, grounder: Any, som: Any, grounder_fn: Any) -> None:
        self._policy = policy
        self._grounder = grounder
        self._som = som                  # (shot_path, space) -> (bytes, path)
        self._augment = grounder_fn      # (grounder, shot_path, space, objective) -> space

    async def after_observe(self, ctx: StepContext) -> None:
        window = ctx.recent_fps[-STUCK_WINDOW:]
        top_repeat = max((window.count(f) for f in window), default=0)
        stuck = ctx.no_progress_streak >= 2 or top_repeat >= 2
        if stuck and not ctx.stuck:
            # Once per stuck episode, not every step of it.
            ctx.history.append(
                "[hint] The screen hasn't advanced — if your target is VISIBLE in the "
                "screenshot but has no [e#] ref: for a FIELD, type with the ref of its label "
                "(the text above it) and the harness finds the box; for anything else, tap it "
                "with tap_point x y (screen fractions).")
        ctx.stuck = stuck
        should_see = self._policy.should_see(ctx.space)
        logger.debug("vision step %d: should_see=%s stuck=%s no_progress=%d top_repeat=%d",
                     ctx.step_num, should_see, stuck, ctx.no_progress_streak, top_repeat)
        if should_see or ctx.step_num == 1 or stuck:
            ctx.space = await self._augment(self._grounder, ctx.shot_path, ctx.space,
                                            ctx.objective)
            ctx.screenshot, ctx.annotated_path = self._som(ctx.shot_path, ctx.space)


# ── action annotation ───────────────────────────────────────────────────────


class RepetitionWarning(Hook):
    """After the 3rd identical action, say so in the note — the agent's cue to change
    approach before the RepeatedAction guardrail ends the run."""

    name = "repetition_warning"
    AT = 3

    async def after_action(self, ctx: StepContext, action: Any, note: str) -> str:
        fp = fingerprint(action, ctx.space)
        if fp and ctx.action_counts[fp] >= self.AT:
            note += (f" — you have already performed this same action "
                     f"{ctx.action_counts[fp]}× with no progress; do NOT repeat it — "
                     "choose a different element, scroll to reveal more, or conclude")
        return note


# ── stop gates ──────────────────────────────────────────────────────────────


class RequiredActionGate(Hook):
    """A PASS asserts the objective was carried out. Some objectives cannot be carried
    out without a specific action, and the step log says whether it happened — so the
    claim is checked in code, not taken on trust. Refuses once. (A FAIL needs no gate.)"""

    name = "required_action_gate"

    async def on_done(self, ctx: StepContext, done: Done) -> str | None:
        if not done.passed or ctx.refusals[self.name]:
            return None
        missing = required_actions(ctx.objective) - ctx.performed
        if not missing:
            return None
        want = ", ".join(sorted(missing))
        logger.info("pass claimed without required action(s): %s", want)
        return (f"[guardrail] Not accepted: you reported success, but this objective "
                f"cannot be met without `{want}`, and you never performed it. Do it now "
                f"and check the result, then conclude. If it turns out the objective does "
                f"not hold, say so — a failing verdict is fine.")


class BlankVerdictGate(Hook):
    """A verdict formed while nothing was on screen observed nothing. Look again once."""

    name = "blank_verdict_gate"

    async def on_done(self, ctx: StepContext, done: Done) -> str | None:
        if ctx.space is None or ctx.space.elements or ctx.refusals[self.name]:
            return None
        logger.info("verdict was formed on a blank screen; re-observing")
        return ("[guardrail] Not accepted yet: that conclusion was reached while the "
                "screen showed nothing at all — the app was still in transition, so "
                "nothing was actually observed. The screen below is what is really "
                "there; decide from it.")


# ── shared helpers ──────────────────────────────────────────────────────────


def fingerprint(action: Any, space: Any) -> str | None:
    """A stable identity for an action, robust to ref re-assignment across steps.
    Uses the target's label + coarse coordinates (not its churning [e#] ref) so
    'tap the same control again' is recognizable. None for Done (no repetition)."""
    if isinstance(action, (Tap, Type)):
        el = space.by_ref(action.ref) if space is not None else None
        if el is not None:
            cx, cy = el.frame.center
            # A switch's state is part of WHAT the tap does: on→off→on is not one
            # repeated action, though the label and position stay the same.
            state = "" if getattr(el, "toggled", None) is None else f":{int(el.toggled)}"
            return f"{action.action}:{el.label}{state}:{int(cx // 20)},{int(cy // 20)}"
        return f"{action.action}:{action.ref}"
    if isinstance(action, BackgroundApp):
        return f"background_app:{action.seconds}"
    if isinstance(action, Swipe):
        # A row-swipe and a window-swipe are different actions. And a swipe is only a
        # REPEAT when made on the same screen: scrolling a long form or feed swipes up
        # many times, each from a new position, and a run-wide count of "swipe:up"
        # killed a legitimate audit (swipes in a modal, then in a list).
        # Swiping on an unchanged screen still repeats — that is being stuck.
        where = getattr(space, "source_hash", "") if space is not None else ""
        return f"swipe:{action.direction}:{(action.ref or '').strip()}:{where}"
    if isinstance(action, TapPoint):
        return f"tap_point:{round(action.x, 1)},{round(action.y, 1)}"
    if isinstance(action, (Wait, Back)):
        return action.action
    return None


class SettledVerdictGate(Hook):
    """A PASS is decided on the last screen the agent read — but a result can land a moment
    later (an error dialog after "Save"). Before accepting a PASS, wait briefly and look once
    more; if an app dialog is now up that the agent has not seen, refuse once with what it says.
    Runs last, so it costs one extra screen read only on a PASS every other gate accepted."""

    name = "settled_verdict_gate"

    def __init__(self, settle_s: float | None = None, sleep: Any = asyncio.sleep) -> None:
        if settle_s is None:
            from tamam.config import get_settings
            settle_s = get_settings().verdict_settle_s
        self._settle_s, self._sleep = settle_s, sleep

    async def on_done(self, ctx: StepContext, done: Done) -> str | None:
        if not done.passed or ctx.refusals[self.name] or ctx.driver is None or self._settle_s <= 0:
            return None
        await self._sleep(self._settle_s)
        try:
            source = await ctx.driver.accessibility()
        except Exception:
            return None          # a failed read is not evidence either way
        title = app_dialog_title(source)
        if not title or title == (getattr(ctx.space, "alert", "") or ""):
            return None          # nothing new (or the dialog the agent already saw)
        logger.info("a dialog appeared after the agent's last look: %r", title[:120])
        return (f'[guardrail] Not accepted yet: after your last look, the app showed a dialog: '
                f'"{title[:160]}". Read it on the screen below before concluding — if it reports '
                "a problem, the test does not pass.")


def default_hooks(*, vision_policy: Any, grounder: Any, som: Any, grounder_fn: Any) -> list[Hook]:
    """The standard pipeline. Order matters where hooks share state: EffectAnnotation
    computes the no-progress streak VisionEscalation reads."""
    return [
        AcceptAlerts(),
        HideKeyboard(),
        LearnIcons(),
        EffectAnnotation(),
        VisionEscalation(vision_policy, grounder, som, grounder_fn),
        RepetitionWarning(),
        RequiredActionGate(),
        BlankVerdictGate(),
    ]


__all__ = ["Hook", "Hooks", "StepContext", "default_hooks", "fingerprint"]
