#!/usr/bin/env python3
"""Show where and why a baseline setup ranks badly, as evidence for choosing what to try next.

    python3 tools/diagnose.py data/tests.json --baseline main
    python3 tools/diagnose.py data/tests.json --baseline main --solr-config data/solr-config.json

For each scored query, a "problem" is a document graded --min-grade or better (default: the top
two grades) that the baseline leaves out of its top k. Problems are labelled:

  buried          in the baseline's list, but below k
  not in list     not in the saved list at all

With --solr-config (the config solr_runs.py uses; --setup picks the setup, default the baseline),
each problem document is also checked against Solr, one small query per check:

  matches, ranked low      the baseline query matches it; it just scores below the others
  blocked by mm            it matches only when one query term is enough (mm=1)
  terms only in unsearched fields
                           query terms match it, but only in fields the setup doesn't search
  no query terms           none of the query's terms match it in any field checked:
                           a vocabulary gap, or text missing from the index
  filtered out             a searched field matches, but the query still never returns it
  fields                   which searched fields (qf) contain a query term, and which other
                           fields listed in the config's text_fields do but aren't searched

The script reports evidence only; deciding what to change is up to you. --json writes everything
for further analysis. Only the Python standard library is needed.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from statistics import mean

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relevance_format import FormatError, load  # noqa: E402


class SolrError(Exception):
    pass


class Probe:
    """Asks Solr why a document does or doesn't match a query under one setup's parameters."""

    def __init__(self, cfg, setup, probe_fields=()):
        self.url, self.id_field = cfg['select_url'], cfg.get('id_field', 'id')
        self.base = {**cfg.get('common_params', {}), **setup.get('params', {})}
        self.extra_fields = [f for f in [cfg.get('title_field'), *cfg.get('text_fields', []), *probe_fields] if f]
        self.calls = 0

    def get(self, params):
        self.calls += 1
        pairs = [(k, str(x)) for k, v in {**params, 'wt': 'json'}.items() for x in (v if isinstance(v, list) else [v])]
        data = urllib.parse.urlencode(pairs).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(self.url, data), timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                msg = json.load(e).get('error', {}).get('msg') or e.reason
            except ValueError:
                msg = e.reason
            raise SolrError(f'Solr answered {e.code}: {msg}') from e
        except urllib.error.URLError as e:
            raise SolrError(f"Couldn't reach {self.url}: {e.reason}") from e

    def effective(self, text):
        """The parameters Solr actually uses for this query, request handler defaults included."""
        r = self.get({**self.base, 'q': text, 'rows': 0, 'echoParams': 'all'})
        return r['responseHeader'].get('params', {})

    def matches(self, text, doc, **override):
        p = {**self.base, 'q': text, 'rows': 0, 'fq': self._with_fq(f'{{!term f={self.id_field}}}{doc}'), **override}
        return self.get(p)['response']['numFound'] > 0

    def _with_fq(self, fq):
        existing = self.base.get('fq', [])
        return (existing if isinstance(existing, list) else [existing]) + [fq]

    def explain(self, text, doc, qf_fields):
        hits = {f: self.matches(text, doc, defType='edismax', qf=f, mm='1', pf='', pf2='', pf3='', bq='', bf='', boost='')
                for f in dict.fromkeys(qf_fields + self.extra_fields)}
        if self.matches(text, doc):
            status = 'matches, ranked low'
        elif not any(hits[f] for f in qf_fields):
            status = 'terms only in unsearched fields' if any(hits.values()) else 'no query terms'
        elif self.matches(text, doc, mm='1'):
            status = 'blocked by mm'
        else:
            status = 'filtered out'  # terms match a searched field, yet the query never returns it (an fq?)
        return status, hits

def qf_fields(params):
    """Field names from a qf/pf string like "title_tsim^10 all_text_timv"."""
    return [re.sub(r'\^.*$', '', f) for f in str(params.get('qf', '')).split() if f]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('file')
    ap.add_argument('--baseline', help='run id to diagnose (default: the run marked as baseline)')
    ap.add_argument('--min-grade', type=float, help='lowest grade that counts as a document the query should find '
                    '(default: the second-highest grade on the scale)')
    ap.add_argument('--show', type=int, default=15, help='how many of the weakest queries to detail (default 15)')
    ap.add_argument('--solr-config', help='solr_runs.py config, to check each problem document against Solr')
    ap.add_argument('--setup', help='setup id in the config whose parameters to probe (default: --baseline)')
    ap.add_argument('--probe-fields', type=lambda s: [f for f in s.split(',') if f], default=[],
                    help='more fields to check for query terms, comma-separated (identifier fields like doi_ssi, say)')
    ap.add_argument('--json', help='also write the full diagnosis here')
    args = ap.parse_args()
    try:
        ts = load(args.file)
    except (FormatError, OSError) as e:
        sys.exit(f'error: {e}')
    if not ts.runs:
        sys.exit('error: the file has no runs; diagnose needs a graded baseline run')
    base = next((r for r in ts.runs if r['id'] == args.baseline), None) if args.baseline \
        else next((r for r in ts.runs if r['baseline']), ts.runs[0])
    if base is None:
        sys.exit(f'error: no run {args.baseline!r}; runs: {", ".join(r["id"] for r in ts.runs)}')
    positive = sorted({g['value'] for g in ts.grades if g['value'] > 0}, reverse=True)
    min_grade = args.min_grade if args.min_grade is not None else (positive[1] if len(positive) > 1 else positive[0])

    probe = setup = None
    if args.solr_config:
        cfg = json.load(open(args.solr_config, encoding='utf-8'))
        sid = args.setup or base['id']
        setup = next((s for s in cfg['setups'] if s['id'] == sid), None)
        if setup is None:
            sys.exit(f'error: no setup {sid!r} in {args.solr_config}; setups: {", ".join(s["id"] for s in cfg["setups"])}')
        probe = Probe(cfg, setup, args.probe_fields)

    k = ts.k
    scored = [q for q in ts.queries if q['id'] in set(ts.scored_queries())] or \
             [q for q in ts.queries if ts.scorable(q['id']) and q['id'] in base['results']]
    others = sorted((r for r in ts.runs if r is not base), key=lambda r: not r['featured'])
    rows = []
    for q in scored:
        qid, judged = q['id'], ts.judgments[q['id']]
        ranked = base['results'].get(qid, [])
        top = ranked[:k]
        want = {d: j['grade'] for d, j in judged.items() if j['grade'] >= min_grade}
        problems = []
        for d, g in sorted(want.items(), key=lambda x: -x[1]):
            if d in top:
                continue
            rank = ranked.index(d) + 1 if d in ranked else None
            problems.append({'doc': d, 'grade': g, 'rank': rank, 'title': (ts.raw.get('docs', {}).get(d) or {}).get('title', ''),
                             'status': f'buried (rank {rank})' if rank else 'not in list'})
        top_grades = [judged[d]['grade'] if d in judged else None for d in top]
        rows.append({
            'query': qid, 'text': q['text'], 'tags': q['tags'], 'ndcg': ts.score(base, qid)['ndcg'],
            'results': len(ranked), 'top_relevant': sum(1 for g in top_grades if g is not None and g >= min_grade),
            'top_not_relevant': sum(1 for g in top_grades if g is not None and g <= 0),
            'top_ungraded': sum(1 for g in top_grades if g is None), 'problems': problems,
            'missing_lower_grades': sum(1 for d, j in judged.items() if 0 < j['grade'] < min_grade and d not in top),
            'other_runs': {r['label']: ts.score(r, qid)['ndcg'] for r in others if qid in r['results']},
        })

    if probe:
        try:
            for row in rows:
                if not row['problems']:
                    continue
                eff = probe.effective(row['text'])
                row['effective'] = {p: ' '.join(str(eff[p]).split()) for p in ('defType', 'qf', 'mm', 'pf') if eff.get(p) is not None}
                fields = qf_fields(eff)
                for pr in row['problems']:
                    status, hits = probe.explain(row['text'], pr['doc'], fields)
                    if not pr['rank']:
                        pr['status'] = status
                    else:
                        pr['solr'] = status
                    pr['fields_with_terms'] = [f for f, hit in hits.items() if hit]
                    pr['unsearched_fields_with_terms'] = [f for f, hit in hits.items() if hit and f not in fields]
        except SolrError as e:
            sys.exit(f'error: {e}')

    # ---------- report ----------
    scores = [r['ndcg'] for r in rows]
    print(f'Baseline "{base["label"]}" on {len(rows)} scored queries: NDCG@{k} {mean(scores):.3f}')
    print(f'  {sum(s >= 0.9 for s in scores)} good (>= 0.9), {sum(0.6 <= s < 0.9 for s in scores)} middling, '
          f'{sum(s < 0.6 for s in scores)} poor (< 0.6); {sum(r["results"] == 0 for r in rows)} with no results')
    probs = [p for r in rows for p in r['problems']]
    print(f'  {len(probs)} documents graded {min_grade:g}+ missing from the top {k}, in '
          f'{sum(1 for r in rows if r["problems"])} queries:')
    for status, n in Counter(re.sub(r' \(rank \d+\)', '', p['status']) for p in probs).most_common():
        print(f'    {n:4}  {status}')
    if probe:
        solr_low = Counter(p.get('solr') for p in probs if p.get('solr'))
        if solr_low:
            print('    (of the buried ones: ' + ', '.join(f'{n} {s}' for s, n in solr_low.items()) + ')')
        field_hits = Counter(f for p in probs for f in p.get('fields_with_terms', []))
        unsearched = Counter(f for p in probs for f in p.get('unsearched_fields_with_terms', []))
        if field_hits:
            print('\n  Fields containing query terms, across the missing documents:')
            for f, n in field_hits.most_common():
                note = '  (not searched by this setup)' if unsearched.get(f) else ''
                print(f'    {n:4}  {f}{note}')
        print(f'  ({probe.calls} Solr queries, using setup "{setup["id"]}")')
    ungraded = sum(r['top_ungraded'] for r in rows)
    if ungraded:
        print(f'\n  Note: {ungraded} of the baseline\'s top-{k} results are ungraded and count as not relevant.')
    for r in others:
        n = sum(1 for q in scored for d in r['results'].get(q['id'], [])[:k] if d not in ts.judgments[q['id']])
        if n:
            print(f'  Note: "{r["label"]}" has {n} ungraded results in its top {k}; grade them before comparing it.')

    weak = sorted(rows, key=lambda r: r['ndcg'])[:args.show]
    weak = [r for r in weak if r['ndcg'] < 0.999]
    if weak:
        print(f'\nWeakest queries (worst first):')
    for r in weak:
        tags = f' [{", ".join(r["tags"])}]' if r['tags'] else ''
        print(f'\n  {r["query"]}: "{r["text"]}"{tags}  NDCG {r["ndcg"]:.2f}, {r["results"]} results')
        print(f'    top {k}: {r["top_relevant"]} graded {min_grade:g}+, {r["top_not_relevant"]} not relevant, {r["top_ungraded"]} ungraded')
        if r.get('effective'):
            for p, v in r['effective'].items():
                print(f'    {p}: {v if len(v) <= 150 else v[:149] + "…"}')
        for p in r['problems'][:6]:
            extra = ''
            if p.get('solr'):
                extra += f'; Solr: {p["solr"]}'
            if 'fields_with_terms' in p:
                extra += f'; terms in: {", ".join(p["fields_with_terms"]) or "no field"}'
            print(f'    - grade {p["grade"]:g} {p["doc"]} {p["title"][:60]!r}: {p["status"]}{extra}')
        if len(r['problems']) > 6:
            print(f'    ... and {len(r["problems"]) - 6} more')
        if not r['problems'] and r['missing_lower_grades']:
            print(f'    (every document graded {min_grade:g}+ is in the top {k}; the score is lower because '
                  f'{r["missing_lower_grades"]} lower-graded document(s) are missing)')
        if r['other_runs']:
            shown = list(r['other_runs'].items())[:4]
            short = lambda l: l if len(l) <= 40 else l[:39] + '…'
            more = len(r['other_runs']) - len(shown)
            print('    other setups: ' + ', '.join(f'{short(l)} {s:.2f}' for l, s in shown)
                  + (f' (+{more} more; best {max(r["other_runs"].values()):.2f})' if more > 0 else ''))

    if args.json:
        with open(args.json, 'w', encoding='utf-8') as f:
            json.dump({'baseline': base['id'], 'k': k, 'min_grade': min_grade, 'queries': rows}, f, ensure_ascii=False, indent=1)
        print(f'\nWrote {args.json}')


if __name__ == '__main__':
    main()
