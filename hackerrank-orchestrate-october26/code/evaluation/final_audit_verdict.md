# Final Audit Verdict: Ghost Data Still Exists

Re-audit of commits `7de75ea` and `ec8a442` ("[BUGFIX] solve reviewer findings"), compared with the state reviewed in [review_findings.md](review_findings.md) (commit `a2eff0a`).
Reviewer: QA audit (read-only; no implementation file was changed).

## Status: **APPROVED** for final submission

There are no critical blockers. The output contract passes every check. The four invalid `COMPLETE` rows with `[]` are fixed, and their evidence is cited correctly. A cache-only run (0 provider calls) reproduces `output.csv` **byte for byte**. The status accuracy on the 32 public samples stays at 32/32.

---

## 1. Output Contract: Final Check

Checks were run with an independent validator, not the project's own code.

| Check | Result |
|---|---|
| `output.csv` at the repository root, UTF-8, LF, no BOM | PASS |
| Header exactly `case_id,deletion_status,steps_json` | PASS |
| 518 data rows; no missing, extra or duplicate `case_id` | PASS |
| Rows sorted by `case_id` (natural order, same as the input file) | PASS |
| `deletion_status` ∈ {COMPLETE, INCOMPLETE, NOT_APPLICABLE} | PASS: 355 / 153 / 10 |
| `steps_json` is a valid JSON array, each step has exactly the 7 required keys | PASS |
| `step_no` starts at 1 and is consecutive | PASS |
| (phase, operation, state) triples are all allowed | PASS |
| Order: FINDING → REMEDIATION → VERIFICATION, then operation table order, `system_id`, `resource_id` | PASS (0 deviations) |
| `NOT_APPLICABLE` rows have `[]` | PASS (10/10) |
| `COMPLETE` rows: only VERIFICATION steps, each with a cited id | PASS |
| `INCOMPLETE` rows: every FINDING cites evidence; REMEDIATION and VERIFICATION use `""` | PASS |
| Every cited id exists and is dated at or before the case `audit_at` | PASS (0 unknown, 0 late) |
| No deletion row (COMPLETE or INCOMPLETE) has an empty plan | PASS (0 rows; previously 4) |
| Every REMEDIATION has a matching VERIFICATION; every FINDING has a REMEDIATION | PASS |
| Reproducibility: `python3 code/main.py` from cache with a blank key | PASS: identical file, 0 calls, 529 cached answers |
| Samples (`--cases sample`) against `sample_output.csv` | Status 32/32; exact `steps_json` 17/32 |
| Secrets: `.env` and `.llm_cache/` untracked; no key prefix in `code/`, `README.md`, `log.txt`, `tasks.md`, `output.csv`; the README `zip` command packages only `README.md` and `code/` | PASS |

---

## 2. Summary of Fixes

| Finding | Status | How it was addressed |
|---|---|---|
| **C1** 4 × `COMPLETE` + `[]` | **Fixed** | case_50 is now INCOMPLETE: `analytics_7438` with REPORT_EXPOSED_PII, citing `evidence_19`. case_53 is INCOMPLETE: `support_1034` with REPORT_EXPOSED_PII, citing `evidence_28`. case_61 is INCOMPLETE: `object_19` with REPORT_REMAINING_RECORD, citing `evidence_46`. case_63 is INCOMPLETE: `marketing_001` with REPORT_REMAINING_RECORD, citing `evidence_60`. |
| F1 "nothing judged → COMPLETE" | Fixed | `evaluator.apply_policy()`: COMPLETE now requires at least one proven-compliant resource, no violation and no unjudged resource. |
| F2 contract guard | Fixed | `planner.validate_plan()` accepts an empty plan only for INCOMPLETE. `main.validate_results()` raises on COMPLETE with no steps. `AuditPipeline.run_case()` downgrades COMPLETE with no steps to INCOMPLETE (three layers of protection). |
| F3 screenshot resource ids | Fixed | `multimodal_extractors.canonical_resource_id()` takes the canonical id from `linked_resources` or attributes and keeps the screen label in `display_id`. |
| F4 catalog links followed to evidence | Fixed | `Investigator._aliases` maps catalog labels (e.g. `object_19 (delivery_confirmation_19.pdf)`) to ids. Catalog-linked records skip the `_foreign()` check, and the catalog `activity_id` is added as supporting evidence. |
| F5 one-character OCR repair | Fixed | `reconcile_screenshot_emails()` corrects an email only when there is exactly one known address with one substituted character, the same domain and the same person name. The original reading is kept in `ocr_original_email`. |
| F6 post-`audit_at` files influencing state | **Not applied; documented** | The team kept reading rows stamped at or before `audit_at` from later files, because the public samples require it. These files are never cited. The reason is in `_usable()` and in the README "Known Limitations". Accepted, because sample accuracy depends on it. |
| F7 unprovable resources | Fixed | Unjudged resources now block COMPLETE. |
| F8 ambiguous identity aborting the run | Fixed | The resolver first breaks ties by who held the address at `requested_at`. A remaining ambiguity yields INCOMPLETE with `[]` instead of exit code 1. It does not occur in the current output. |
| F9 fingerprint heuristics | Documented | They are labelled HEURISTICS in `investigator.py` and listed in the README "Known Limitations". They run only when no grounded link exists. |
| F10 regression cases | Fixed | New custom cases in `custom_cases.py` cover the user-id title, the CS-xxxx title, the file name mapped via the catalog, and a catalog link shown under another name. The COMPLETE-needs-steps rule is enforced at runtime by `validate_plan` and `validate_results`. The `invariants.py` docstring was updated. |
| F11 packaging and secrets | Fixed (docs) | The README packaging command excludes `.env` and `.llm_cache/` and recommends rotating the key. |
| L6 fixed-step wording | Fixed | The `FIXED_VERIFICATIONS` comment now says the steps are added only when the system was found but not judged. |
| L7 row order | Unchanged (acceptable) | Natural order matches the input and sample files. |

Side effects outside the four target cases: only two other rows changed status.

- **case_43**: INCOMPLETE → COMPLETE. The account screenshot `evidence_04`, captured exactly at `audit_at` (inclusive), is now indexed under `account_8681` and shows it absent.
- **case_46**: COMPLETE → INCOMPLETE. The deleted-account reference screenshot `evidence_42` now resolves to `tombstone_17` and shows exposed PII.

Both changes follow from the new id normalisation and look consistent with the evidence. They cannot be checked against golden answers.

---

## 3. Remaining Risks (non-blocking)

1. **Step-level precision.** Statuses match 32/32 samples, but only 17/32 `steps_json` arrays match exactly. Partial credit depends on how the grader scores steps. The main known causes are evidence choice (which file to cite) and unlinked accounts or security records (see `tasks.md`, TASK-07 known limits).
2. **Heuristic attribution.** The second-of-minute fingerprint, the `reported_at` window and the new `_link_lone_anonymization` fallback depend on timing. They may misattribute records on the hidden set. They are documented but not validated beyond the samples.
3. **Catalog link overrides the on-screen owner (case_63).** `marketing_001` is attributed by the data catalog although the screenshot shows another person's name. This is a reasoned choice, but the hidden answer could treat it as a decoy.
4. **Later-captured files read for state (F6).** This is intentional and documented. A later snapshot can still describe the state before `audit_at`.
5. **OCR repair scope.** Only a single substituted character with matching name and domain is repaired. Insertions or deletions, and names misread by the vision model, are not handled.
6. **Ambiguous-identity path is untested on real data.** It returns INCOMPLETE with `[]`, which is valid but scores no steps. It does not occur in the current 518 rows.
7. **Operational.** The local `.env` holds a live Groq key. It is not in any tracked or submitted file, but it should be rotated after the event. `evaluation_report.md` and `usage_report.md` should be regenerated only from this final run so the numbers match `output.csv`.

## 4. Submission Checklist

- [x] `output.csv` (518 rows, valid, reproducible from cache)
- [x] `code/` with `README.md`, `code/evaluation/evaluation_report.md`, `code/evaluation/usage_report.md`
- [x] `log.txt` as the chat transcript
- [ ] Build `code.zip` with the README command, open it, and confirm there is no `.env`, `.llm_cache/`, `dataset/` or `__pycache__/`
- [ ] Submit at https://www.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists
