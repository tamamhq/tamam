import contextlib
from tamam import observability as obs

def test_span_propagates_body_typeerror(monkeypatch):
    """A TypeError from the body must surface as itself, not 'generator didn't stop'."""
    class FakeLaminar:
        def start_as_current_span(self, name, **kw):
            return contextlib.nullcontext()
    monkeypatch.setattr(obs, "_laminar", FakeLaminar())
    import pytest
    with pytest.raises(TypeError, match="boom"):
        with obs.span("x", step=1):
            raise TypeError("boom")

def test_span_falls_back_when_input_kwarg_unsupported(monkeypatch):
    """Old lmnr (no input kwarg) → constructor TypeError → fallback, body still runs once."""
    import contextlib as c
    calls = []
    class OldLaminar:
        def start_as_current_span(self, name, **kw):
            if "input" in kw:
                raise TypeError("unexpected kwarg 'input'")
            calls.append(name)
            return c.nullcontext()
    monkeypatch.setattr(obs, "_laminar", OldLaminar())
    ran = []
    with obs.span("x", step=1):
        ran.append(True)
    assert ran == [True] and calls == ["x"]


def test_span_output_is_recorded_and_fails_open(monkeypatch):
    got = []
    class FakeLaminar:
        def set_span_output(self, v): got.append(v)
    monkeypatch.setattr(obs, "_laminar", FakeLaminar())
    obs.span_output({"step": 1})
    assert got == [{"step": 1}]

    class Broken:
        def set_span_output(self, v): raise RuntimeError("laminar down")
    monkeypatch.setattr(obs, "_laminar", Broken())
    obs.span_output({"step": 2})          # must not raise: observability fails open
    monkeypatch.setattr(obs, "_laminar", None)
    obs.span_output({"step": 3})          # off: no-op
