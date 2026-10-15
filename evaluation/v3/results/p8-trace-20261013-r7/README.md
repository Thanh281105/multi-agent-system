# Trace audit — P8 r7 (development only)

This folder contains a reproducible, local-only aggregation of the P8 r7 execution trace. It does **not** contain semantic judgments and it is not independent confirmation: the 60-case held-out split was already used during system remediation.

## Binding and coverage

- SUT: `app/v2`, commit `4238394ee932dbf79a53aaa1a565c5203f857263`.
- P7 protocol SHA-256: `342906ef4713edaef23fc0b829780ff7d70a3bc1f64fa58cd36cefe28f999a2a`.
- P7 pilot: 36/36; P8: 60 cases × 3 repeats × 4 variants = 720/720 completed cells.
- P8 schedule SHA-256: `e07a5e9996f12a57093a66f17de7585f63c0c1f0120cabf17aa2866fea495cef`.
- P8 checkpoint SHA-256: `cd9f7c0de094e1c7935880f3adc3c210604ac24c66f8cd9caede58f804e53261`.
- The checkpoint contains raw SUT receipts; the summary omits prompts and answers. Regenerate summary with `python audit_trace.py`.

## Trace findings

| Variant | Calls | Cost (USD) | Observation latency p50 / p95 (s) | Input / output tokens | User-turn outcomes |
|---|---:|---:|---:|---:|---|
| Adaptive MA + RAG | 874 | 1.601708 | 11.171 / 27.006 | 977,842 / 259,351 | 168 answered, 36 clarification, 6 abstained, 6 confirmation |
| Single-Agent + RAG | 773 | 1.432035 | 11.125 / 24.429 | 849,886 / 240,693 | 168 answered, 36 clarification, 6 abstained, 6 confirmation |
| Fixed MA + RAG | 700 | 1.457079 | 9.438 / 24.664 | 909,517 / 240,260 | 168 answered, 36 clarification, 6 abstained, 6 confirmation |
| Adaptive MA, no RAG | 234 | 0.339016 | 1.640 / 7.193 | 213,770 / 50,499 | 33 answered, 36 clarification, 141 abstained, 6 confirmation |

Each arm has 180 observations, 60 case IDs, and 27 work-group IDs. Outcome totals are **216 user turns per arm**, because some observations contain multi-turn conversations. Neither observations nor turns are 720 independent samples.

Adaptive MA + RAG vs SA + RAG used 13.1% more generation calls and 11.85% more known cost; its trace p95 was 10.55% higher. Compared with Fixed MA + RAG, Adaptive used 24.86% more calls, 9.93% more cost, and had 9.49% higher p95. These are execution trade-offs on this development split, not evidence about answer quality.

RAG arms logged many `answer_repair_missing_required_knowledge` fallback reasons (Adaptive 72, Fixed 74, SA 76) and `answer_repair` actions (92, 90, 100 respectively). Treat these as trace diagnostics: they do not prove a retrieval miss or establish citation correctness. The checkpoint records one incomplete model response in Adaptive RAG, two in Fixed MA RAG, two in SA RAG, and none in no-RAG; receipt-level safe errors were zero.

The user-turn receipt shows exactly one knowledge retrieval on each of 135 retrieval-bearing turns per RAG arm; the other 81 turns per arm have zero. The missing-required-knowledge fallback occurred on 72/135 Adaptive, 74/135 Fixed, and 76/135 SA retrieval-bearing turns. It is concentrated in cases with eight gold fact blueprints: 32/36 Adaptive, 30/36 Fixed, and 33/36 SA observations (12 cases repeated three times). In the `multi_expert_compare_recommendation` category the same pattern is 32/36, 30/36, and 33/36.

The source path in `GroundedAnswerProducer.produce` shows this fallback is assigned after the repair draft still lacks required evidence; the implementation then runs its deterministic answer path with the same `ToolEvidence`. So the trace locates a costly answer/grounding recovery path shared by all RAG variants. It does **not** establish that retrieval missed the source: the receipt stores only a retrieval count plus final emitted evidence/citations, not the full retrieved-hit set or source-level relevance labels. The 8-fact strata are only 12 work groups, with three repeated observations each; these counts are development diagnostics, not independent inferential results.

Citation wiring is structurally intact in the recorded final results (all claim citation references resolve to citation IDs and all citation evidence IDs resolve to emitted evidence IDs). This is a schema/link check only; it does not verify whether each citation supports the claim. Document recall remains unmeasured.

## Limits

- “Answered” is the runtime outcome enum, not a correctness label.
- There is no LLM or human scoring in this artifact. Correctness, claim support, citation quality, and document recall remain unmeasured.
- The 27 work groups are the clustering unit indicated by the run; the benchmark is development/regression evidence, not a preregistered independent validation set.
- P8 coverage verifies that all scheduled cells ran. It does not verify semantic quality.
- Latency p50/p95 use the sum of `elapsed_seconds` across turns within each of 180 observations per variant; percentiles use type-7 interpolation. No confidence intervals are claimed here.

## Files

- `audit_trace.py`: standard-library-only aggregation and hash/binding checks.
- `summary.json`: computed aggregate, all per-variant stage/error/fallback counts, hashes, and limitations.
- `manifest.json`: binds the archived source artifacts, byte counts, hashes, and r7 SUT revision.
- `p7-p8-r7-raw-artifacts.zip`: includes the P7/P8 protocol, schedules, execution plan, receipts, repeat decision, and successor binding. Extract it at the repository root to restore the original `output/evaluation-v3/...` paths.

After extracting the archive, regenerate this summary with `python evaluation/v3/results/p8-trace-20261013-r7/audit_trace.py`. The archive does not contain credentials or `.env` files. Its results remain development-only and do not establish answer quality.
