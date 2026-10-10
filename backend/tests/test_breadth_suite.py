"""the breadth suite is well-formed — every app has negative controls, and a negative
control can never be satisfied by its own oracle."""

from tamam.evals.breadth import APPS, breadth_cases


def test_every_app_has_passes_and_negative_controls_with_sound_oracles():
    for name, spec in APPS.items():
        cases = breadth_cases(name, app="/x.app")
        assert len({c.id for c in cases}) == len(cases), name
        assert 5 <= len(cases) <= 10, name
        assert any(c.expected == "fail" for c in cases), name
        assert sum(c.expected == "pass" for c in cases) >= 4, name
        assert spec["commit"] and spec["repo"].startswith("https://github.com/"), name
        for c in cases:
            assert c.target.app_bundle_id == spec["bundle"]
            assert not set(c.require_text) & set(c.forbid_text), c.id
            if c.expected == "fail":
                assert not c.require_text or c.forbid_text, c.id
