---
name: judge-relevance
description: Grade search results for a Relevance Explorer test set with Claude, blind and calibrated against people's grades. Use when someone wants to fill in missing relevance judgments, top up grades after a new search setup surfaced new results, or check how well an LLM grader agrees with their own grades.
---

# Grading search results with Claude

`tools/judge.py` does the grading through the Claude API. Your job is everything around it: the inputs, the calibration gate, the cost check, and the merge. Do not grade documents yourself in this session. The point of the script is that grading is blind, repeatable and recorded, and a session that has seen the runs is none of those.

## 1. Check the inputs

Run `python3 tools/validate.py <file>` and read the output with the user.

- **The file** must be in the Relevance Explorer format (README.md). It needs `queries`. It needs `runs`, or a candidates file (`--candidates`, a JSON object of `{query id: [doc ids]}`), to know what to grade.
- **Document text.** The grader only sees each document's `title`, `text` and any `fields`. These come from the file's `docs` plus a `--doc-text` JSONL file (one `{"id", "title", "text", "fields"}` object per line). Title-only grading is weak. If there is no text, ask where it can come from. The `export-runs` skill's Solr adapter writes this file.
- **The grading unit.** Judgments, runs and document text must all use the same ids, at the level people judge (collection or item, product or variant). Settle this before grading. Regrading at a different level later throws the grades away.
- **The rubric.** The built-in rubric uses the file's `grades` labels, plus standard wording when the scale is 0–3. Grades can carry a `description` of their own. Pass `--context "<one or two sentences on what is being searched and who searches it>"`. For anything more specific, write a rubric file and pass `--rubric`.

Real judgments and document text belong in `data/`, which git ignores.

## 2. Show the plan before spending anything

Always start with `--dry-run`. It prints how many grades and requests a run will send, the system prompt, and the first request, and it sends nothing. Show the user the counts and the prompt.

Every live run costs money. Get the user's explicit go-ahead in this conversation, for this run, before running without `--dry-run`. Pass `--yes` only after they agree. The script refuses to call the API without a terminal unless `--yes` is given.

## 3. Calibrate first

Before grading in bulk, check the grader against people:

```bash
python3 tools/judge.py calibrate data/tests.json --doc-text data/docs.jsonl \
  --against "Product owner" --context "..." --dry-run
```

`--against` names the judge (or judges, repeat the flag) whose grades are the reference. `--sample N` re-grades a random subset. The report shows:
- exact agreement
- agreement within one grade
- relevant-or-not agreement (at grade 2 on a 0–3 scale)
- quadratic weighted kappa
- a confusion matrix
- the biggest disagreements, with Claude's reasons

How to read it:
- **Kappa of 0.6 or more** is good agreement, and bulk grading is reasonable. ArcLight's grader reached 0.69 against its product owner, and was within one grade 94% of the time.
- **Below about 0.4,** don't grade in bulk. Read the disagreements with the user. The usual fixes are a clearer `--context` or rubric, adding document text, or `--use-descriptions` when queries have notes on what the searcher wants. Then calibrate again.
- **Calibration belongs to its settings.** If the model, effort, rubric or context changes, calibrate again before trusting new grades.

If nobody has graded anything yet, say so. Suggest that a person grades a small set first, even 50 pairs, so there is something to calibrate against.

## 4. Grade

```bash
python3 tools/judge.py grade data/tests.json --doc-text data/docs.jsonl \
  --out data/tests.graded.json --context "..." --dry-run
```

- **What gets graded:** the top `--depth` results (default 10) of every run, or of only `--runs a,b`, plus any `--candidates`, for pairs nobody has graded. Existing grades are never changed or re-sent.
- **What each new grade records:** `judge` (default "Claude (<model>)", change it with `--judge-name`), `model`, `reason` and `graded_on`.
- **Defaults:**
  - model `claude-opus-5-5` and effort `medium`
  - 20 candidates per request, 4 requests at a time
  - refusal fallback on: a request the model declines is re-run on another model. Some gateways reject the parameter; if the error says so, use `--no-fallback`.
  - structured outputs on, so the reply always matches the expected JSON. A gateway that runs Claude on Google Vertex AI or another cloud may block this by policy. The error names `structured_outputs`; then use `--no-structured-output`, which asks for JSON in the prompt and checks every id and grade in the reply.
  - A gateway may not offer the default model. Pass one it lists with `--model`, and calibrate with that model. Stanford's AI gateway, for example, needs `--model claude-opus-5 --effort high --no-fallback --no-structured-output`.
- **Interruptions:** progress is saved to `<out>.progress.jsonl`, so running the same command again resumes. Failed batches are listed and retried on the next run. To start over, delete the progress file.
- **Credentials:** these come from the environment (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or an `ant auth login` profile). `ANTHROPIC_BASE_URL` points it at a gateway. Never print a key.

After a run, check the output with `python3 tools/validate.py <out>`. Then tell the user what changed: how many new grades, any failed batches, and any documents skipped for having no text. Suggest opening the file in the viewer and spot-checking a few of Claude's reasons, especially in the queries where scores moved the most. Once the baseline is graded, the `suggest-setups` skill can work out what to try next.

## Things to keep straight

- **Keep grading blind.** Don't add run names, ranks or existing grades to the context, rubric or document text.
- **New grades go to a new `--out` file.** Let the user decide when it replaces their main file.
- **Don't invent test queries to fill gaps.** If a query has no relevant results anywhere, check with the user that a good answer exists in the index before treating it as a search failure.
