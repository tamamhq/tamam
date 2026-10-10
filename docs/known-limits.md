# Known limits

What Tamam does not do well yet, measured rather than guessed. Numbers come from repeated runs
on the bundled demo app, Shoply.

**Accuracy.** Across 402 runs of the Shoply suite (correct and buggy builds), 0 false passes and
1% of runs not ending in the right verdict. The misses were all *inconclusive*, never a wrong
PASS: a star-rating control (3 of 7 runs) and a press-and-hold button (1 of 7).

**One known false pass.** On Shoply's error-alert test, an app dialog that appears late can be
missed by the agent's single look, so the buggy build occasionally passes (about 1 run in 20
when it was found; none in the 402 runs above). The regression gate lists it in
`backend/scripts/known_false_passes.txt` until it is fixed.

**Apps with poor accessibility.** Tamam reads the accessibility tree first and falls back to
OCR. Screens drawn as one big image, or controls with no label at all, are slower and less
reliable.

**Apple Pay and other system sheets.** Payment sheets, Face ID prompts and some system dialogs
are outside the app and are not driven yet.

**Platforms.** iOS only, Apple Silicon Macs only. Web and Android are coming.

**Cost and speed.** An AI-driven run costs about $0.10–0.15 and takes about a minute for a
short flow. Graduated tests replay without AI in seconds; see
[fast replay](guide/test-graduation.md).
