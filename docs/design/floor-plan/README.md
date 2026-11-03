# Approved floor plan editor

Maxi accepted this interactive design on 2026-11-03 after trying it and requesting
the refinements below. It is the implementation reference for T-3.11 (#496).
T-3.12 (#497), manual reservations on tables, remains a separate follow-on task.

Open [the interactive prototype](../../../gui/mockups/floor-plan-editor.html)
in a browser. GitHub's file view displays source, not the application. From an
Ubuntu checkout, this optional command serves it on loopback only:

```bash
python3 -m http.server 8765 --bind 127.0.0.1 --directory gui/mockups
```

Then open <http://localhost:8765/floor-plan-editor.html>. No build, CDN, account,
database or application server is required. The export also opens directly as a
local file. Browser-local storage keeps the example between reloads where the
browser permits it; it is not a backup or production persistence.

## Approval and scope

- Keep the fixed right-hand property sidebar, floor plan, compact creation
  toolbar and room navigation. On narrow screens properties move below the plan.
- Preserve the calm green selection, neutral furniture, readable labels and
  light/dark appearance of this editor. This approval concerns the floor plan;
  it does not replace D27's operations-board styling elsewhere.
- Maxi chose HTMX with native pointer events over introducing React for this
  view. D6 stays in force: Python, FastAPI, Jinja2 and HTMX. Dragging is local;
  persist the completed operation rather than making a request for each pointer
  movement. The mockup itself uses JavaScript and browser-local state, not HTMX.
- The furniture and dimensions supplied at startup are invented examples, not
  the pilot's layout. The user's live chat edits are not embedded in this file.
- German strings are the approved team-facing product copy. Documentation and
  implementation identifiers remain English.

## What to build, and why

| Area | Accepted behavior | Reason |
|---|---|---|
| Rooms | Create and name independent indoor rooms and terraces; switch without losing each plan | Restaurants may have several dining areas |
| Geometry | Enter width and depth in metres; rectangular or square, with coupled square sides; proportional drawing | A square must look square; furniture retains its physical dimensions |
| Boundaries | Editable outer-wall thickness; terraces use a boundary outline | Outdoor areas must not appear enclosed by solid walls by default |
| Entrance | Add, name and drag an entrance along the outer boundary; choose its side, width and offset | The actual entrance is not necessarily bottom-left |
| Counter | Add, move, resize and rotate; text remains readable at all four quarter turns | The earlier vertically rendered label became upside down after rotation |
| Walls | Draw by dragging or setting two points; move a wall or its endpoints; edit length, thickness and angle | Real rooms contain partitions |
| Tables | Move, rotate, name, choose rectangular or round, edit seats in single-person steps | Three- and five-person arrangements are needed |
| Presets | Choose a table size before exposing free dimensions | Common setup should be quick and approachable |
| Advanced mode | Enable individual table dimensions explicitly; disabling locks editing but preserves the values | Prevent accidental size changes and avoid losing a custom table |
| Copy | Copy a table's size, shape, seat count, end-seat choice and rotation; give the copy a new ID and number | Furnish many similar tables without re-entering properties |
| Combinations | Mark which tables can combine; copying a table does not copy these relationships | Compatibility is explicit, not inferred from proximity |
| Labels | Create, edit, place and remove free labels | Describe windows, zones or other room features |
| Recovery | Undo local edits, keyboard movement, Escape to cancel drawing | Correct placement mistakes without rebuilding the plan |

The preview offers rectangular presets 80 x 80, 120 x 80, 140 x 80,
160 x 80, 180 x 90 and 200 x 100 cm; round diameters 80, 100, 110,
120 and 140 cm. Selecting a size preserves the explicitly chosen seat count.
These are editor presets, not rules about bookable capacity.

### Seat arrangement: final accepted rule

For a rectangle in its unrotated orientation:

| Seats | Top | Bottom | Left end | Right end |
|---|---|---|---|---|
| 4 | 2 | 2 | 0 | 0 |
| 5 | 2 | 2 | 1 | 0 |
| 6 | 2 | 2 | 1 | 1 |
| 7 | 3 | 2 | 1 | 1 |
| 8 | 3 | 3 | 1 | 1 |

With one end seat the user can choose left or right. From six seats onward,
both ends remain occupied and further seats alternate between the long sides.
Hide the single-end selector when both ends are occupied. Round-table chairs
remain evenly spaced. Rotate the seating arrangement with the table, while
keeping its label readable. Drawing chairs does not certify physical clearance
or change reservation availability.

## Prototype limitations are not production decisions

- No backend writes, authentication, tenancy, audit log, concurrency control,
  revision conflicts, network failure handling or server-side validation.
- Example state is browser-local and may disappear; no deployment or real data
  import has occurred. The prototype's storage adapter is not application code.
- Eight areas, 100 objects, the storage-size guard, seat limits and numerical
  input bounds are demo limits, not agreed restaurant business limits.
- Placement can overlap walls or furniture. Copy placement is a convenience,
  not a collision or accessibility-clearance solver. Nothing here reserves a
  real table, confirms a booking or calculates availability.
- Small preview controls are not a waiver of docs/06's production touch-target,
  typography, focus, accessibility and destructive-action requirements.
  Prototype deletion is immediate with undo; production deletion needs the
  documented confirmation and checks for referenced tables/rooms.
- Moving an entrance onto an interior wall, irregular outer polygons, room
  deletion and cross-room table copying are not implemented or approved here.
- D28 remains unchanged: the caller-facing agent still uses seat capacity until
  a separately agreed transition after the plan and manual seating are in use.

## Integration and acceptance

Use this file as a visual and interaction specification. Port the behavior into
existing GUI templates/static assets; do not serve the demo as a production
route or copy its local-storage model as the source of truth. Put persistent
rules in domain services and models, with migrations and up/down checks where
needed. Preserve existing auth, tenant isolation, audit and write contracts.
Assess whether the rest of T-3.11's scope needs model/effort escalation before
starting; migrations and booking decisions require the repository's higher route.

Before declaring the implementation ready, demonstrate room isolation and reload
persistence; both wall drawing gestures; entrance movement; all counter rotations;
the seat-count table above; preset/custom-mode transitions; independent copies;
label removal/undo; and keyboard/touch behavior. Add regression coverage for the
two reported mistakes (upside-down counter text and missing end seats at six).
Test rejected writes, network failures, conflicting editors and tenant isolation
against the real backend. Run the checks required by the affected modules and
record actual results; the prototype checks do not satisfy production gates.

## Provenance and verification

Exported from the accepted chat prototype on 2026-11-03. Original fragment SHA-256:
`f37c9459235d5958a358bbbb5226d0363b34f43e44973f1ddc56de88c9fce97b`.
The standalone export replaces the optional chat state API with local storage,
removes host-only design controls and optional decorative icon placeholders, and
adds document metadata. Layout, product copy and editor behavior are preserved.
No dependency on Codex, Claude, a Windows path or a CDN remains.

Standalone export verification on 2026-11-03: headless Edge/Playwright opened the
repository HTML directly, selected six seats and measured 2 top / 2 bottom /
1 left / 1 right, copied a table, created a terrace, added a table there and
reloaded with both areas and the terrace table retained. No JavaScript errors
or HTTP(S) requests were observed. The rendered export was visually inspected.
`git diff --check` passed. The status bump initially failed with the host Python
because `tzdata` was missing; it succeeded with the bundled Python runtime.
`make lint`, `make test`, evals and migration checks were not run: this capture
changes only an isolated prototype and documentation, not application Python,
dialogue behavior or a database schema. No production performance measurement,
CI result or code-review approval is claimed.

See [the Ubuntu handover](handover.md) for the next session's prompt. This design
capture does not complete #496 or #497 and does not authorize a merge or deployment.
