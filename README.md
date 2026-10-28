# Hackathons

[![Hackathons](https://img.shields.io/badge/Hackathons-1-informational?style=flat&logo=devpost&logoColor=white&color=00ADD8)](#-participations)
[![Wins](https://img.shields.io/badge/Wins-0-informational?style=flat&logo=hackthebox&logoColor=white&color=F5A623)](#-trophy-case)
[![Latest](https://img.shields.io/badge/Latest-HackerRank%20Orchestrate-informational?style=flat&logo=hackerrank&logoColor=white&color=2EC866)](#hackerrank-orchestrate--october-2026)
[![Python](https://img.shields.io/badge/Language-Python-informational?style=flat&logo=python&logoColor=white&color=3776AB)](hackerrank-orchestrate-october26)
[![Method](https://img.shields.io/badge/Method-Spec--Driven-informational?style=flat&color=8333D4)](hackerrank-orchestrate-october26/tasks.md)

A collection of every hackathon I take part in: the challenge, my solution, and how it went. Each project lives in its own folder with its full commit history.

[![HackerRank Orchestrate badge](assets/orchestrate-oct26-badge.jpg)](#hackerrank-orchestrate--october-2026)

## 🏆 Trophy case

| 🥇 Wins | 🥈 Podiums | 🎟️ Participations | 📦 Projects shipped |
| :---: | :---: | :---: | :---: |
| **0** | **0** | **1** | **1** |

> This section updates as results come in. Wins and podiums will show up here with their medals.

## 🎟️ Participations

| Date | Hackathon | Project | Stack | Result |
| --- | --- | --- | --- | --- |
| Oct 2026 | [HackerRank Orchestrate, October Edition](#hackerrank-orchestrate--october-2026) | [Ghost Data Still Exists, deletion-audit agent](hackerrank-orchestrate-october26) | ![Python](https://img.shields.io/badge/-Python-3776AB?style=flat&logo=python&logoColor=white) ![LLM](https://img.shields.io/badge/-LLM-8333D4?style=flat) ![SDD](https://img.shields.io/badge/-SDD-informational?style=flat) | ![Submitted](https://img.shields.io/badge/Submitted-success?style=flat) Results pending |

## 🗺️ Timeline

```mermaid
timeline
    title Hackathon journey
    2026 Oct : HackerRank Orchestrate : Deletion-audit agent
```

---

## HackerRank Orchestrate · October 2026

![Participant](https://img.shields.io/badge/Role-Participant-informational?style=flat&color=2EC866)
![Python](https://img.shields.io/badge/Python-3.10%2B-informational?style=flat&logo=python&logoColor=white&color=3776AB)
![Dependencies](https://img.shields.io/badge/Dependencies-stdlib%20only-informational?style=flat&color=brightgreen)
![Output](https://img.shields.io/badge/output.csv-518%20rows-informational?style=flat&color=00ADD8)
![Accuracy](https://img.shields.io/badge/Sample%20status%20accuracy-32%2F32-informational?style=flat&color=success)

**Challenge: _Ghost Data Still Exists._** Northstar Shop, an imaginary company, lost **$2.4 million** after one supposedly deleted customer came back as a ghost: every dashboard said **Complete**, yet the email was still registered. The task was to build an AI agent that proves whether a user's personal data is truly gone, from evidence and not from workflow claims.

**Solution: a deletion-audit agent.** For every request in the dataset, the agent:

1. decides whether the request text actually **authorizes deletion** (any language);
2. resolves the subject's **current and historical** identifiers;
3. reconstructs the **true state at `audit_at`** of every linked record, data flow and backup from SQL snapshots, JSON, logs, text records, PDFs and screenshots;
4. applies `deletion_rules.csv` (delete, redact, anonymize, retain, account tombstone);
5. returns `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE` with an ordered `FINDING → REMEDIATION → VERIFICATION` plan.

### Highlights

| Decision | Why |
| --- | --- |
| **Deterministic core, narrow LLM use** | The model only classifies request intent and reads screenshots; SQL, JSON, logs, PDFs, identity, policy and planning are plain Python |
| **Claims are not proof** | `reported_status` is never consulted, and a `SUCCESS` activity never sets a resource state on its own |
| **Strict time cutoff** | Nothing later than a case's `audit_at` is used to decide it or cited |
| **Reproducible** | Every accepted LLM answer is cached on disk, so a warm run makes zero provider calls |
| **Contract-safe writes** | `output.csv` is validated and written atomically; a failed run never leaves a partial file |

### How it was built

The project follows **Spec-Driven Development**: [`tasks.md`](hackerrank-orchestrate-october26/tasks.md) fixes module boundaries, contracts and acceptance criteria before any code.

```mermaid
flowchart LR
  A[parser<br/>+ evidence extractors<br/>+ multimodal] --> B[identity_resolver]
  A --> C[investigator]
  B --> C
  C --> D[evaluator]
  D --> E[planner]
  E --> F[main → output.csv]
  L[llm<br/>cache + usage] -.-> A
  L -.-> D
```

### Results

| Metric | Value |
| --- | --- |
| Status accuracy on the 32 public samples | **100%** (32/32) |
| Step F1 (identity) | 93.3% |
| Custom cases | 11/11 pass |
| Full run (518 requests) | `COMPLETE` 153 · `INCOMPLETE` 355 · `NOT_APPLICABLE` 10 |
| Cost | about $0.07 in total, around $0.000135 per request |

### Run it

```bash
cd hackerrank-orchestrate-october26
python3 code/main.py        # audits all 518 requests and writes output.csv
```

### Links

[![Project folder](https://img.shields.io/badge/Project-folder-informational?style=flat&logo=github&color=181717)](hackerrank-orchestrate-october26)
[![Full README](https://img.shields.io/badge/Docs-Full%20README-informational?style=flat&color=2EC866)](hackerrank-orchestrate-october26/README.md)
[![Problem statement](https://img.shields.io/badge/Docs-Problem%20statement-informational?style=flat&color=00ADD8)](hackerrank-orchestrate-october26/problem_statement.md)
[![Evaluation report](https://img.shields.io/badge/Docs-Evaluation%20report-informational?style=flat&color=8333D4)](hackerrank-orchestrate-october26/code/evaluation/evaluation_report.md)

---

## 📁 Repository structure

```text
hackathons/
├── assets/                          # badges and images for the README
├── hackerrank-orchestrate-october26/  # HackerRank Orchestrate · Oct 2026 (full history kept)
└── README.md
```

## ➕ Adding a new hackathon

1. Import the repo keeping its history: `git subtree add --prefix=<folder> <repo-url> main`
2. Save the badge or certificate in `assets/`.
3. Add a row to the **Participations** table and update the counters in the badges and **Trophy case**.

---

<sub>Built by [Valentina Varela](https://github.com/Valentina17varela)</sub>
