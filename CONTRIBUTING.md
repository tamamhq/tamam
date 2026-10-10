# Contributing

Thanks for helping. Small, focused pull requests are easiest to review.

## Run the tests

```bash
cd backend && uv sync
uv run pytest        # no API key, simulator or network needed
uv run ruff check .
```

Every behaviour change needs a unit test. Changes to how the agent sees, acts or decides
should also pass an end-to-end eval on Shoply (needs an API key and a simulator):

```bash
export TAMAM_FIXTURE_CORRECT_APP=$PWD/../fixtures/ios/build/Correct/Shoply.app
export TAMAM_FIXTURE_BUGGY_APP=$PWD/../fixtures/ios/build/Buggy/Shoply.app
uv run python scripts/run_evals.py --repeats 1 --case cart-total
scripts/regression.sh --quick     # unit tests + replay eval (~25 min); without --quick: the full gate
```

The bar for a change to the trust layer: no new false pass (a buggy build that passes) and
no new false alarm (a correct build that fails). See
[evals and the regression gate](docs/guide/evals-and-regression-gate.md).

## Sign your commits

We use the [Developer Certificate of Origin](https://developercertificate.org/): add
`Signed-off-by` to each commit with `git commit -s`.
