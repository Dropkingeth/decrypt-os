## What changed

<!-- The problem this solves, the affected component(s), and what changed. -->

## How was this tested

<!-- Commands you ran and their results. Separate source-fixture/unit test
results from any native-guest (Linux ARM64) observations, and say
plainly which platforms you did NOT test on. -->

```
$ 
```

- [ ] Ran `python3 tools/privacy_check.py --root .` and it reported clean.
- [ ] Ran the tests for the component(s) this touches.

## Known limitations

<!-- What this doesn't cover, and anything a reviewer should double check. -->

## Checklist

- [ ] No credentials, personal paths, account numbers, personalized disk
      images or internal logs are included anywhere in this diff.
- [ ] Trading strategy logic is not included (strategies are invite-only
      TradingView scripts, not part of this repository).
- [ ] Third-party license notices are preserved (see `NOTICES.md`).
- [ ] This PR does not change image identity, enrollment, privilege
      boundaries, signatures or release packaging -- or it does, and I've
      flagged that clearly above for an independent review.
