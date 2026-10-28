# ➕ Adding a new hackathon

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
