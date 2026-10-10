"""DeviceSession.final: the evidence frame is the APP's end state."""

from tamam.executors.ios.session import DeviceSession

SHEET = """<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="App" label="App" x="0" y="0" width="402" height="874" visible="true">
 <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Logged in successfully" name="Logged in successfully" x="16" y="100" width="300" height="30" visible="true"/>
 <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Save Password?" name="Save Password?" x="16" y="600" width="300" height="30" visible="true"/>
 <XCUIElementTypeButton type="XCUIElementTypeButton" label="Not Now" name="Not Now" x="16" y="700" width="160" height="44" visible="true"/>
 <XCUIElementTypeButton type="XCUIElementTypeButton" label="Save Password" name="Save Password" x="200" y="700" width="160" height="44" visible="true"/>
</XCUIElementTypeApplication>"""
APP = """<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="App" label="App" x="0" y="0" width="402" height="874" visible="true">
 <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Logged in successfully" name="Logged in successfully" x="16" y="100" width="300" height="30" visible="true"/>
</XCUIElementTypeApplication>"""


class Driver:
    def __init__(self):
        self.sheet_up, self.tapped = True, []

    async def accessibility(self):
        return SHEET if self.sheet_up else APP

    def screen_size(self):
        return (402, 874)

    async def tap(self, el):
        self.tapped.append(el.label)
        self.sheet_up = False

    async def screenshot(self):
        return None


async def test_a_save_password_sheet_is_declined_before_the_final_capture():
    """the evidence frame was iOS's 'Save Password?' sheet, not the app."""
    d = Driver()
    s = DeviceSession(d, perceiver=None, reconnect=None)
    s.screenshot = d.screenshot
    text, _ = await s.final()
    assert d.tapped == ["Not Now"]                      # declined: never "Save Password"
    assert "Save Password?" not in text and "Logged in successfully" in text
