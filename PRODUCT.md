# Thương Trí — Product brief

## Platform

Responsive web application served same-origin by FastAPI. The production UI is
built from React, Vite, TypeScript, Tailwind CSS, and shadcn/ui, then packaged as
static assets in the Python application.

## Product summary

Thương Trí is a Vietnamese e-commerce multi-agent decision workspace. It turns
a natural-language shopping or market question into a visible sequence of
routing, planning, domain-agent execution, evidence retrieval, and grounded
synthesis. It is both a usable assistant and an inspectable graduation-thesis
artifact.

## Primary users

- A shopper or demo operator who needs a concise recommendation grounded in the
  available product and review data.
- A thesis evaluator who needs to inspect which agents ran, how the DAG was
  compiled, which models were called, and which sources support the response.
- A developer or operator who needs correlation IDs, failure/fallback state,
  latency, token usage, and sample-data labeling without seeing secrets or raw
  prompts.

## Core outcome

Within one screen, a user can ask a Vietnamese commerce question, understand
the answer, and independently inspect the execution path and evidence behind it.

## Positioning

This is an evidence-first agent operations console, not a generic chatbot. The
distinctive product value is the pairing of a calm conversational workspace
with an honest, live execution dossier: route, authorized plan, specialist
agents, model calls, fallbacks, provenance, and trace identity.

## Verified capabilities

- Vietnamese intent routing with structured model output and deterministic
  fallback.
- Authorized DAG compilation for Product, Review, Trust, and Market agents.
- Agent Gateway-only data/tool access with typed contracts and provenance.
- Evidence-linked specialist analysis and final claim synthesis.
- JSON and POST-based SSE chat interfaces with cancellation and stable errors.
- Frozen objective evaluation assets and explicitly labeled sample datasets.
- Synthetic regression data (30 products, 150 reviews) and a small public Tiki
  Books snapshot (200 products, 1,773 reviews); neither represents the real
  Vietnamese e-commerce market.

## Experience mode

Operate. The primary surface is an interactive decision workspace rather than a
marketing page or passive report.

## Design principles

1. Evidence before spectacle: every prominent conclusion must sit near its
   provenance and sample-data boundary.
2. Orchestration made legible: distinguish model reasoning, deterministic
   controls, agent work, and tool evidence instead of collapsing them into one
   “AI” status.
3. Dense but calm: support thesis-level inspection without turning the first
   viewport into a monitoring wall.
4. Honest resilience: loading, partial success, fallback, cancellation, empty,
   offline, and error states are first-class UI states.
5. Vietnamese-first copy: concise, professional, and free of inflated claims.

## Accessibility and interaction

- WCAG 2.2 AA contrast target, complete keyboard operation, visible focus, and
  a skip link.
- Focus must not be obscured by sticky surfaces.
- Motion is limited to one or two high-value transitions and fully disabled by
  `prefers-reduced-motion`.
- API credentials remain in memory only; session identity and non-sensitive
  conversation history may use `sessionStorage`.

## Evidence and claim boundary

The interface may claim that the repository demonstrates typed orchestration,
structured model calls, deterministic authorization/fallback, provenance, and
objective regression evaluation when the corresponding checks pass. It must not
claim marketplace representativeness, human preference superiority, semantic
helpfulness superiority, high availability, or “perfect production” without
direct evidence.
