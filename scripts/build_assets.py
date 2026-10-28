#!/usr/bin/env python3
"""Regenerates assets/podium.svg and assets/carousel.svg from hackathons.json.

Usage (from the repo root):  python3 scripts/build_assets.py
Needs Pillow:                pip install pillow

hackathons.json is a list; each item looks like:
  {"name": "...", "image": "assets/xxx.jpg", "place": null}   # place: 1, 2, 3 or null
"""
import base64
import io
import json
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
DATA = json.loads((ROOT / "hackathons.json").read_text())

# ---------------------------------------------------------------- podium
wins = sum(1 for h in DATA if h.get("place") == 1)
second = sum(1 for h in DATA if h.get("place") == 2)
third = sum(1 for h in DATA if h.get("place") == 3)
total = len(DATA)


def cup(cx, base_y, color, light, dark, scale=1.0):
    """A flat trophy cup centred on cx, standing on base_y."""
    s = scale
    top = base_y - 92 * s
    return f"""
  <g stroke-linejoin="round" stroke-linecap="round">
    <path d="M{cx-34*s} {top+10*s} q{-26*s} 0 {-24*s} {22*s} q{3*s} {20*s} {30*s} {26*s}" fill="none" stroke="{dark}" stroke-width="{7*s}"/>
    <path d="M{cx+34*s} {top+10*s} q{26*s} 0 {24*s} {22*s} q{-3*s} {20*s} {-30*s} {26*s}" fill="none" stroke="{dark}" stroke-width="{7*s}"/>
    <rect x="{cx-36*s}" y="{top}" width="{72*s}" height="{8*s}" rx="{2*s}" fill="{dark}"/>
    <path d="M{cx-34*s} {top+8*s} h{68*s} q0 {52*s} {-26*s} {58*s} h{-16*s} q{-26*s} {-6*s} {-26*s} {-58*s}z" fill="{color}"/>
    <path d="M{cx-34*s} {top+8*s} h{22*s} q{-4*s} {50*s} {10*s} {58*s} h{-6*s} q{-26*s} {-6*s} {-26*s} {-58*s}z" fill="{light}" opacity=".55"/>
    <rect x="{cx-5*s}" y="{top+64*s}" width="{10*s}" height="{14*s}" fill="{color}"/>
    <rect x="{cx-20*s}" y="{top+78*s}" width="{40*s}" height="{8*s}" rx="{2*s}" fill="{dark}"/>
    <rect x="{cx-24*s}" y="{top+86*s}" width="{48*s}" height="{6*s}" rx="{2*s}" fill="#7a4a1d"/>
  </g>"""


def block(x, y, w, h, label):
    return f"""
  <rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#7fb6dc"/>
  <rect x="{x}" y="{y}" width="{w}" height="8" fill="#5f9cc6"/>
  <text x="{x+w/2}" y="{y+h/2+22}" text-anchor="middle" font-size="64" font-weight="800" fill="#ffffff" fill-opacity=".9">{label}</text>"""


def count(cx, y, n, caption):
    return f"""
  <text x="{cx}" y="{y}" text-anchor="middle" font-size="26" font-weight="700" fill="#8b949e">{n}</text>
  <text x="{cx}" y="{y+18}" text-anchor="middle" font-size="13" fill="#8b949e" letter-spacing="1">{caption}</text>"""


W, H = 720, 330
ground = 296
podium = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">
  <title>Podium: {wins} first, {second} second, {third} third, {total} participations</title>
  <!-- 2nd -->{block(150, ground-90, 150, 90, "2")}{cup(225, ground-90, "#aab2bd", "#d8dde3", "#7c8591")}{count(225, ground-205, second, "2ND PLACE")}
  <!-- 1st -->{block(300, ground-130, 150, 130, "1")}{cup(375, ground-130, "#fcd34d", "#fff0a8", "#f5a623", 1.15)}{count(375, ground-255, wins, "1ST PLACE")}
  <!-- 3rd -->{block(450, ground-60, 150, 60, "3")}{cup(525, ground-60, "#ee6a35", "#f7a07c", "#b8431d")}{count(525, ground-175, third, "3RD PLACE")}
  <rect x="120" y="{ground}" width="510" height="3" rx="1.5" fill="#8b949e" opacity=".35"/>
  <text x="{W/2}" y="{H-8}" text-anchor="middle" font-size="15" fill="#8b949e" letter-spacing="2">{total} PARTICIPATION{'S' if total != 1 else ''}</text>
</svg>
"""
(ROOT / "assets" / "podium.svg").write_text(podium)

# -------------------------------------------------------------- carousel
CW, CH = 420, 315          # slide box (the original images are 4:3)
SHOW, FADE = 4.0, 0.8      # seconds each slide is shown / cross-fade
imgs = []
for h in DATA:
    if not h.get("image"):
        continue
    im = Image.open(ROOT / h["image"]).convert("RGB")
    im.thumbnail((CW * 2, CH * 2))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80, optimize=True)
    imgs.append((h["name"], base64.b64encode(buf.getvalue()).decode()))

n = len(imgs)
cycle = n * SHOW
slides, css = [], []
for i, (name, b64) in enumerate(imgs):
    slides.append(
        f'<g class="s s{i}"><image href="data:image/jpeg;base64,{b64}" width="{CW}" height="{CH}" '
        f'preserveAspectRatio="xMidYMid slice"/>'
        f'<rect y="{CH-34}" width="{CW}" height="34" fill="#000" fill-opacity=".55"/>'
        f'<text x="{CW/2}" y="{CH-12}" text-anchor="middle" font-size="14" fill="#fff">{name}</text></g>'
    )
    if n > 1:
        a = i * SHOW / cycle * 100
        b = (i * SHOW + FADE) / cycle * 100
        c = ((i + 1) * SHOW) / cycle * 100
        d = ((i + 1) * SHOW + FADE) / cycle * 100
        if i == 0:
            frames = f"0%{{opacity:1}} {c:.2f}%{{opacity:1}} {d:.2f}%{{opacity:0}} {100-FADE/cycle*100:.2f}%{{opacity:0}} 100%{{opacity:1}}"
        else:
            frames = f"0%,{a:.2f}%{{opacity:0}} {b:.2f}%{{opacity:1}} {c:.2f}%{{opacity:1}} {min(d,100):.2f}%{{opacity:0}} 100%{{opacity:0}}"
        css.append(f"@keyframes k{i}{{{frames}}} .s{i}{{animation:k{i} {cycle}s linear infinite}}")
    else:
        css.append(f".s{i}{{opacity:1}}")

dots = ""
if n > 1:
    for i in range(n):
        x = CW / 2 - (n - 1) * 8 + i * 16
        a = i * SHOW / cycle * 100
        c = (i + 1) * SHOW / cycle * 100
        css.append(
            f"@keyframes d{i}{{0%,{a:.2f}%{{opacity:.35}} {a+0.01:.2f}%,{c:.2f}%{{opacity:1}} {min(c+0.01,100):.2f}%,100%{{opacity:.35}}}} .d{i}{{animation:d{i} {cycle}s linear infinite}}"
        )
        dots += f'<circle class="d{i}" cx="{x}" cy="{CH-46}" r="4" fill="#fff"/>'

carousel = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CW} {CH}" width="{CW}" height="{CH}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">
<defs><clipPath id="r"><rect width="{CW}" height="{CH}" rx="12"/></clipPath></defs>
<style>{' '.join(css)}</style>
<g clip-path="url(#r)">{''.join(slides)}{dots}</g>
</svg>
"""
(ROOT / "assets" / "carousel.svg").write_text(carousel)
print(f"podium: {wins}/{second}/{third} of {total} · carousel: {n} slide(s)")
