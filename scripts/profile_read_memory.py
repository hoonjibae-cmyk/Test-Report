"""Profile /read's worker in a fresh process using a non-sensitive scan fixture.

Example (Linux/macOS):
    python scripts/profile_read_memory.py scan.png template.json --iterations 10
Fixture creation must happen in a separate process so its peak is not counted.
No scans, student IDs, or answers are printed. Use synthetic input for sharing.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import resource
import sys
import tempfile
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('image')
parser.add_argument('template')
parser.add_argument('--iterations', type=int, default=3)
parser.add_argument('--repo', default=str(Path(__file__).resolve().parents[1]))
args = parser.parse_args()
sys.path.insert(0, str(Path(args.repo).resolve()))

from omr_api.main import _read_all  # noqa: E402
from omr.reader import ReadParams  # noqa: E402
import cv2  # noqa: E402

with open(args.template, encoding='utf-8') as source:
    template = source.read()

for iteration in range(args.iterations):
    start = time.perf_counter()
    with tempfile.TemporaryDirectory() as directory:
        results, problems = _read_all(
            [('synthetic.png', args.image)], directory, args.template,
            ReadParams(), True, True, template,
        )
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_mib = peak / (1024 * 1024 if sys.platform == 'darwin' else 1024)
    print(json.dumps({
        'iteration': iteration + 1,
        'peak_rss_mib': round(peak_mib, 1),
        'seconds': round(time.perf_counter() - start, 3),
        'results': len(results), 'problems': len(problems),
        'opencv': cv2.__version__, 'opencv_threads': cv2.getNumThreads(),
    }), flush=True)
    # Do not retain the previous response while building the next one.
    del results, problems
