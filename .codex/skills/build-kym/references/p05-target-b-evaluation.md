# P05 Target-B Evaluation Gate

Use for P05 Target-B generation and Golden evaluation.

- Customer: product-direction decision maker/change authority belongs under `Stakeholders`; keep one actor with supported Who/Why.
- Naming Conventions: root `statement_refs` stays empty; children are terms. Apply remove-term test and move rule, scope, exclusion, or capability remainder elsewhere.
- Scope / Target Profiles: explicit `Target-B Mainline` plus `Target-A Calibration and Promotion Side-lane` requires two complete candidate profiles; do not encode profiles only as prose or treat generation as accepted.
- Business Rules: create semantic parent `P05 Target-B Candidate Governance`, then groups for contract, diagnostics, provider, promotion, legacy assets. Do not flatten every point or turn artifacts into rules.
- Reverse-walk all branches and record placement, excluded neighbors, support status, and why. A lexical match that fails placement is `review-needed`.

When a Golden conflicts with contract, preserve old version in Git, record oracle discrepancy, and prefer confirmed contract until human fixture update:

```bash
python3 tools/compare_kym_golden.py --expected <expected.md> --candidate <candidate.md> --output <comparison.json>
```
