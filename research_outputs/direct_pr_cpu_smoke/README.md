# CPU sum smoke: Issue #85

Issue: https://github.com/ken2129/c3_neural_compression/issues/85

## Method and result

One initial CPU-only smoke experiment used the public constant N=1000.
An explicit loop adds integers 1 through N, then compares the sum with
`N * (N + 1) // 2`. Both values were **500500**, and match was
**True**. Calculation process exit code: 0.
Python version: 3.11.17; standard library only.

## Reproduction

From the repository root:

```sh
python -B research_outputs/direct_pr_cpu_smoke/smoke_sum.py
python -B -m unittest discover -s research_outputs/direct_pr_cpu_smoke -p 'test_smoke_sum.py' -v
```

The recorded run used Python -B for the calculation subprocess and the
unittest runner. `result.json` records measured values and test counts.
The commands above reproduce the calculation and tests; they do not rewrite
this historical result file.

## Validation and limitations

Ran **5 tests**: failures=0, errors=0,
skipped=0; tests_passed=True.
Cases cover N=0, N=1, N=1000, rejection of a negative integer, and rejection
of a noninteger. Invalid inputs raised their expected exceptions; no test
or calculation failures occurred. No additional experiment was run.

This checks elementary integer arithmetic only; it provides no neural
compression, performance, GPU, or external-data evidence. No dependencies
were installed. Initial environment inspection found `rg` and `git` unavailable;
Python filesystem inspection was used and publication uses the supplied helper.

Captured unittest output:

```text
test_negative_rejected (test_smoke_sum.SumTests.test_negative_rejected) ... ok
test_noninteger_rejected (test_smoke_sum.SumTests.test_noninteger_rejected) ... ok
test_one (test_smoke_sum.SumTests.test_one) ... ok
test_thousand (test_smoke_sum.SumTests.test_thousand) ... ok
test_zero (test_smoke_sum.SumTests.test_zero) ... ok

----------------------------------------------------------------------
Ran 5 tests in 0.000s

OK
```
