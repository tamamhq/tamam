"""the trust layer and graduation sit above the platform seam.

A web or Android perceiver maps its native classes onto the shared roles and the rest works
unchanged. Proved two ways: no shared module imports a platform package, and a run recorded on
an Android-style screen compiles and replays with no iOS code in the path.
"""

import ast
from pathlib import Path

from tamam.executors.core.contracts import ROLES, ActionSpace, Element, Frame
from tamam.graduation.compiler import compile_run
from tamam.graduation.replay import ReplayRunner

APP = Path(__file__).resolve().parents[1] / "tamam"



def _imports(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


def test_shared_modules_import_no_platform_package():
    shared = [*(APP / "graduation").glob("*.py"), *(APP / "harness").glob("*.py"),
              *(APP / "executors" / "core").glob("*.py")]
    assert len(shared) > 20, "the shared layers moved: point this test at them"
    leaks = {str(p.relative_to(APP)): sorted(m for m in _imports(p) if m.startswith("tamam.executors.ios"))
             for p in shared}
    assert {k: v for k, v in leaks.items() if v} == {}


# --- an Android-style perceiver: native classes → shared roles ------------------------------

ANDROID_ROLE = {"android.widget.Button": "Button", "android.widget.EditText": "TextField",
                "android.widget.TextView": "StaticText", "android.widget.Switch": "Switch"}


def droid(ref, cls, text, y=100, resource_id=""):
    role = ANDROID_ROLE[cls]
    assert role in ROLES
    return Element(ref=ref, type=role, label=text, frame=Frame(16, y, 360, 48),
                   identifier=resource_id)


def droid_screen(*els, h):
    return ActionSpace(elements=list(els), screen_w=411, screen_h=891, source_hash=h)


def rec(step, action, target=None, texts=()):
    return {"schema": 2, "step": step, "action": dict(action), "target": target, "notes": [],
            "screen": {"anchors": list(texts[:3]), "tree_hash": f"h{step}", "size_pt": [411, 891],
                       "texts": list(texts), "toggles": {}}}


TEST = "Open Cart and verify the total is $35"


class DroidSession:
    """An ActingSession over Android-style screens."""

    act_timed_out = False

    def __init__(self, screens):
        self.screens, self.acts = list(screens), []

    async def observe(self, heartbeat=None):
        return self.screens[0]

    async def act(self, space, action):
        self.acts.append(action)
        if len(self.screens) > 1:
            self.screens.pop(0)
        return f"[step] {action.action}"

    async def screenshot(self):
        return None


async def _nosleep(_):
    return None


async def test_an_android_style_run_compiles_and_replays_with_no_ios_code():
    cart_btn = {"type": "Button", "label": "Cart", "identifier": "com.shop:id/cart",
                "frame_frac": [0.04, 0.9, 0.2, 0.05]}
    records = [rec(1, {"action": "tap", "ref": "e2"}, cart_btn, texts=["Shop", "Cart"]),
               rec(2, {"action": "done", "passed": True}, texts=["Cart", "Total: $35", "Checkout"])]
    clauses = [{"id": "c1", "text": TEST, "status": "done", "step": 2, "evidence": "Total: $35"}]
    d = compile_run(records, clauses, TEST, passed=True, source_run_id="r1")
    assert d.ready, d.reasons
    assert d.script.steps[0].target.locators[0].value == "com.shop:id/cart"   # resource-id first

    session = DroidSession([
        droid_screen(droid("e1", "android.widget.TextView", "Shop"),
                     droid("e2", "android.widget.Button", "Cart", y=800, resource_id="com.shop:id/cart"), h="a"),
        droid_screen(droid("e1", "android.widget.TextView", "Cart"),
                     droid("e2", "android.widget.TextView", "Total: $35"),
                     droid("e3", "android.widget.Button", "Checkout"), h="b"),
    ])
    out = await ReplayRunner(session, d.script, TEST, sleep=_nosleep).run()
    assert out.verdict == "passed", out.reason
    assert session.acts[0].ref == "e2"


def test_the_ios_perceiver_only_emits_shared_roles():
    """ROLES is the contract: if iOS starts emitting a new type, shared code must learn it too."""
    from tamam.executors.ios.perceiver import INTERACTIVE_TYPES
    assert INTERACTIVE_TYPES | {"Other"} <= ROLES


def test_screen_moved_means_scrolled_not_merely_changed():
    """a self-loading catalog ('Loaded: 40 items' -> '60') looked like a moving list, so
    scroll_to never noticed it sat at the top and never turned around."""
    from tamam.executors.core.contracts import screen_moved

    def scr(*rows):
        return ActionSpace(elements=[Element(ref=f"e{i}", type="StaticText", label=t, frame=Frame(0, y, 300, 40))
                                     for i, (t, y) in enumerate(rows)])
    top = scr(("Catalog", 60), ("Loaded: 40 items", 100), ("Item 1", 150), ("Item 2", 200), ("Item 3", 250))
    counter = scr(("Catalog", 60), ("Loaded: 60 items", 100), ("Item 1", 150), ("Item 2", 200), ("Item 3", 250))
    shifted = scr(("Catalog", 60), ("Item 2", 150), ("Item 3", 200), ("Item 4", 250))
    flicked = scr(("Catalog", 60), ("Item 40", 150), ("Item 41", 200), ("Item 42", 250))
    grown = scr(("Catalog", 60), ("Loaded: 40 items", 100), ("Item 1", 150), ("Item 2", 200), ("Item 3", 250),
                ("Item 4", 300), ("Item 5", 350))
    assert not screen_moved(top, counter) and not screen_moved(top, top) and not screen_moved(top, grown)
    assert screen_moved(top, shifted) and screen_moved(top, flicked)
