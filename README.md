# Relevance Explorer

A single-page viewer for search relevance test sets. Give it one JSON file of test queries and graded results (judgments), and optionally the ranked results from one or more search setups (runs), and you can:

- **Browse the judgments:** what each query is, which documents were graded, who graded them and why, and which queries still have no judgments or no relevant ones.
- **Compare search setups:** NDCG@k, MAP and MRR for every setup, overall and per tag. Look at each setup's top results side by side, and follow one document across setups.

The page computes every score itself from the rankings and grades, so any search engine can produce the input. Nothing is uploaded: the file is read in the browser.

## Using it

Open `index.html` in a browser, straight from disk, and drop a JSON file on it (or use **Open a file**).

To share a test set, build a single file with the data inside it: `python3 embed.py data.json out.html` writes a copy of the viewer with the data built in. Send or host `out.html` as is; it opens straight to the data.

The examples folder has two small made-up data sets: `judgments-only.json` and `with-runs.json`.

Put real data and built pages in `data/`, which git ignores, so judgments aren't committed by accident.

When a file has runs, a **Judgments / Results** switch in the header changes between the two views.

## Building a test set

Scripts in `tools/` produce and check the data. Each one explains its options in its opening comment and with `--help`.

| Script | What it does |
|---|---|
| `tools/validate.py FILE` | Checks a file the way the viewer does and prints each run's NDCG@k, MAP and MRR (`--by-tag` for each tag). Standard library only. |
| `tools/adapters/solr_runs.py` | Runs every query against Solr once per search setup and writes the `runs`, plus the document text for grading. Can roll results up to a parent (items to collections). Standard library only. |
| `tools/adapters/trec_to_json.py` | Builds a file from TREC topics, qrels and run files. |
| `tools/judge.py calibrate` | Re-grades documents people already graded, blind, and reports how well Claude agrees with them (agreement rates, weighted kappa, the biggest disagreements). |
| `tools/judge.py grade` | Grades the documents the runs returned that nobody has graded yet, blind, and writes a copy of the file with the new grades added. Existing grades are never changed. |

`judge.py` calls the Claude API (`pip install -r requirements.txt`; Python 3.9 or later). It reads credentials from the environment the way the Anthropic SDK does (`ANTHROPIC_API_KEY`, or a profile from `ant auth login`; `ANTHROPIC_BASE_URL` for a gateway). Every command takes `--dry-run`, which shows what would be sent without sending anything. Live runs ask before calling the API. A gateway that runs Claude on another cloud may reject the refusal fallback or structured outputs; `--no-fallback` and `--no-structured-output` turn them off, and the error message says which one is needed.

### With Claude Code

The repo includes two Claude Code skills, in `.claude/skills/`, that walk through these steps and catch the usual mistakes. In Claude Code, opened in this repo, ask for what you want, for example "grade the new results in data/tests.json" or "add a run for our Solr phrase-boost setup".

- **`export-runs`** works out what counts as one result, which setups to compare and how deep to save. It then runs the right adapter, or writes one for an engine without an adapter, and checks the scores for id mismatches.
- **`judge-relevance`** checks the inputs and calibrates against people's grades before grading in bulk. It shows the plan and asks before spending anything, then grades and reports what changed.

## Data format

One JSON object. Only `queries` is required; everything else is optional.

```json
{
  "title": "Catalog relevance tests",
  "description": "One line shown under the title.",
  "unit": { "singular": "record", "plural": "records" },
  "k": 10,
  "grades": [
    { "value": 3, "label": "Target" },
    { "value": 2, "label": "Partly relevant" },
    { "value": 1, "label": "Marginal" },
    { "value": 0, "label": "Not relevant" }
  ],
  "tags": {
    "known_item": { "label": "Known item", "description": "Looks for one specific record." }
  },
  "docs": {
    "rec1": { "title": "Papers of Jane Doe", "url": "https://example.org/rec1" }
  },
  "queries": [
    { "id": "q1", "text": "jane doe papers", "tags": ["known_item"],
      "description": "What a good answer looks like.", "meta": { "Hits": 1234 } }
  ],
  "judgments": [
    { "query": "q1", "doc": "rec1", "grade": 3, "judge": "Product owner",
      "reason": "The collection the query names.", "note": "Regraded from 2" }
  ],
  "runs": [
    { "id": "main", "label": "Main keyword", "baseline": true, "featured": true,
      "group": "keyword", "description": "What runs in production today.",
      "results": { "q1": ["rec7", "rec1"] } }
  ],
  "groups": { "keyword": { "label": "Keyword tweaks", "note": "What was tried and what came of it." } },
  "comparisons": [ { "a": "hybrid", "b": "main", "label": "Hybrid vs main keyword" } ],
  "notes": "Free text shown under the score table."
}
```

### Fields

| Field | Meaning |
|---|---|
| `title`, `description` | Shown at the top of the page. |
| `unit` | What a result is called ("collection", "record", "product"). Default "document". |
| `k` | The cutoff for NDCG and for the side-by-side columns. Default 10. |
| `grades` | Labels for the grade scale, any order. If left out, the grades used in `judgments` are shown as numbers. Grades must be numbers; 0 or below means not relevant. An optional `description` on each grade is used in `judge.py`'s rubric. |
| `relevantMin` | The lowest grade counted as relevant for MAP and MRR. Default: any grade above 0. |
| `tags` | Labels, descriptions and display order for the tags queries use. Tags not listed here still work and are shown by their id. |
| `docs` | Titles and links for documents, keyed by id. A document without an entry is shown by its id (or by a `title` on its judgment). Links must be http or https. `judge.py` also reads a `text` field here, though large text is better kept in a separate `--doc-text` file. |
| `queries` | `id` and `text` are required. `tags` is a list of tag ids. `meta` is any set of label/value pairs to show with the query. |
| `judgments` | One entry per query and document: `query`, `doc` and `grade` are required; `judge`, `reason` and `note` are optional, and other fields (such as the `model` and `graded_on` that `judge.py` adds) are kept but not shown. A later entry for the same pair replaces an earlier one. |
| `runs` | Each run is one search setup. `results` maps a query id to its ranked list of document ids, best first (strings, or objects with an `id` or `doc` field). List every query the setup was run on; use `[]` when it returned nothing. |
| `runs[].baseline` | The setup the others are compared against (the first one marked wins). Adds a "better / worse than baseline" column. |
| `runs[].featured` | The setups shown by default in the score table and the per-query columns. If none are marked, the first five are featured. The rest appear under "Show all setups", grouped by `group`. |
| `groups` | Labels and notes for run groups, in display order. |
| `comparisons` | The pairs offered in the query list's **Compare** menu. Default: each featured setup against the baseline. |
| `notes` | Shown under the score table. |

### How scores are calculated

Scores are per query, then averaged over queries.

- **NDCG@k:** each result in the top k earns its grade, multiplied by 1 / log2(rank + 1). The total is divided by the total for the ideal order, built from every positive grade for that query. A score of 1 is the best possible order.
- **AP (averaged into MAP):** precision at each relevant result in the whole list, summed and divided by the number of relevant judged documents.
- **RR (averaged into MRR):** 1 / rank of the first relevant result, or 0 if none.

Unjudged results count as not relevant. A query is scored only if it has at least one positive grade. Averages cover the scored queries that every run has results for, so all setups are compared on the same queries; the page warns when a run is missing some.

"Better" and "worse" count queries whose score moved by more than 0.02 against the baseline.

### Things the viewer leaves to you

- **What counts as a document.** If results should be judged at a coarser level (for example, rolling components up to their collection), do that before writing the file, so judgments and rankings use the same ids.
- **How deep to save rankings.** Ranks in the graded-documents table only go as deep as the lists you provide. MAP depends on that depth too.
