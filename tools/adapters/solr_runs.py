#!/usr/bin/env python3
"""Run every query against Solr once per search setup and add the results to a Relevance Explorer file.

    python3 tools/adapters/solr_runs.py config.json data/tests.json --out data/tests.runs.json --doc-text data/docs.jsonl

The config describes the Solr core, the fields to read, and each setup's request parameters:

    {
      "select_url": "http://localhost:8983/solr/my-core/select",
      "id_field": "id",
      "title_field": "title_ssm",
      "text_fields": ["abstract_tesim", "scopecontent_tesim"],
      "rollup_field": "parent_ids_ssim",
      "rows": 100,
      "keep": 50,
      "common_params": {"fq": "-hidden_bsi:true"},
      "setups": [
        {"id": "main", "label": "Main keyword", "baseline": true, "featured": true,
         "params": {"defType": "edismax", "qf": "title_tesim^5 all_text_tesim"}},
        {"id": "pf", "label": "Keyword + phrase boost", "featured": true,
         "params": {"defType": "edismax", "qf": "title_tesim^5 all_text_tesim", "pf": "title_tesim^10"}}
      ]
    }

- Each query's text is sent as q. Parameters are sent as given; a list value sends the
  parameter once per item (for several fq values, say).
- rollup_field (optional) grades results at a coarser level: each hit counts as the first value
  of that field (its parent collection, say), or as itself when the field is empty. Each rolled-up
  id keeps the rank of its first hit. Ask for more rows when rolling up, since many hits collapse.
- keep is how many ids to save per query (default 50). Rankings deeper than k only matter for
  MAP and for the viewer's "where each setup ranks them" table.
- Setups already in the file with the same id are replaced; other runs are kept.

With --doc-text, also writes the title and text of every result (and every judged document) to a
JSONL file for tools/judge.py, fetching any that weren't in the result fields from Solr.
Only the Python standard library is needed.
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def solr_get(url, params):
    pairs = []
    for k, v in params.items():
        for item in v if isinstance(v, list) else [v]:
            pairs.append((k, str(item)))
    try:
        with urllib.request.urlopen(f'{url}?{urllib.parse.urlencode(pairs)}', timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get('error', {}).get('msg') or e.reason
        except ValueError:
            msg = e.reason
        raise SolrError(f'Solr answered {e.code}: {msg}') from e
    except urllib.error.URLError as e:
        raise SolrError(f"Couldn't reach {url}: {e.reason}") from e


class SolrError(Exception):
    pass


def first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def doc_info(doc, cfg):
    text = ' '.join(' '.join(map(str, v)) if isinstance(v, list) else str(v)
                    for f in cfg.get('text_fields', []) if (v := doc.get(f)) is not None)
    return {'title': str(first(doc.get(cfg.get('title_field', 'title'))) or ''), 'text': text}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('config')
    ap.add_argument('file', help='Relevance Explorer file with the queries')
    ap.add_argument('--out', required=True)
    ap.add_argument('--doc-text', help='also write document titles and text here (JSONL)')
    args = ap.parse_args()
    cfg = json.load(open(args.config, encoding='utf-8'))
    data = json.load(open(args.file, encoding='utf-8'))
    url, id_field = cfg['select_url'], cfg.get('id_field', 'id')
    rollup, rows, keep = cfg.get('rollup_field'), cfg.get('rows', 100), cfg.get('keep', 50)
    fl = {id_field, cfg.get('title_field', 'title'), *cfg.get('text_fields', [])} | ({rollup} if rollup else set())
    info = {}

    new_runs = []
    for setup in cfg['setups']:
        results = {}
        for q in data['queries']:
            params = {**cfg.get('common_params', {}), **setup.get('params', {}),
                      'q': q.get('text', q['id']), 'rows': rows, 'fl': ','.join(sorted(fl)), 'wt': 'json'}
            try:
                docs = solr_get(url, params)['response']['docs']
            except SolrError as e:
                sys.exit(f'error: setup "{setup["id"]}", query "{q["id"]}": {e}')
            ids = []
            for d in docs:
                own = str(d[id_field])
                target = str(first(d.get(rollup)) or own) if rollup else own
                if target == own:
                    info.setdefault(own, doc_info(d, cfg))
                if target not in ids:
                    ids.append(target)
            results[q['id']] = ids[:keep]
        run = {k: v for k, v in setup.items() if k != 'params'}
        run['results'] = results
        new_runs.append(run)
        print(f'{setup["id"]}: ran {len(results)} queries', file=sys.stderr)

    ids = {s['id'] for s in new_runs}
    data['runs'] = [r for r in data.get('runs', []) if r.get('id') not in ids] + new_runs
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f'wrote {args.out}', file=sys.stderr)

    if args.doc_text:
        wanted = {d for r in new_runs for lst in r['results'].values() for d in lst}
        wanted |= {str(j['doc']) for j in data.get('judgments', [])}
        missing = sorted(wanted - info.keys())
        for i in range(0, len(missing), 100):
            chunk = missing[i:i + 100]
            params = {'q': '*:*', 'fq': '{!terms f=%s}%s' % (id_field, ','.join(chunk)), 'rows': len(chunk),
                      'fl': ','.join(sorted(fl)), 'wt': 'json'}
            for d in solr_get(url, params)['response']['docs']:
                info[str(d[id_field])] = doc_info(d, cfg)
        with open(args.doc_text, 'w', encoding='utf-8') as f:
            for d in sorted(wanted):
                if d in info:
                    f.write(json.dumps({'id': d, **info[d]}, ensure_ascii=False) + '\n')
        print(f'wrote {args.doc_text}: {len(wanted & info.keys())} of {len(wanted)} documents found', file=sys.stderr)


if __name__ == '__main__':
    main()
