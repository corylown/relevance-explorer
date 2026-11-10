---
name: export-runs
description: Produce the "runs" (ranked results per search setup) and document text for a Relevance Explorer file, by running the test queries against a search engine or converting existing run files. Use when someone wants to compare search setups in the viewer, add a new setup to an existing test set, or prepare document text for grading.
---

# Producing runs for the Relevance Explorer

A run is one search setup's ranked results: `{query id: [doc ids, best first]}`, saved in the file's `runs` list (README.md). Every search engine is different, so this skill is mostly about getting the mapping right. Two adapters cover the common cases, and anything else gets a small script written for it.

## 1. Agree on the setups and the grading unit

Ask the user:
- **Which setups to compare.** If they only know the baseline, run and grade that first. Then the `suggest-setups` skill can propose changes from evidence. Otherwise, include what runs in production today as the baseline (`"baseline": true`). Mark the few setups to show by default with `"featured": true`. A short `label` is what people will read.
- **What counts as one result.** Judgments and runs must use the same ids, at the level people judge. If the engine returns items but people judge collections, roll each hit up to its collection. Each collection keeps the rank of its first hit and appears once. ArcLight did exactly this, mapping each component to the first value of `parent_ids_ssim`. Decide this before grading, because changing it later invalidates the grades.
- **How deep to save.** Keep at least the top 10 for NDCG@10. Keep 50 or more if MAP or the viewer's "where each setup ranks them" table matters.

## 2. Pick the path

**Solr:** `tools/adapters/solr_runs.py`. Write a config JSON (the format is in the script's docstring):
- the select URL and the id, title and text fields
- optionally a `rollup_field`
- the parameters for each setup

Then run:

```bash
python3 tools/adapters/solr_runs.py data/solr-config.json data/tests.json \
  --out data/tests.runs.json --doc-text data/docs.jsonl
```

It sends each query's text as `q`. A setup already in the file with the same id is replaced, and other runs are kept. `--doc-text` also writes the title and text of every result and every judged document, which is what `judge-relevance` needs. When rolling up, ask for more `rows` (for example 100–300), since many hits collapse into the same parent.

**TREC files** (topics, qrels and run files from another tool): `tools/adapters/trec_to_json.py`. See its docstring.

**Anything else** (Elasticsearch, OpenSearch, a vector database, an app's own API): write a small script in `data/` or the user's own repo, using `solr_runs.py` as the model. It should:
- read `queries` from the file and send each query's `text` exactly as the app would
- write a results list for every query, using `[]` when nothing came back, so a query with no results counts as a miss instead of disappearing
- apply the same rollup and the same ids as the judgments
- keep the existing runs, replacing only setups with the same id
- optionally write document text as JSONL (`{"id", "title", "text"}` per line)

Prefer the app's own search endpoint or exact request parameters over re-creating them. A setup that drifts from what users actually get makes the comparison meaningless. If the app adds boosts or filters outside the request (a search builder, plugins), make sure the run includes them, or say clearly that it doesn't.

## 3. Check the result

- `python3 tools/validate.py <out> --by-tag` prints every run's NDCG@k, MAP and MRR. A setup scoring near 0 usually means an id mismatch, such as items against collection judgments or a prefix difference. It rarely means a truly bad setup.
- Compare a few queries by hand: run one in the app and check that the run's top ids match what the app shows.
- Report how many results nobody has graded yet. New setups surface new documents. If there are many in the top 10, suggest topping up grades with the `judge-relevance` skill before reading much into the scores. Ungraded results count as not relevant, so they push new setups' scores down.

To look at the results, open `index.html` and drop the file on it, or build a single shareable page with `python3 embed.py <file> data/<name>.html`.
