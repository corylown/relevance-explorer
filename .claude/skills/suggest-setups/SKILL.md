---
name: suggest-setups
description: Work out what to try next to improve a search baseline, from evidence. Reads how search is configured today, diagnoses where and why the graded baseline ranks badly, and proposes a short ranked list of single-change setups to run. Use when someone has a graded baseline and asks what to try, why some queries do badly, or how to improve their search, and not to run or grade anything itself.
---

# Suggesting setups to try

The goal is a short list of changes worth testing. Each one needs a reason grounded in this test set and this configuration. Most reasonable-sounding changes don't help, so the value is in good evidence and honest ranking, not in a long list. You propose. The user picks what to run, and `export-runs` and `judge-relevance` do the running and grading.

## 1. Check there is something to diagnose

Run `python3 tools/validate.py <file>`. You need queries, grades, and a baseline run scored on them.
- **No baseline run, or no grades:** use `export-runs` for the run and `judge-relevance` for the grades first.
- **Many ungraded results in the baseline's top k:** grade them first. Ungraded results count as not relevant, so the diagnosis would be about missing grades, not about ranking.

## 2. Find out how search works today

Read the real configuration, not what the setup labels say. A setup with empty `params` runs on the request handler's defaults.
- **The request handler:** in `solrconfig.xml` (or what the app's `qt` points at), look at `defType`, `qf`, `pf`/`pf2`/`pf3`, `mm`, `tie`, `bq`/`bf`/`boost`, and the default sort.
- **What the app adds in code:** in Blacklight, look at the catalog controller's search fields and the search builder's processors. Boosts often live there, not in Solr. Also check the `sort` the app sends, since a tiebreaker such as recency changes rankings.
- **How text gets into fields:** in the schema, look at field types and analysis (stemming, folding, tokenization of identifiers), `copyField` rules, and any `maxChars` limits that truncate long text.
- **Leftovers:** look for fields in `qf` that don't exist in the index. A config copied from another app often carries some, and they cost nothing, but they hint that the weights were never tuned.

Write down the baseline's effective parameters. `diagnose.py` prints them for each weak query.

## 3. Diagnose the baseline

```bash
python3 tools/diagnose.py data/tests.json --baseline <run id>
python3 tools/diagnose.py data/tests.json --baseline <run id> --solr-config data/solr-config.json \
  --probe-fields <identifier fields, e.g. doi_ssi> --json data/diagnosis.json
```

For each scored query, it lists the well-graded documents (by default the top two grades) that the baseline leaves out of its top k. With the Solr config, it also says why for each one:

| Label | What it means |
|---|---|
| `matches, ranked low` | The query finds it, but other documents score higher. |
| `blocked by mm` | It only matches when one query term is enough. |
| `terms only in unsearched fields` | Query terms appear in it, but only in fields the setup doesn't search. |
| `no query terms` | Nothing matches in any field checked. That's a vocabulary gap, or the text isn't indexed. |
| `filtered out` | A searched field matches, but the query never returns it. Look for an `fq`. |

It also counts which fields contain query terms across all the missing documents, and it shows how the other runs scored each weak query.

Don't stop at the counts. Read the weakest queries, and open a few of the missing documents in the viewer. Check whether the grade is right before blaming the search. If Claude graded it, read the reason.

## 4. Turn evidence into candidates

Match what you found to changes. These are starting points, not rules:

| Evidence | Changes worth considering |
|---|---|
| Many `blocked by mm`, or queries with no results, mostly longer ones | Relax `mm` one step, for example 90% to 75%. Watch short queries, where looser matching lets in noise. |
| `matches, ranked low`, with terms in a field that has low or no weight | Reweight `qf`: give the fields where the answer's terms live real weight, and stop a catch-all field from swamping them. Add phrase boosts (`pf`, `pf2`) when the right documents contain the query as a phrase. |
| `terms only in unsearched fields` | Add the field to `qf`. For identifiers (DOIs, call numbers, ids), use an exact-match field with a high weight, and check that the identifier is normalized the same way at index and query time. |
| `no query terms` | This isn't a weighting problem. Check that the text is indexed at all (copyField truncation, missing fields). Then consider synonyms or stemming, or semantic or hybrid search for queries that describe a need in other words. |
| Top results full of documents graded not relevant that do contain the query's terms | Matching is too loose, or the wrong fields carry the weight: phrase boosts, `mm` for short queries, or less weight on broad fields. |
| Failures concentrated in one tag (identifiers, names, long topics) | A targeted change for that kind of query, such as an exact-match boost or a name-field boost. Then check it doesn't hurt the others. |
| Users want one kind of record first (the collection, the parent, the dataset rather than its files) | A boost for that record type or size. Keep it small, and test it on the queries it's meant for. |

What ArcLight's tests found, as priors only, since another app can behave differently:
- **Helped:**
  - **Hybrid search** helped most for topic queries. Keyword search with a small semantic re-rank was the best setup.
  - **A collection boost and a size boost** helped queries for a named person or organization whose material is spread across collections.
  - **Boosting abstract and scope-note fields** helped a little.
- **Looked good but didn't hold up:**
  - **Relaxed `mm` and phrase pairs** helped keyword search on its own, but hurt the hybrid setup.
  - **Giving the semantic score more weight** in the re-rank hurt identifier lookups.
  - **Rank fusion (RRF)** lost on exact lookups.
  - **Having an LLM route each query** to keyword or semantic search didn't help.

## 5. Propose 3 to 5 setups

Rank them by expected payoff against risk. For each, give:
- **The change:** a single change from the baseline, or from the best setup so far, written as a setup entry for the `solr_runs.py` config (`id`, `label`, `description`, `params`).
- **The evidence:** which queries and how many missing documents it targets, using ids from the diagnosis.
- **What should get better, and what might get worse:** for example, "relaxing `mm` should fix q18, q20 and q01; watch the 2-word known-item queries".
- **The cost:** most changes are free to run locally. New documents in the top k mean more grading, which costs money.

Only combine changes after each has been measured on its own. If the evidence points to something that isn't a configuration change (missing text, bad data, needing semantic search), say so plainly instead of forcing a tweak.

## 6. Traps to point out

- **Overfitting.** Small test sets reward changes that fix three queries and quietly break others. Prefer changes with a clear mechanism. Look at the better-and-worse counts and the per-tag scores, not just the average. Be wary of a change that only helps the exact queries you studied.
- **Ungraded results.** A new setup surfaces new documents. Grade its top k (`judge-relevance`) before comparing; `diagnose.py` and `validate.py` both warn about this.
- **One change at a time,** or you can't tell what helped.
- **A sample index isn't production.** On a subset of the data, compare setups with each other, not with production numbers.
- **Matching production.** A setup should be what users would actually get, including boosts and filters the app adds.

## 7. Hand off

Once the user picks:
1. Add the setups to the Solr config.
2. Run them with `export-runs`.
3. Grade the new top results with `judge-relevance`.
4. Compare with `validate.py --by-tag` and the viewer's better-and-worse counts.

For the next round, diagnose the new best setup.
