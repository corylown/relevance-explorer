"""Read, check and score Relevance Explorer files (the JSON format in README.md).

The checks and the scores match index.html: a file that loads cleanly here loads cleanly in the
viewer, and the averages printed by validate.py are the ones the viewer shows.
"""
import json
import math
from dataclasses import dataclass, field


class FormatError(ValueError):
    """The file can't be used at all (the viewer would refuse to open it)."""


@dataclass
class TestSet:
    raw: dict
    queries: list            # [{'id', 'text', 'tags', 'description', 'meta'}], file order
    judgments: dict          # {query id: {doc id: judgment dict with a numeric 'grade'}}
    runs: list               # [{'id', 'label', 'baseline', 'featured', 'group', 'results': {query id: [doc ids]}}]
    grades: list             # [{'value', 'label', ...}], best first
    k: int = 10
    relevant_min: float = None
    warnings: list = field(default_factory=list)

    def is_relevant(self, grade):
        return grade > 0 and (self.relevant_min is None or grade >= self.relevant_min)

    def scorable(self, qid):
        """A query can be scored once it has at least one positive grade."""
        return any(j['grade'] > 0 for j in self.judgments[qid].values())

    def scored_queries(self):
        """The queries the viewer averages over: scorable, with results from every run."""
        return [q['id'] for q in self.queries
                if self.scorable(q['id']) and all(q['id'] in r['results'] for r in self.runs)]

    def score(self, run, qid):
        return score_query(run['results'][qid], self.judgments[qid], self.k, self.is_relevant)


def _plural(n, one, many):
    return f'{n} {one if n == 1 else many}'


def load(path):
    with open(path, encoding='utf-8') as f:
        try:
            raw = json.load(f)
        except json.JSONDecodeError as e:
            raise FormatError(f"{path} isn't valid JSON: {e}") from e
    return parse(raw)


def parse(raw):
    if not isinstance(raw, dict):
        raise FormatError('The file must contain a JSON object.')
    if not isinstance(raw.get('queries'), list) or not raw['queries']:
        raise FormatError('The file needs a non-empty "queries" list.')
    warn = []

    queries, by_id, no_id, dup_q = [], {}, 0, 0
    for q in raw['queries']:
        qid = str(q['id']) if isinstance(q, dict) and q.get('id') not in (None, '') else None
        if qid is None:
            no_id += 1
            continue
        if qid in by_id:
            dup_q += 1
            continue
        tags = list(dict.fromkeys(str(t) for t in q.get('tags', []))) if isinstance(q.get('tags'), list) else []
        o = {'id': qid, 'text': str(q.get('text', q.get('query', qid))), 'tags': tags,
             'description': str(q.get('description') or ''), 'meta': q.get('meta') if isinstance(q.get('meta'), dict) else {}}
        queries.append(o)
        by_id[qid] = o
    if no_id:
        warn.append(f"{_plural(no_id, 'query has', 'queries have')} no id and {'was' if no_id == 1 else 'were'} skipped.")
    if dup_q:
        warn.append(f"{_plural(dup_q, 'query id appears', 'query ids appear')} more than once; only the first of each was kept.")

    judgments = {q['id']: {} for q in queries}
    unknown_q = bad = dup_j = 0
    for j in raw.get('judgments') or []:
        if not isinstance(j, dict):
            bad += 1
            continue
        qid, did = str(j.get('query', '')), j.get('doc')
        if qid not in judgments:
            unknown_q += 1
            continue
        grade = j.get('grade')
        if did is None or isinstance(grade, bool) or not isinstance(grade, (int, float)) or not math.isfinite(grade):
            bad += 1
            continue
        did = str(did)
        if did in judgments[qid]:
            dup_j += 1
        judgments[qid][did] = dict(j, doc=did, query=qid, grade=grade)
    if unknown_q:
        warn.append(f"{_plural(unknown_q, 'judgment refers', 'judgments refer')} to a query that isn't in \"queries\" and {'was' if unknown_q == 1 else 'were'} skipped.")
    if bad:
        warn.append(f"{_plural(bad, 'judgment is', 'judgments are')} missing a document id or a numeric grade and {'was' if bad == 1 else 'were'} skipped.")
    if dup_j:
        warn.append(f"{_plural(dup_j, 'query and document pair was', 'query and document pairs were')} graded more than once; the last grade was kept.")

    grades = [dict(g, value=g['value']) for g in raw.get('grades') or []
              if isinstance(g, dict) and isinstance(g.get('value'), (int, float)) and not isinstance(g.get('value'), bool)]
    for v in {j['grade'] for m in judgments.values() for j in m.values()}:
        if not any(g['value'] == v for g in grades):
            grades.append({'value': v, 'label': str(v)})
    grades.sort(key=lambda g: -g['value'])

    rel_min = raw.get('relevantMin')
    if rel_min is not None and (isinstance(rel_min, bool) or not isinstance(rel_min, (int, float))):
        warn.append('"relevantMin" must be a number; any grade above 0 counts as relevant.')
        rel_min = None

    k = raw.get('k', 10)
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        if 'k' in raw:
            warn.append('"k" must be a positive whole number; 10 was used.')
        k = 10

    runs, run_ids, dup_run, unknown_run_q = [], set(), 0, 0
    for i, r in enumerate(x for x in raw.get('runs') or [] if isinstance(x, dict)):
        rid = str(r.get('id', f'run-{i + 1}'))
        if rid in run_ids:
            dup_run += 1
            continue
        run_ids.add(rid)
        results = {}
        for qid, lst in (r.get('results') or {}).items():
            if qid not in by_id:
                unknown_run_q += 1
                continue
            out, seen = [], set()
            for x in lst if isinstance(lst, list) else []:
                d = x.get('id', x.get('doc')) if isinstance(x, dict) else x
                if d is None or str(d) in seen:
                    continue
                seen.add(str(d))
                out.append(str(d))
            results[qid] = out
        runs.append({'id': rid, 'label': str(r.get('label', rid)), 'baseline': bool(r.get('baseline')),
                     'featured': r.get('featured') is True, 'group': str(r.get('group') or ''), 'results': results})
    if dup_run:
        warn.append(f"{_plural(dup_run, 'run id appears', 'run ids appear')} more than once; only the first of each was kept.")
    if unknown_run_q:
        warn.append(f"Runs list results for {_plural(unknown_run_q, 'query', 'queries')} that {'isn' if unknown_run_q == 1 else 'aren'}'t in \"queries\"; those results were skipped.")
    baselines = [r for r in runs if r['baseline']]
    if len(baselines) > 1:
        warn.append(f'More than one run is marked as the baseline; "{baselines[0]["label"]}" is used.')
    for r in runs:
        r['baseline'] = bool(baselines) and r is baselines[0]

    ts = TestSet(raw=raw, queries=queries, judgments=judgments, runs=runs, grades=grades, k=k,
                 relevant_min=rel_min, warnings=warn)
    scorable = [q['id'] for q in queries if ts.scorable(q['id'])]
    for r in runs:
        miss = sum(1 for qid in scorable if qid not in r['results'])
        if miss:
            warn.append(f'"{r["label"]}" has no results list for {_plural(miss, "scored query", "scored queries")}, '
                        "so they're left out of every setup's averages. Use an empty list for a query that returned nothing.")
    return ts


def score_query(ranked, judged, k, is_relevant):
    """NDCG@k, AP and RR for one ranked list. Unjudged results count as not relevant."""
    def dcg(gains):
        return sum(g / math.log2(i + 2) for i, g in enumerate(gains[:k]))
    ideal = sorted((j['grade'] for j in judged.values() if j['grade'] > 0), reverse=True)
    idcg = dcg(ideal)
    total = sum(1 for j in judged.values() if is_relevant(j['grade']))
    found, ap_sum, rr = 0, 0.0, 0.0
    for i, d in enumerate(ranked):
        j = judged.get(d)
        if j and is_relevant(j['grade']):
            found += 1
            ap_sum += found / (i + 1)
            rr = rr or 1 / (i + 1)
    gains = [judged[d]['grade'] if d in judged and judged[d]['grade'] > 0 else 0 for d in ranked]
    return {'ndcg': dcg(gains) / idcg if idcg else 0.0, 'ap': ap_sum / total if total else 0.0, 'rr': rr}
