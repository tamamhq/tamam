"""Platform-neutral core of the device agent: the element model and action space, the
action vocabulary, and secret redaction. iOS is the first platform built on it; the trust layer
and graduation import from here, so a web or Android perceiver/driver plugs in underneath
without touching them. A perceiver maps its native widget classes onto the ROLES in contracts."""
