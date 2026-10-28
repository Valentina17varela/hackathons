# Hackathons

[![Hackathons](https://img.shields.io/badge/Hackathons-1-informational?style=flat&logo=devpost&logoColor=white&color=00ADD8)](#-participations)
[![Wins](https://img.shields.io/badge/Wins-0-informational?style=flat&logo=hackthebox&logoColor=white&color=F5A623)](#-trophy-case)
![Latest](https://img.shields.io/badge/Latest-HackerRank%20Orchestrate-informational?style=flat&logo=hackerrank&logoColor=white&color=2EC866)

A collection of every hackathon I take part in: the challenge, my solution, and how it went. Each project lives in its own folder with its full commit history.

## 🏆 Podium

<p align="center">
  <img src="assets/podium.svg" alt="Podium: 0 first places, 0 second places, 0 third places, 1 participation" width="620">
</p>

## 📸 Gallery

<p align="center">
  <img src="assets/carousel.svg" alt="Hackathon badges carousel" width="420">
</p>

## 🎟️ Participations

| Date | Hackathon | Project | Stack | Result |
| --- | --- | --- | --- | --- |
| Oct 2026 | [HackerRank Orchestrate, October Edition](#hackerrank-orchestrate--october-2026) | [Ghost Data Still Exists, deletion-audit agent](hackerrank-orchestrate-october26) | ![Python](https://img.shields.io/badge/-Python-3776AB?style=flat&logo=python&logoColor=white) ![LLM](https://img.shields.io/badge/-LLM-8333D4?style=flat) ![SDD](https://img.shields.io/badge/-SDD-informational?style=flat) | ![Submitted](https://img.shields.io/badge/Submitted-success?style=flat) Results pending |

## ➕ Adding a new hackathon

1. Import the project keeping its commit history:

   ```bash
   git subtree add --prefix=<folder-name> <repo-url> main
   ```

2. Save its badge or certificate image in `assets/`.
3. Add an entry to `hackathons.json`. `place` is `1`, `2`, `3` or `null` when there is no podium finish:

   ```json
   { "name": "Hackathon name · Month Year", "image": "assets/my-badge.jpg", "place": null }
   ```

4. Regenerate the podium and the carousel (needs `pip install pillow` once):

   ```bash
   python3 scripts/build_assets.py
   ```

5. Add a row to the **Participations** table, update the badge counters at the top, then commit and push.
