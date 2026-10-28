# Review Findings: Ghost Data Still Exists

Reviewer role: QA audit and code review (read-only; no implementation files were changed).
Scope: `AGENTS.md`, `problem_statement.md`, `README.md`, `code/**`, `output.csv`, `dataset/**`.
Method: I checked the contract of `output.csv` with a standalone script. I re-ran the pipeline on suspicious cases from the LLM cache only (blank API key, 0 provider calls). I also inspected the source and the raw evidence.

---

## 0. Summary

| Area | Result |
|---|---|
| Header `case_id,deletion_status,steps_json` | PASS |
| 518 rows, no missing, extra, or duplicate `case_id` | PASS |
| `deletion_status` values limited to the allowed set | PASS (351 INCOMPLETE, 157 COMPLETE, 10 NOT_APPLICABLE) |
| `steps_json` parses as a JSON array with exactly the 7 required keys | PASS |
| `step_no` starts at 1 and is consecutive | PASS |
| (phase, operation, state) triples are all allowed | PASS |
| Order: phase → operation table order → `system_id` → `resource_id` | PASS (0 rows deviate; the same check passes on `sample_output.csv`) |
| `NOT_APPLICABLE` rows have `[]` | PASS (10/10) |
| `COMPLETE` rows contain only VERIFICATION steps, each with evidence | PASS for non-empty rows |
| `INCOMPLETE` rows: every FINDING cites evidence; REMEDIATION and VERIFICATION use `""` | PASS |
| Every cited id exists and is dated at or before `audit_at` | PASS (0 unknown ids, 0 late citations) |
| Every REMEDIATION has a matching VERIFICATION; every FINDING has a REMEDIATION | PASS |
| **`COMPLETE` rows with an empty plan** | **FAIL: 4 rows (case_50, case_53, case_61, case_63)** |
| Secrets | `.env` is untracked and git-ignored. It still contains a live Groq key, so it must be kept out of `code.zip`. |

The output is syntactically valid and well ordered. The grader should accept the file. The main correctness defect is concentrated in screenshot-only subjects.

---

## 1. Critical Bugs and Contract Violations

### C1. Four deletion cases are reported `COMPLETE` with `steps_json = []`

| Case | User | Evidence that exists (≤ audit_at) | Why it was missed |
|---|---|---|---|
| case_50 | user_20 (darwish.omar@icloud.com) | `evidence_19` analytics screenshot (13 Jul 10:00) shows the identifiable profile `analytics_7438`. `activity_4003` catalog link. The ANONYMIZE activity FAILED. | The vision extractor used `user_20` as the `resource_id` instead of `analytics_7438`, although the header says "user_20 · analytics_7438". |
| case_53 | user_22 (kagyeman8947@fastmail.com) | `evidence_28` support screenshot shows `support_1034` with the email. `activity_4005` catalog link. | The vision extractor misread the email as `kagyeman88**4**7`, so no identity match occurred. The `linked_resources: [support_1034]` it did extract were never followed. Its `resource_id` is `CS-5034`, which does not match `_RESOURCE_ID_RE`. |
| case_61 | user_07 (priya.raman@gmail.com) | `evidence_46` object-storage screenshot shows `delivery_confirmation_19.pdf`. `activity_4007` says object_19 belongs only to this user. | The screenshot record uses the filename as `resource_id`. Nothing maps `delivery_confirmation_19.pdf` to `object_19`. |
| case_63 | user_09 (kwame-mensah@proton.me) | `evidence_60` marketing screenshot for `marketing_001`. `activity_4008` catalog link to this user. | The screenshot names a different person (Soo-jin Park). The catalog link is the only attribution, and the records for `marketing_001` were rejected by `_foreign()`. |

Why these four rows break the contract:

- `problem_statement.md` says that for `COMPLETE`, the agent must "include only verification steps **proving** the current state". An empty array proves nothing, so the grader will treat it as wrong for status and for steps.
- In at least case_50, case_53 and case_61, the evidence shows the data is still present. The correct status is `INCOMPLETE`, so these rows are false "Complete" claims. That is the failure mode the challenge is built around.

Root causes in the code:

1. [evaluator.py](code/evaluator.py) `apply_policy()`: when no resource can be judged, it logs "no resource could be judged; reporting COMPLETE" and returns `COMPLETE` because the violation list is empty. Absence of evidence is treated as proof of deletion.
2. [planner.py](code/planner.py) `validate_plan()` explicitly allows an empty plan for a deletion status. That bypasses `models.validate_steps`, so the guard in [main.py](code/main.py) `validate_results()` never fires.
3. [investigator.py](code/investigator.py) `_catalog_links()` adds `analytics_7438`, `support_1034`, `object_19` and `marketing_001` to `linked_ids`. However, `index.by_resource_id()` returns nothing for those ids, because the screenshot records were indexed under `user_20`, `CS-5034` and the filename. The catalog link is found and then lost.
4. [multimodal_extractors.py](code/multimodal_extractors.py) takes `resource_id` from the model answer without reconciling it against the canonical id pattern, the `linked_resources` list, or the screenshot header.

---

## 2. Logic and Edge-Case Gaps

### L1. Evidence captured after `audit_at` is used to decide state (contradicts README)
`Investigator._usable()` admits a record when its `observed_at` ≤ `audit_at`, even if the file's `captured_at` is later. Such files are not cited, but they can still change the decision. `README.md` states "nothing later than a case's `audit_at` is used or cited", so the code and README disagree.

This is a leakage risk: a later capture can reflect remediation or recreation the auditor could not have known about at `audit_at`. It is not a contract failure, but it can flip statuses.

### L2. Fingerprint heuristics fit to how the dataset was generated
- `_ANON_SECOND_OFFSETS = {40, 41, 42}` with `_ANON_MAX_DELAY = 6h` links anonymized analytics profiles to a subject by the *second-of-minute* offset from the account deletion.
- `_REPORT_WINDOW_BEFORE/AFTER` (−90 min / +20 min around `reported_at`) is used to link accounts.

These are not hardcoded labels, but they rely on timing patterns from how the data was generated rather than on participant-facing semantics. They are fragile on the hidden set and hard to defend in the AI interview. At minimum, they should be documented as heuristics in `README.md` and in the evaluation report, with a measured precision on the samples.

### L3. "Unknown" resources are silently dropped
`treatment_for()` returns `None` for `ObservedState.UNKNOWN`, and the resource disappears from the plan. When a linked resource has no readable state, the case can still become `COMPLETE` because of the remaining resources. A deletion that cannot be proven should not be reported `COMPLETE`.

### L4. Identity resolution: one ambiguous email fails the whole run
`IdentityResolver._find_user()` raises `AmbiguousIdentityError` when an email maps to more than one user. In `main.run_cases()`, any failed case makes the run exit with code 1, and no `output.csv` is written. On the hidden set, a single email reused across users would therefore produce no submission at all.

The rest of the historical-identity handling is sound:
- the email→user lookup respects `introduced_at ≤ audit_at`;
- values introduced after `audit_at` are excluded;
- the audit ids that are cited are only those at or before `audit_at`;
- hashes of historical emails are derived only when the snapshot provably uses `sha256(lower(email))`.

### L5. Vision extraction has no consistency check
For case_53, the model returned an email that differs by one digit from the identity table, and the case was lost. Image extraction is the only non-deterministic input, but nothing cross-checks its answers against:
- the canonical id patterns;
- `user_identities.csv` (e.g. edit distance ≤ 1 combined with a matching name);
- `LINK_RESOURCE_IDENTITY` activities.

### L6. Fixed verification steps are applied only partly
`FIXED_VERIFICATIONS` adds account `VERIFY_RECORD_ABSENT` and security `VERIFY_RECORD_PRESENT` only when the investigator found a resource of that system. That matches the samples: 8 of 22 samples have no security step. However, the docstring says "every deletion case is expected to verify" both. The wording should be corrected so that nobody later turns this into an unconditional rule.

### L7. Row sort order is natural, not lexicographic
`case_sort_key()` sorts `case_9` before `case_10`. A plain string sort would put `case_10` first. The input files use natural order, and graders normally join on `case_id`, so the risk is low. If the grader compares positions literally, this matters. It should be confirmed against `dataset/output.csv` (the template) and kept consistent with it.

### L8. Phase and operation ordering: no issues found
All 518 rows and all 32 samples match the canonical key (phase → operation table order → `system_id` → `resource_id`, string comparison).

---

## 3. Actionable Fixes

| # | Fix | Where | Detail |
|---|---|---|---|
| F1 | Never emit `COMPLETE` with an empty plan | `evaluator.apply_policy()` | If `not violations and not compliant`, return `INCOMPLETE` with no fabricated steps. Better, let F3 and F4 supply the missing resources. Remove the "reporting COMPLETE" fallback. |
| F2 | Restore the contract guard | `planner.validate_plan()` | Delete the early `return` for empty non-`NOT_APPLICABLE` plans. Make `main.validate_results()` raise `OutputValidationError` for any `COMPLETE` row with `[]`, so the regression cannot silently come back. |
| F3 | Normalize screenshot resource ids | `multimodal_extractors.py`, after parsing the model answer | If `resource_id` does not match `investigator._RESOURCE_ID_RE`, search the record's `linked_resources`, `attributes` and header text for a canonical id (`analytics_\d+`, `support_\d+`, `marketing_\d+`, …) and use it. For object-storage files, map `<name>_<n>.<ext>` to `object_<n>` only when a `LINK_RESOURCE_IDENTITY` activity names that filename (activity_4007 does: "object_19 (delivery_confirmation_19.pdf)"). Keep the original value in `fields["display_id"]`. |
| F4 | Follow catalog links to evidence | `investigator.investigate()` | For each id in `catalog_ids` with no records from `index.by_resource_id()`, search visible screenshot records whose `linked_resources`, `display_id` or text contains that id. Exempt records reached through an explicit catalog link from `_foreign()`, because the catalog link is the stronger attribution. Add the catalog `activity_id` to `support_ids` so the finding has a citable id. |
| F5 | Tolerate one-character vision errors | `multimodal_extractors.py` | When an extracted email does not exist in `user_identities.csv` or `user_audit_log.csv`, but exactly one known email is at edit distance 1 *and* the extracted name matches that user, correct it and set `fields["ocr_corrected"]=True`. Otherwise keep the original value. |
| F6 | Do not let post-`audit_at` files influence state | `investigator._usable()` | Also require `record.captured_at <= audit_at`. Re-run the sample evaluation. If sample accuracy drops, document the trade-off explicitly in README and the evaluation report instead of keeping the silent contradiction. |
| F7 | Treat unprovable resources as not compliant | `evaluator.treatment_for()` / `apply_policy()` | For a linked resource with `UNKNOWN` state whose rule action is `DELETE`, `REDACT` or `ANONYMIZE`, do not count the case as `COMPLETE`. At minimum, record it in the assessment and in the evaluation report. |
| F8 | Make ambiguous identities non-fatal | `main.AuditPipeline.run_case()` | Catch `AmbiguousIdentityError`. Prefer the owner whose email was active at `requested_at`. If it is still ambiguous, emit `INCOMPLETE` from the union of candidates instead of aborting the batch. |
| F9 | Document or remove fingerprint heuristics | `investigator.py` (`_ANON_SECOND_OFFSETS`, `_REPORT_WINDOW_*`) | Prefer linking through participant-visible relationships (catalog links, `downstream resource` details, shared `user_id` tombstone). If kept, add their measured precision and recall to `evaluation_report.md`. |
| F10 | Add regression cases | `code/evaluation/custom_cases.py` | Add custom cases for: (a) a screenshot-only subject reached by catalog link; (b) a one-digit OCR email error; (c) a file captured after `audit_at` that would change the state. Add a contract invariant to `invariants.py`: deletion statuses never have an empty plan. |
| F11 | Packaging hygiene | Release step | Build `code.zip` from tracked files only (`git archive`). Confirm `.env` is absent. Rotate the Groq key that is now stored in the local `.env`. |

### Expected impact
- F1, F2: remove the 4 invalid `COMPLETE + []` rows (contract-level wrongness).
- F3, F4, F5: should convert case_50, case_53 and case_61 to `INCOMPLETE` with correct findings. Case_63 needs a decision on catalog link vs. screenshot owner (see F4).
- F6, F8: reduce hidden-set risk (leakage, and an aborted run).

---

## 4. Verification Checklist after Fixes

1. Run `python3 code/main.py` and confirm it exits with code 0 and writes 518 rows.
2. Run the contract script: 0 issues, including "deletion status with empty plan".
3. Run `python3 code/evaluation/main.py`: sample status accuracy must not drop below the current value. Regenerate `evaluation_report.md` and `usage_report.md`.
4. Re-check case_50, case_53, case_61 and case_63 by hand against `evidence_19`, `evidence_28`, `evidence_46`, `evidence_60` and activities `activity_4003`, `activity_4005`, `activity_4007`, `activity_4008`.
5. Confirm a cached second run makes 0 provider calls.
