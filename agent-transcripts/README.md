# Agent development transcripts

This project was built with a coding agent (Claude, via Claude Code). This folder
is the honest record of that process, as required by deliverable 6 of the
assignment — including the approaches that did not work and how they were
corrected.

It is not a marketing document. The failures listed here are real, they cost
real time, and several of them were only caught because the output was checked
against reality rather than trusted.

## Files

| File | Contents |
|---|---|
| `checkpoint-1-knowledge-spine.md` | Ingestion, retrieval, schema, evaluation harness |

*(Further checkpoint logs are added as they are completed.)*

## How this work was directed

The agent was not asked to "build a RAG chatbot". It was given the assignment,
told to inspect the data source and the runtime environment first, and required
to produce a checkpoint plan justified by dependency and risk order before
writing code.

Three standing instructions shaped the result more than anything else:

1. **Do not trust generated code, including your own.** Every non-trivial claim
   had to be verified by running something — a test, the pipeline, a query
   against the live index.
2. **Investigate before choosing.** Where a decision mattered (chunking strategy,
   fusion method, refusal threshold), measure rather than assume.
3. **Report honestly.** Surface problems found, including ones introduced
   earlier in the same session.

## Secrets

No API keys, tokens, connection strings with real credentials, or personal data
appear in these logs. The only credentials referenced are the local development
defaults (`lenny:lenny`) that also appear in `.env.example` and are not secret.
