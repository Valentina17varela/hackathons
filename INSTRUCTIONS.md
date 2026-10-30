# ➕ Adding a new hackathon

1. Import the project keeping its commit history:

   ```bash
   git subtree add --prefix=<folder-name> <repo-url> main
   ```

2. Save its badge or certificate image in `assets/`.
3. Add the participation to `hackathons.json`. `place` is `1`, `2`, `3` or `null` when there is no podium finish:

   ```json
   { "name": "Hackathon name · Month Year", "place": null }
   ```

4. Add its badge, certificate, or results images to `gallery.json`:

   ```json
   { "name": "Hackathon name · Results", "image": "assets/my-results.png" }
   ```

   Images are optimized as static JPEGs; GIFs use only their first frame.

5. Regenerate the podium and the carousel (needs `pip install pillow` once):

   ```bash
   python3 scripts/build_assets.py
   ```

6. Add a row to the **Participations** table, update the badge counters at the top, then commit and push.
