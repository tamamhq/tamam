# Writing good tests

A test is one plain-English instruction, the way you would ask a colleague to check something.

```text
Add the Wireless Mouse and the USB Cable to the cart, open Cart, and verify the total is $35
```

**Name what the user sees.** Use the words on the screen: "tap Add to Cart", "open the Profile
tab". Tamam finds controls by their visible labels, not by code ids.

**End with something checkable.** "…and verify the total is $35", "…and verify Dark mode is
on". A PASS needs every part of the test proven from the screen, so a vague ending ("…and make
sure it works") can only end *inconclusive*.

**One flow per test.** Keep it to one journey with a handful of steps. Split "sign up, buy
something and change settings" into three tests.

**Put test data in the test.** Credentials, search terms and amounts go in the text:
"Sign in as demo@example.com with password demo1234". Use a test account, never a real one.

**When a run is inconclusive,** the reason line says which part couldn't be proven. Usually the
fix is to name the exact button or value, or to split the test.

## Cost

An AI run costs about $0.10–0.15 and takes about a minute for a short flow. To keep it down:

- keep tests short; `--max-steps` caps how many actions a run may take (default 30);
- set a monthly spend limit on your Anthropic account;
- passing tests can graduate into a replay that needs no AI at all: see
  [fast replay](guide/test-graduation.md).
