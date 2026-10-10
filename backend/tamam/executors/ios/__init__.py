"""iOS mobile executor — the AgentExecutor's shape, retargeted to a device.

Same brain, same loop, same evidence bundle as the web agent; only the bottom
three seams (provider / driver / perceiver) are iOS-specific. Appium is pure
actuation behind the DeviceDriver seam — never a decision-maker.
"""
