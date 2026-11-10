#!/usr/bin/env python3
"""Build a Relevance Explorer file from TREC-style files: topics, qrels and run files.

    python3 tools/adapters/trec_to_json.py --topics topics.tsv --qrels qrels.txt \
        --run main=runs/main.txt --run hybrid=runs/hybrid.txt --baseline main --out data/tests.json

- topics: one query per line, "<query id><TAB><query text>".
- qrels: "<query id> <ignored> <doc id> <grade>" per line (the standard trec_eval format).
  Use --judge to name who made these grades (default "qrels").
- each --run is LABEL=PATH, in the standard run format "<query id> Q0 <doc id> <rank> <score> <tag>".
  Results are ordered by rank, and by score when ranks tie. Every topic gets a results list, empty
  if the run has no lines for it, so queries that returned nothing still count.
"""
import argparse
import json
import sys


def read_topics(path):
    queries = []
    for line in open(path, encoding='utf-8'):
        if line.strip():
            qid, _, text = line.rstrip('\n').partition('\t')
            queries.append({'id': qid.strip(), 'text': text.strip() or qid.strip()})
    return queries


def read_qrels(path, judge):
    out = []
    for n, line in enumerate(open(path, encoding='utf-8'), 1):
        parts = line.split()
        if not parts:
            continue
        if len(parts) < 4:
            sys.exit(f'{path} line {n}: expected "<query> <ignored> <doc> <grade>"')
        grade = float(parts[3])
        out.append({'query': parts[0], 'doc': parts[2], 'grade': int(grade) if grade.is_integer() else grade, 'judge': judge})
    return out


def read_run(path, query_ids):
    rows = {}
    for n, line in enumerate(open(path, encoding='utf-8'), 1):
        parts = line.split()
        if not parts:
            continue
        if len(parts) < 5:
            sys.exit(f'{path} line {n}: expected "<query> Q0 <doc> <rank> <score> <tag>"')
        rows.setdefault(parts[0], []).append((int(parts[3]), -float(parts[4]), parts[2]))
    return {q: [d for _, _, d in sorted(rows.get(q, []))] for q in query_ids}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--topics', required=True)
    ap.add_argument('--qrels')
    ap.add_argument('--judge', default='qrels')
    ap.add_argument('--run', action='append', default=[], help='LABEL=PATH, repeatable')
    ap.add_argument('--baseline', help='label of the run to compare the others against')
    ap.add_argument('--title', default='Relevance tests')
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    queries = read_topics(args.topics)
    ids = [q['id'] for q in queries]
    data = {'title': args.title, 'queries': queries, 'judgments': read_qrels(args.qrels, args.judge) if args.qrels else []}
    runs = []
    for spec in args.run:
        label, sep, path = spec.partition('=')
        if not sep:
            sys.exit(f'--run needs LABEL=PATH, got {spec!r}')
        run = {'id': label, 'label': label, 'results': read_run(path, ids)}
        if label == args.baseline:
            run['baseline'] = True
        runs.append(run)
    if args.baseline and not any(r.get('baseline') for r in runs):
        sys.exit(f'--baseline {args.baseline!r} is not one of the --run labels')
    if runs:
        data['runs'] = runs
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f'wrote {args.out}: {len(queries)} queries, {len(data["judgments"])} judgments, {len(runs)} runs')


if __name__ == '__main__':
    main()
