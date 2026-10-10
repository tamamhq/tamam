"""The typed-text redaction policy, on its own."""

from tamam.executors.core.redact import MARK, Redactor, credentials_in


def test_credentials_written_into_the_test_are_found():
    t = "Sign in with email a@b.co and password hunter2. Enter PIN: 4821; token=abc-123."
    assert credentials_in(t) == {"hunter2", "4821", "abc-123"}


def test_a_word_after_password_is_not_a_secret():
    assert credentials_in("Tap the password field, then the PIN pad") == set()


def test_policy_secure_or_not_in_the_test():
    r = Redactor("search for Mouse")
    assert r.note_typed("Mouse", secure=False)["in_test_text"] is True
    r.note_typed("s3cret!", secure=True)
    r.note_typed("made.up@x.io", secure=False)
    out = r.scrub({"a": "Mouse s3cret! made.up@x.io", "n": 3, "l": ["s3cret!"]})
    assert out == {"a": f"Mouse {MARK} {MARK}", "n": 3, "l": [MARK]}


def test_short_values_are_not_scrubbed_and_structure_survives():
    r = Redactor("")
    r.note_typed("ok", secure=True)               # 2 chars: never scrubbed (documented)
    assert r.scrub({"action": "done", "m": "ok"}) == {"action": "done", "m": "ok"}


def test_a_value_the_test_marks_as_the_secret_counts_even_without_digits():
    """"password: sunshine" and "PIN 'tiger'" are secrets; "password is required" is not."""
    assert credentials_in("Log in with password: sunshine") == {"sunshine"}
    assert credentials_in("Enter the PIN 'tiger' when asked") == {"tiger"}
    assert credentials_in("A password is required; tap the password field") == set()
