# Evidence Atlas design constitution

## Direction contract

- **THESIS:** A cartographer's decision desk for the historical Tiki Books
  snapshot, where every recommendation is a route that can be retraced.
- **OWN-WORLD:** Warm map-paper surfaces, midnight indigo ink, oxide-red route
  annotations, and muted sage completion marks. Newsreader is the scholarly
  display voice; Manrope carries interface copy; JetBrains Mono is reserved for
  trace IDs, tokens, and timings. Content lives in ledgers, folios, agent
  rosters, route diagrams, and source indexes—not a wall of generic cards.
- **STORY:** The user dispatches a question about a title, category, price, or
  reader feedback in the book snapshot; watches it pass through Router and
  Planner; follows the authorized agent route; then inspects each claim at its
  evidence destination.
- **FIRST VIEWPORT:** Product identity and historical-snapshot boundary; book
  agent station roster; current conversation and composer; live progress;
  truthful execution DAG; model-call ledger; provenance index; trace identity.
  The conversation is dominant, while the execution dossier remains visible
  without scrolling at a 1440px desktop viewport.
- **FORM:** A clearly visible multi-line Vietnamese query composer with a named
  submit action, cancel state, keyboard hint, validation/error recovery, and a
  focused credential dialog that never persists the API key.
- **SEED:** `6739c506`
- **FINISH:** This interface should feel like an instrument for defending a
  technical conclusion, not an AI chat skin.

## Reference

The approved taste anchor is
`docs/design/evidence-atlas-comp.png`. It governs density, hierarchy, linework,
type contrast, and color weighting. Its illustrative answer, counts, latency,
token values, and consumer-electronics query are not product facts and must
never be copied into runtime UI.

## Tiki Books domain language

- Visible product identity is **Evidence Atlas**, with the compact descriptor
  **Tiki Books · Historical snapshot**. On narrow mobile headers, **Atlas** is
  the approved short wordmark.
- Copy is Vietnamese-first, but established technical terms such as agent,
  Orchestrator, DAG, model, request, trace, session, fallback, and token stay in
  English when translating them would sound invented or reduce precision.
- Visible prompts, placeholders, answers, and fixtures use books, authors,
  categories, prices, publishers, or reader feedback from the Tiki Books
  domain. Headphones, laptops, and generic marketplace examples do not belong
  in the shipping workspace or its regression fixtures.
- The interface names the fixed agents as Orchestrator, Danh mục sách,
  Đánh giá độc giả, Trust signals, and Snapshot stats. Contract identifiers
  such as `product_agent`, `market_agent`, `selected_product_id`,
  `product.search`, and `catalog.product` remain unchanged at the API and
  provenance boundaries.
- Always describe the data as a cleaned historical Tiki Books snapshot. Never
  imply that its catalog, prices, reviews, rankings, or popularity represent
  current Tiki inventory or the broader Vietnamese book market.

## Semantic color tokens

Use semantic classes in components. Raw color values belong only in the root
theme token declaration.

| Token | Light value | Role |
| --- | --- | --- |
| `--background` | `oklch(0.965 0.014 86)` | warm atlas canvas |
| `--foreground` | `oklch(0.235 0.038 257)` | midnight ink |
| `--card` | `oklch(0.985 0.009 86)` | folio surface |
| `--card-foreground` | `oklch(0.235 0.038 257)` | folio text |
| `--primary` | `oklch(0.285 0.072 257)` | primary action and identity |
| `--primary-foreground` | `oklch(0.985 0.006 86)` | text on primary |
| `--secondary` | `oklch(0.915 0.024 83)` | quiet control surface |
| `--secondary-foreground` | `oklch(0.285 0.055 257)` | secondary control text |
| `--muted` | `oklch(0.925 0.012 82)` | subdued ledger row |
| `--muted-foreground` | `oklch(0.455 0.033 254)` | supporting copy |
| `--accent` | `oklch(0.455 0.145 31)` | active route / annotation |
| `--accent-foreground` | `oklch(0.985 0.006 86)` | text on oxide |
| `--success` | `oklch(0.48 0.085 145)` | verified/completed state |
| `--success-foreground` | `oklch(0.985 0.006 86)` | text on success |
| `--destructive` | `oklch(0.52 0.19 27)` | error/cancel/destructive |
| `--destructive-foreground` | `oklch(0.985 0.006 86)` | text on destructive |
| `--border` | `oklch(0.73 0.025 79)` | map rules and separators |
| `--input` | `oklch(0.73 0.025 79)` | input boundary |
| `--ring` | `oklch(0.49 0.145 31)` | high-visibility focus |

No gradient text. No decorative glass. Status may never be communicated by
color alone; pair color with icon, label, line style, or shape.

## Typography

- Display: Newsreader Variable, 600–700. Use only for product identity, the
  primary response heading, and rare section-level statements.
- Interface/body: Manrope Variable, 400–700. Base size 15–16px; line-height
  1.5–1.65; prose measure 65–75ch.
- Data: JetBrains Mono Variable, 450–600. Use for correlation IDs, token counts,
  durations, timestamps, and code only.
- Display tracking never tighter than `-0.035em`; body tracking remains normal.
- Labels are sentence case in Vietnamese. Avoid eyebrow/kicker labels.
- Numerals in operational tables use `tabular-nums`.

Fonts are self-hosted in the frontend bundle; production CSP does not gain
third-party font origins. Ship only Vietnamese, Latin Extended, and Latin
subsets; add another script only when product copy demonstrably requires it.

## Layout and spacing

- Base rhythm: 4px; normal component spacing: 8, 12, 16, 24, 32px.
- Desktop grid: `17.5rem minmax(32rem, 1fr) 23rem`, bounded by the viewport.
- Tablet: conversation first; agent roster becomes a collapsible rail; dossier
  moves below or into a Sheet.
- Mobile: one column. Composer remains in document flow or safely sticky with
  `scroll-padding-bottom`; the evidence dossier opens as a Sheet with a real
  title and description.
- Tight groups stay close; major regions use at least twice their internal gap.
- Panels use rules and surface contrast. Do not combine a border and a wide
  shadow. Elevation is reserved for the active composer, dialog, and mobile
  Sheet.
- Panel radius: 12px; control radius: 8–10px; pills only for compact statuses.

## Cartographic grammar

- Agent nodes are route stops; dependencies are route segments. Do not expose a
  forced “trạm” metaphor in navigation copy. Solid oxide is
  active, solid sage is complete, dashed ink is waiting, and dotted destructive
  is failed.
- Route geometry must come from `executions[].depends_on`; never infer edges.
- Map contours are sparse authored SVG geometry behind empty space, never a
  repeated decorative grid.
- Source records are indexed like an atlas legend and tied to exact source IDs.
- Model calls are a ledger, visually distinct from agent execution. They show
  provider/model/stage/status/latency/tokens/fallback only—never prompt, output,
  response ID, hidden reasoning, raw tool payload, or credentials.

## Component conventions

- Build controls from shadcn/ui primitives and semantic tokens.
- Cards use full `CardHeader`, `CardTitle`, `CardDescription`, `CardContent`, and
  `CardFooter` composition when a Card is warranted. Do not nest Cards.
- Forms use `FieldGroup`, `Field`, `FieldLabel`, `FieldDescription`, and
  `FieldError`; labels remain associated with native controls.
- Dialog/Sheet content always has an accessible title and description.
- Trigger composition uses `asChild`; class composition uses `cn()`.
- Use Lucide icons consistently. No emoji or Unicode glyph used as an icon.
- Clickable rows are native buttons/links with pointer cursor and visible
  `focus-visible` ring. No action is available only on hover.

## Required states

Every data-bearing region must define: initial, loading, success, partial
success, empty, error, offline, cancelled, and disabled. Model calls separately
show successful, failed-with-fallback, and failed-required states. The composer
must remain honest about whether a request is sending, cancellable, queued, or
blocked by missing credentials.

## Motion and browser surfaces

- One authored moment: the active route dash advances from station to station.
  It begins from a readable static state and uses an exponential ease-out.
- Small controls use 150–220ms transitions and tactile `active` feedback.
- Under `prefers-reduced-motion: reduce`, route motion, smooth scrolling, and
  transform feedback stop; final states remain fully legible.
- Theme selection, caret, scrollbars, text-decoration offset, focus rings, and
  tabular numerals from the token system.

## Accessibility floor

- WCAG 2.2 AA contrast for text and controls.
- A visible-on-focus skip link targets the main conversation workspace.
- DOM order follows the keyboard reading order even when desktop grid places
  regions side by side.
- Live progress uses a polite live region; terminal errors use an assertive
  alert. Do not announce every animation frame.
- On touch layouts, primary and icon actions target at least 44px. Dense desktop
  metadata controls may be smaller when they retain a clear focus ring.
- Verify at 375, 768, 1024, and 1440px and at 200% zoom.
