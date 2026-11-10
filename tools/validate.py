#!/usr/bin/env python3
"""Check a Relevance Explorer file and print what's in it, plus each run's scores.

    python3 tools/validate.py data/my-tests.json
    python3 tools/validate.py data/my-tests.json --by-tag

Exits with status 1 if the file can't be opened, 2 if it opens with problems (the same problems
the viewer lists), and 0 if it's clean. Scores are the viewer's: NDCG@k, MAP and MRR averaged
over the queries every run can be scored on.
"""
import argparse
import sys
from collections import Counter
from statistics import mean

from relevance_format import FormatError, load


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('file')
    ap.add_argument('--by-tag', action='store_true', help='also print NDCG for each tag')
    args = ap.parse_args()
    try:
        ts = load(args.file)
    except (FormatError, OSError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 1

    js = [j for m in ts.judgments.values() for j in m.values()]
    judged = [q for q in ts.queries if ts.judgments[q['id']]]
    print(f'{len(ts.queries)} queries: {len(judged)} with judgments, '
          f'{sum(1 for q in ts.queries if ts.scorable(q["id"]))} with a relevant result, '
          f'{len(ts.queries) - len(judged)} with none')
    grades = Counter(j['grade'] for j in js)
    print(f'{len(js)} judgments: ' + ', '.join(
        f'{g.get("label", g["value"])} ({g["value"]}) {grades[g["value"]]}' for g in ts.grades))
    by_judge = Counter(j.get('judge') or 'unknown' for j in js)
    if by_judge:
        print('graded by: ' + ', '.join(f'{w} {n}' for w, n in by_judge.most_common()))

    if ts.runs:
        scored = ts.scored_queries()
        print(f'\n{len(ts.runs)} runs, scored on {len(scored)} queries (k = {ts.k})')
        if scored:
            width = min(60, max(len(r['label']) for r in ts.runs))
            print(f'  {"run".ljust(width)}  NDCG@{ts.k}    MAP    MRR')
            for r in ts.runs:
                s = [ts.score(r, q) for q in scored]
                tag = '  (baseline)' if r['baseline'] else ''
                label = r['label'] if len(r['label']) <= width else r['label'][:width - 1] + '…'
                print(f'  {label.ljust(width)}  {mean(x["ndcg"] for x in s):7.3f} {mean(x["ap"] for x in s):6.3f} '
                      f'{mean(x["rr"] for x in s):6.3f}{tag}')
            if args.by_tag:
                print(f'\n  NDCG@{ts.k} by tag:')
                tags = list(dict.fromkeys(t for q in ts.queries for t in q['tags']))
                by_id = {q['id']: q for q in ts.queries}
                for t in tags:
                    qs = [q for q in scored if t in by_id[q]['tags']]
                    if qs:
                        print(f'  tag {t} ({len(qs)} queries): ' + ', '.join(
                            f'{r["label"]} {mean(ts.score(r, q)["ndcg"] for q in qs):.3f}' for r in ts.runs))

    if ts.warnings:
        print(f'\n{len(ts.warnings)} problem(s):')
        for w in ts.warnings:
            print(f'  - {w}')
        return 2
    print('\nNo problems found.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
