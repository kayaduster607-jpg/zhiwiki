# Xingzhou Store · Fictional Knowledge Exercises

[简体中文](README.md) | [English](README.en.md)

Observe document ingestion, source readback, Wiki versions, and source invalidation. All 16 Markdown originals, 4 human-authored Wiki examples, and evaluation questions are fictional teaching materials, not promises by a real merchant or evidence of model business performance.

## Load and inspect once

Install dependencies following the [main README](../README.en.md), then run from the repository root:

```bash
python -B -m zhiwiki --config config.example.json seed
python -B -m zhiwiki --config config.example.json serve --port 8875
```

Open [http://127.0.0.1:8875](http://127.0.0.1:8875), ask “2026年退款政策与2025年有什么区别？”, expand original-source citations, and inspect Wiki pages prefixed `sample-`. Default offline mode returns deterministic extracts. Wiki conclusions are human-authored and marked mode=sample.

Seed output goes into the local data database, not another article under examples. For Markdown, obtain a real ID with wiki list, then specify an output path with wiki export.

## Which files are imported

`manifest.json` is the sole original-document import list, recording title, category, update time, source_key, and description. README files, evaluation questions, and Wiki definitions are not automatically ingested as originals. Starting the service does not load examples by itself.

Materials cover refunds, shipping, warranties, products, orders, quality inspection, discounts, membership, payment, and campaigns. `wiki_samples.json` holds four human-authored pages. Seed verifies quoted source text exactly before creating citations. Repeated loading is idempotent: it does not overwrite same-name user pages or edited pages, nor automatically restore invalidated pages to trusted state.

## Demonstrate source invalidation

The 2025 and 2026 policies have different source_key values. Different business years do not imply different database revision numbers; both can initially be version=1.

To observe invalidation, update the text under **the same source_key** to create a new revision, then check whether Wiki pages citing old chunks need review. Adding a similarly named file does not automatically declare it a replacement source.

## Use the evaluation materials

`evaluation.json` contains 24 fictional questions, expected sources, and human check points. Use it for source coverage and failure scenarios. Coverage is not answer accuracy, and offline extracts do not establish model capability. Materials include long multichunk text and cross-document conditions; read back results individually.
