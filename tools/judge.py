#!/usr/bin/env python3
"""Grade search results with Claude, blind, and check the grader against people's grades.

Two commands, both reading a Relevance Explorer file plus the text of each document:

  calibrate   Re-grade documents that people already graded, without showing their grades, and
              report how well Claude agrees with them. Run this first, and again whenever the
              model, effort or rubric changes.
  grade       Grade the documents the runs returned that nobody has graded yet, and write a copy
              of the file with the new grades added. Existing grades are never changed.

  python3 tools/judge.py calibrate data/tests.json --doc-text data/docs.jsonl --against "Product owner" --dry-run
  python3 tools/judge.py grade data/tests.json --doc-text data/docs.jsonl --out data/tests.graded.json --dry-run

Without --dry-run the script shows what it will send and asks before calling the API; pass --yes
to skip the question (required when there is no terminal to ask in). Credentials come from the
environment the way the Anthropic SDK reads them (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an
`ant auth login` profile); set ANTHROPIC_BASE_URL to go through a gateway.

How grading stays blind: Claude sees only the query (and, with --use-descriptions, its notes)
and each candidate's id, title and text. It never sees existing grades, ranks, or which run
returned a candidate, and candidates are shuffled the same way on every run.

Progress is saved to <output>.progress.jsonl after each batch, so an interrupted run picks up
where it stopped. Delete that file to start over.
"""
import argparse
import datetime
import json
import os
import random
import sys
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relevance_format import FormatError, load  # noqa: E402

DEFAULT_MODEL = 'claude-opus-5-5'
SEED = 20261102
TEXT_LIMIT = 3000
FALLBACK_BETA = 'server-side-fallback-2026-07-01'
# SDK errors that mean the request itself is wrong (bad model, key, parameters): stop the run.
FATAL_ERRORS = {'BadRequestError', 'AuthenticationError', 'PermissionDeniedError', 'NotFoundError'}
# Used when the file's grade scale is 0-3 and its grades carry no descriptions of their own.
DEFAULT_GRADE_TEXT = {
    3: 'fully relevant; the result the searcher was after, answering the need directly.',
    2: 'substantively on target but a weaker fit than a 3 (for example, it covers the subject in passing '
       'while being strongly on theme).',
    1: 'defensible as a neighbour (same series, same person or organization, or a good answer to the query '
       'as worded but not to the underlying need).',
    0: 'not relevant, including near misses that only share words with the query.',
}


class JudgeError(Exception):
    pass


# ---------- inputs ----------

def load_docs(ts, path):
    """Title, text and extra fields for each document: the file's "docs", then --doc-text lines."""
    docs = {str(k): dict(v) for k, v in (ts.raw.get('docs') or {}).items() if isinstance(v, dict)}
    if path:
        with open(path, encoding='utf-8') as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError as e:
                    raise JudgeError(f'{path} line {n} is not valid JSON: {e}') from e
                if not isinstance(d, dict) or d.get('id') is None:
                    raise JudgeError(f'{path} line {n} needs an "id"')
                docs.setdefault(str(d['id']), {}).update({k: v for k, v in d.items() if k != 'id'})
    return docs


def describe(doc_id, docs):
    """What the grader sees for one candidate, or None if there's nothing to grade it on."""
    d = docs.get(doc_id) or {}
    text = ' '.join(str(d.get('text') or '').split())
    if not d.get('title') and not text:
        return None
    out = {'id': doc_id, 'title': d.get('title') or ''}
    if text:
        out['text'] = text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT] + '...'
    if isinstance(d.get('fields'), dict):
        out.update({k: v for k, v in d['fields'].items() if k not in out})
    return out


def build_rubric(ts, context, rubric_path):
    if rubric_path:
        with open(rubric_path, encoding='utf-8') as f:
            return f.read().strip()
    unit = (ts.raw.get('unit') or {}).get('singular', 'document') if isinstance(ts.raw.get('unit'), dict) \
        else ts.raw.get('unit') or 'document'
    values = {g['value'] for g in ts.grades}
    standard = values <= {0, 1, 2, 3} and values
    lines = []
    for g in ts.grades:
        text = g.get('description') or (DEFAULT_GRADE_TEXT.get(g['value']) if standard else '')
        label = g.get('label', str(g['value']))
        lines.append(f'  {g["value"]} = {label}' + (f': {text}' if text else ''))
    return '\n'.join([
        context or 'You are judging the results of a search system.',
        f'For the search query, grade each candidate {unit} for how well it serves the person who typed the query:',
        *lines,
        'Judge each candidate on its own merits, from its title and text. A candidate counts if it holds '
        'substantial material on what the query asks for, even if that is only one part of it. A word '
        'match alone is not relevance.',
        'Give exactly one entry per candidate, using its id exactly as given, with a one-sentence reason.',
    ])


def user_message(query, candidates, use_descriptions):
    parts = [f'Query: {query["text"]}']
    if use_descriptions and query.get('description'):
        parts.append(f'Notes on what the searcher wants: {query["description"]}')
    parts.append('Candidates:\n' + json.dumps(candidates, ensure_ascii=False, indent=1))
    return '\n\n'.join(parts)


def shuffled(qid, ids):
    ids = list(ids)
    random.Random(SEED ^ zlib.crc32(qid.encode())).shuffle(ids)
    return ids


# ---------- calling Claude ----------

def make_client():
    try:
        import anthropic
    except ImportError as e:
        raise JudgeError('The anthropic package is not installed: pip install -r requirements.txt') from e
    return anthropic.Anthropic(max_retries=4, timeout=600)


def grade_batch(client, opts, system, query, candidates, grade_values, _retry=True):
    """Grades for one batch of candidates: {doc id: {'grade', 'reason', 'model'}}."""
    ids = [c['id'] for c in candidates]
    numeric = 'integer' if all(float(v).is_integer() for v in grade_values) else 'number'
    schema = {
        'type': 'object',
        'properties': {'judgments': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {'id': {'type': 'string', 'enum': ids},
                           'grade': {'type': numeric, 'enum': grade_values},
                           'reason': {'type': 'string'}},
            'required': ['id', 'grade', 'reason'], 'additionalProperties': False}}},
        'required': ['judgments'], 'additionalProperties': False,
    }
    content = user_message(query, candidates, opts.use_descriptions)
    output_config = {'effort': opts.effort}
    if opts.structured:
        output_config['format'] = {'type': 'json_schema', 'schema': schema}
    else:
        # Without structured outputs the reply is asked for in words and checked below.
        content += ('\n\nRespond with JSON only, no other text: {"judgments": [{"id": "<candidate id>", '
                    f'"grade": <one of {", ".join(map(str, grade_values))}>, "reason": "<one sentence>"}}]}}')
    request = dict(model=opts.model, max_tokens=16000, system=system,
                   messages=[{'role': 'user', 'content': content}], output_config=output_config)
    if opts.fallback:
        # On a policy decline, the API re-runs the request on a fallback model it picks.
        response = client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks='default', **request)
    else:
        response = client.messages.create(**request)
    if response.stop_reason == 'refusal':
        details = getattr(response, 'stop_details', None)
        raise JudgeError(f'declined ({getattr(details, "category", None) or "no category"})')
    if response.stop_reason == 'max_tokens':
        raise JudgeError('ran out of output tokens; try a smaller --batch-size')
    text = next((b.text for b in response.content if b.type == 'text'), None)
    if text is None:
        raise JudgeError(f'no text in the response (stop reason {response.stop_reason})')
    got = {}
    for j in parse_judgments(text):
        # With structured outputs these checks always pass; without them they catch a stray id or grade.
        if (isinstance(j, dict) and j.get('id') in ids and j['id'] not in got and j.get('grade') in grade_values
                and not isinstance(j.get('grade'), bool)):
            got[j['id']] = {'grade': j['grade'], 'reason': str(j.get('reason') or '').strip(), 'model': response.model}
    missing = [c for c in candidates if c['id'] not in got]
    if missing and _retry:
        got.update(grade_batch(client, opts, system, query, missing, grade_values, _retry=False))
    elif missing:
        raise JudgeError(f'no grade returned for {len(missing)} candidate(s)')
    return got


def parse_judgments(text):
    """The "judgments" list from a reply: the whole text as JSON, or the outermost {...} in it."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find('{'), text.rfind('}')
        if start < 0 or end <= start:
            raise JudgeError(f'no JSON in the reply: {text[:200]!r}')
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as e:
            raise JudgeError(f'the reply is not valid JSON: {e}') from e
    judgments = data.get('judgments') if isinstance(data, dict) else None
    if not isinstance(judgments, list):
        raise JudgeError('the reply has no "judgments" list')
    return judgments


def run_batches(client, opts, system, work, grade_values, progress_path):
    """Grade every (query, candidates) batch, saving each finished batch to the progress file."""
    lock = threading.Lock()
    failures, done = [], 0

    def one(query, batch):
        return query, grade_batch(client, opts, system, query, batch, grade_values)

    with ThreadPoolExecutor(max_workers=opts.workers) as pool, open(progress_path, 'a', encoding='utf-8') as out:
        futures = {pool.submit(one, q, b): (q, b) for q, b in work}
        for fut in as_completed(futures):
            q, b = futures[fut]
            try:
                _, grades = fut.result()
            except Exception as e:  # one failed batch shouldn't lose the others
                failures.append((q['id'], len(b), f'{type(e).__name__}: {e}'))
                print(f'  {q["id"]}: batch of {len(b)} failed: {type(e).__name__}: {e}', file=sys.stderr)
                if type(e).__name__ in FATAL_ERRORS:  # every other request would fail the same way
                    for f in futures:
                        f.cancel()
                    hint = ''
                    if type(e).__name__ == 'BadRequestError':
                        msg = str(e)
                        hint = (' The gateway blocks structured outputs: add --no-structured-output.' if 'structured_output' in msg
                                else ' The gateway blocks the refusal fallback: add --no-fallback.' if 'fallback' in msg
                                else ' If a gateway rejected the request, try --no-fallback or --no-structured-output.')
                    raise JudgeError(f'Stopped: {type(e).__name__}. Grades finished so far are saved.{hint}') from e
                continue
            with lock:
                for doc, g in grades.items():
                    out.write(json.dumps({'query': q['id'], 'doc': doc, **g}, ensure_ascii=False) + '\n')
                out.flush()
                done += 1
                print(f'  {done}/{len(work)} batches done ({q["id"]})', file=sys.stderr)
    return failures


def read_progress(path):
    got = {}
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    j = json.loads(line)
                    got[(j['query'], j['doc'])] = j
    return got


def plan_batches(ts, pairs, docs, batch_size):
    """Group (query, doc) pairs into shuffled batches, dropping docs with nothing to grade on."""
    by_query, no_text = {}, set()
    for qid, doc in pairs:
        by_query.setdefault(qid, []).append(doc)
    queries = {q['id']: q for q in ts.queries}
    work = []
    for qid in [q['id'] for q in ts.queries if q['id'] in by_query]:
        cands = []
        for doc in shuffled(qid, by_query[qid]):
            c = describe(doc, docs)
            if c is None:
                no_text.add(doc)
            else:
                cands.append(c)
        work += [(queries[qid], cands[i:i + batch_size]) for i in range(0, len(cands), batch_size)]
    return work, no_text


def confirm(opts, work, what):
    n_docs = sum(len(b) for _, b in work)
    extras = (', refusal fallback on' if opts.fallback else '') + ('' if opts.structured else ', JSON asked for in the prompt')
    print(f'{what}: {n_docs} grades for {len({q["id"] for q, _ in work})} queries, in {len(work)} requests '
          f'to {opts.model} (effort {opts.effort}{extras}).')
    if opts.dry_run:
        q, b = work[0]
        print('\n--- system prompt ---\n' + opts.system)
        print('\n--- first request ---\n' + user_message(q, b, opts.use_descriptions)[:3000])
        print('\nDry run: nothing was sent.')
        return False
    if opts.yes:
        return True
    if not sys.stdin.isatty():
        raise JudgeError('Pass --yes to call the API without a terminal to confirm in.')
    return input('Send these requests? [y/N] ').strip().lower() == 'y'


# ---------- commands ----------

def cmd_grade(opts, ts, docs):
    runs = [r for r in ts.runs if not opts.runs or r['id'] in opts.runs]
    if opts.runs and len(runs) != len(opts.runs):
        raise JudgeError(f'unknown run id in --runs; the file has: {", ".join(r["id"] for r in ts.runs)}')
    extra = {}
    if opts.candidates:
        with open(opts.candidates, encoding='utf-8') as f:
            extra = json.load(f)
    if not runs and not extra:
        raise JudgeError('Nothing to grade: the file has no runs. Pass --candidates with {query id: [doc ids]}.')
    progress_path = opts.out + '.progress.jsonl'
    saved = read_progress(progress_path)
    pairs = []
    for q in ts.queries:
        if opts.queries and q['id'] not in opts.queries:
            continue
        pool = [d for r in runs for d in r['results'].get(q['id'], [])[:opts.depth]] + list(extra.get(q['id'], []))
        for d in dict.fromkeys(map(str, pool)):
            if d not in ts.judgments[q['id']] and (q['id'], d) not in saved:
                pairs.append((q['id'], d))
    work, no_text = plan_batches(ts, pairs, docs, opts.batch_size)
    if no_text:
        print(f'Skipping {len(no_text)} documents with no title or text to grade on '
              f'(for example {", ".join(sorted(no_text)[:5])}). Add them to --doc-text.', file=sys.stderr)
    if saved:
        print(f'{len(saved)} grades already saved in {progress_path}; only the rest will be sent.')
    failures = []
    if work and confirm(opts, work, 'To grade'):
        failures = run_batches(make_client(), opts, opts.system, work, [g['value'] for g in ts.grades], progress_path)
    elif not work:
        print('Nothing left to grade.')
    if opts.dry_run:
        return 0
    saved = read_progress(progress_path)
    today = datetime.date.today().isoformat()
    new = [{'query': j['query'], 'doc': j['doc'], 'grade': j['grade'], 'judge': opts.judge_name,
            'reason': j['reason'], 'model': j['model'], 'graded_on': today}
           for j in saved.values() if j['doc'] not in ts.judgments.get(j['query'], {})]
    out = dict(ts.raw, judgments=list(ts.raw.get('judgments') or []) + new)
    with open(opts.out, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f'Wrote {opts.out}: {len(new)} new grades from {opts.judge_name}, {len(ts.raw.get("judgments") or [])} kept as they were.')
    if failures:
        print(f'{len(failures)} batches failed; run the same command again to retry them.', file=sys.stderr)
        return 3
    return 0


def kappa(pairs, values):
    """Quadratic weighted kappa between reference and model grades on an ordered scale."""
    idx = {v: i for i, v in enumerate(sorted(values))}
    k, n = len(idx), len(pairs)
    if k < 2 or not n:
        return float('nan')
    obs = [[0.0] * k for _ in range(k)]
    for a, b in pairs:
        obs[idx[a]][idx[b]] += 1
    rows = [sum(r) for r in obs]
    cols = [sum(obs[i][j] for i in range(k)) for j in range(k)]
    num = den = 0.0
    for i in range(k):
        for j in range(k):
            w = (i - j) ** 2 / (k - 1) ** 2
            num += w * obs[i][j]
            den += w * rows[i] * cols[j] / n
    return 1 - num / den if den else float('nan')


def cmd_calibrate(opts, ts, docs):
    ref = [(qid, d, j) for qid, m in ts.judgments.items() for d, j in m.items() if j.get('judge') in opts.against]
    if not ref:
        judges = sorted({j.get('judge') or '' for m in ts.judgments.values() for j in m.values()})
        raise JudgeError(f'No grades by {", ".join(opts.against)}. Judges in the file: {", ".join(judges) or "none"}')
    if opts.sample and opts.sample < len(ref):
        ref = random.Random(SEED).sample(ref, opts.sample)
    report_path = opts.report
    progress_path = report_path + '.progress.jsonl'
    saved = read_progress(progress_path)
    work, no_text = plan_batches(ts, [(q, d) for q, d, _ in ref if (q, d) not in saved], docs, opts.batch_size)
    if no_text:
        print(f'Skipping {len(no_text)} graded documents with no title or text.', file=sys.stderr)
    failures = []
    if work and confirm(opts, work, f'To re-grade (reference: {", ".join(opts.against)})'):
        failures = run_batches(make_client(), opts, opts.system, work, [g['value'] for g in ts.grades], progress_path)
    if opts.dry_run:
        return 0
    saved = read_progress(progress_path)
    rows = [{'query': q, 'doc': d, 'reference': j['grade'], 'model': saved[(q, d)]['grade'],
             'reason': saved[(q, d)]['reason']} for q, d, j in ref if (q, d) in saved]
    if not rows:
        raise JudgeError('No grades came back to compare.')
    pairs = [(r['reference'], r['model']) for r in rows]
    values = [g['value'] for g in ts.grades]
    at = opts.relevant_at if opts.relevant_at is not None else (2 if set(values) <= {0, 1, 2, 3} else min(v for v in values if v > 0))
    n = len(pairs)
    stats = {
        'pairs': n, 'queries': len({r['query'] for r in rows}),
        'exact': sum(a == b for a, b in pairs) / n,
        'within_one_grade': sum(abs(a - b) <= 1 for a, b in pairs) / n,
        f'relevant_at_{at}_agreement': sum((a >= at) == (b >= at) for a, b in pairs) / n,
        'quadratic_weighted_kappa': kappa(pairs, values),
        'model': opts.model, 'effort': opts.effort, 'reference': opts.against,
    }
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump({'stats': stats, 'grades': rows}, f, ensure_ascii=False, indent=1)
    print(f'\nAgreement with {", ".join(opts.against)} on {n} grades ({stats["queries"]} queries):')
    print(f'  exact                      {stats["exact"]:.0%}')
    print(f'  within one grade           {stats["within_one_grade"]:.0%}')
    print(f'  relevant (>= {at}) or not      {stats[f"relevant_at_{at}_agreement"]:.0%}')
    print(f'  quadratic weighted kappa   {stats["quadratic_weighted_kappa"]:.2f}')
    order = sorted(values)
    print('  confusion (rows = reference, columns = model): ' + ' '.join(map(str, order)))
    for a in order:
        print(f'    {a}: ' + ' '.join(str(sum(1 for x, y in pairs if x == a and y == b)) for b in order))
    worst = sorted(rows, key=lambda r: -abs(r['reference'] - r['model']))[:8]
    worst = [r for r in worst if r['reference'] != r['model']]
    if worst:
        print('\n  Biggest disagreements:')
        for r in worst:
            print(f'    {r["query"]} / {r["doc"]}: reference {r["reference"]}, model {r["model"]}: {r["reason"][:140]}')
    print(f'\nWrote {report_path}')
    if failures:
        print(f'{len(failures)} batches failed; run the same command again to retry them.', file=sys.stderr)
        return 3
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0], epilog='See the docstring at the top of judge.py.')
    sub = ap.add_subparsers(dest='command', required=True)
    for name in ('grade', 'calibrate'):
        p = sub.add_parser(name)
        p.add_argument('file', help='Relevance Explorer JSON file')
        p.add_argument('--doc-text', help='JSONL file with {"id", "title", "text", "fields"} per document')
        p.add_argument('--model', default=DEFAULT_MODEL)
        p.add_argument('--effort', default='medium', choices=['low', 'medium', 'high', 'xhigh', 'max'])
        p.add_argument('--context', help='one or two sentences on what is being searched, for the start of the rubric')
        p.add_argument('--rubric', help='file with a complete rubric to use instead of the built-in one')
        p.add_argument('--use-descriptions', action='store_true', help="show the grader each query's description")
        p.add_argument('--batch-size', type=int, default=20)
        p.add_argument('--workers', type=int, default=4)
        p.add_argument('--no-fallback', dest='fallback', action='store_false',
                       help="don't ask the API to retry declined requests on a fallback model (some gateways reject it)")
        p.add_argument('--no-structured-output', dest='structured', action='store_false',
                       help='ask for JSON in the prompt instead of using structured outputs (for gateways that block them); '
                            'replies are still checked and missing grades asked for again')
        p.add_argument('--dry-run', action='store_true', help='show the plan and the first request; send nothing')
        p.add_argument('--yes', action='store_true', help='send without asking')
    g = sub.choices['grade']
    g.add_argument('--out', required=True, help='where to write the file with new grades added')
    g.add_argument('--depth', type=int, default=10, help='grade the top N results of each run (default 10)')
    g.add_argument('--runs', type=lambda s: s.split(','), help='only pool these run ids (comma-separated)')
    g.add_argument('--queries', type=lambda s: set(s.split(',')), help='only these query ids')
    g.add_argument('--candidates', help='JSON file {query id: [doc ids]} to grade as well as (or instead of) runs')
    g.add_argument('--judge-name', help='name recorded on each new grade (default: "Claude (<model>)")')
    c = sub.choices['calibrate']
    c.add_argument('--against', required=True, action='append', help='judge name whose grades are the reference (repeatable)')
    c.add_argument('--sample', type=int, help='re-grade a random sample of this many reference grades')
    c.add_argument('--relevant-at', type=float, help='grade counted as relevant in the relevant-or-not agreement')
    c.add_argument('--report', help='where to write the calibration report (default: next to the input file)')
    opts = ap.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # keep status lines in order with stderr progress
    try:
        ts = load(opts.file)
        docs = load_docs(ts, opts.doc_text)
        opts.system = build_rubric(ts, opts.context, opts.rubric)
        if opts.command == 'grade':
            opts.judge_name = opts.judge_name or f'Claude ({opts.model})'
            return cmd_grade(opts, ts, docs)
        opts.report = opts.report or os.path.splitext(opts.file)[0] + f'.calibration.{opts.model}.json'
        return cmd_calibrate(opts, ts, docs)
    except (JudgeError, FormatError, OSError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
