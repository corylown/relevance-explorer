#!/usr/bin/env python3
"""Write a copy of the viewer with a data file built in, so it can be shared as one HTML file.

    python3 embed.py data.json out.html

The data is checked only for being valid JSON; the page itself reports any format problems.
"""
import json
import pathlib
import sys

SLOT = '<script type="application/json" id="re-data"></script>'

if len(sys.argv) != 3:
    sys.exit(__doc__.strip())
data_path, out_path = map(pathlib.Path, sys.argv[1:])
viewer = (pathlib.Path(__file__).parent / 'index.html').read_text(encoding='utf-8')
if SLOT not in viewer:
    sys.exit('index.html has no data slot; was it edited?')
data = json.loads(data_path.read_text(encoding='utf-8'))
# Compact, and write every "<" as < (JSON.parse reads it back as "<"), so text like
# "</script>" or "<!--<script>" in the data can't end or derail the script tag it sits in.
payload = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
out_path.write_text(viewer.replace(SLOT, SLOT.replace('></', f'>{payload}</')), encoding='utf-8')
print(f'wrote {out_path} ({out_path.stat().st_size // 1024} KB)')
