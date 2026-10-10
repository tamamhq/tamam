"""The screen matcher shared by compile-time grounding and replay."""

from tamam.executors.core.screen import element_shows, screen_shows


def test_amounts_are_whole_tokens():
    assert element_shows("Golden Ticket — $5", "$5")
    assert not element_shows("Golden Ticket — $50", "$5")
    assert not element_shows("Total: $5.50", "$5")
    assert element_shows("20% off everything", "20%")
    assert not element_shows("120% off", "20%")


def test_words_are_whole_tokens():
    assert element_shows("Books (12)", "Books")
    assert not element_shows("Bookshelf", "Books")


def test_with_text_binds_the_value_to_its_row():
    rows = ["Catalog item 5 — $5", "Golden Ticket — $50"]
    assert screen_shows(rows, "$5")                               # some row has $5 ...
    assert not screen_shows(rows, "$5", with_text="Golden Ticket")  # ... but not the ticket's
    assert screen_shows(["Golden Ticket — $5"], "$5", with_text="Golden Ticket")
