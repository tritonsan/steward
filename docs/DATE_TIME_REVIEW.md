# English calendar and Istanbul time — 11 September 2026

## Live meeting observation

The visitor-parking meeting moved from choosing times to waiting for participant
availability after the manager's scheduling command. The read-only snapshot at
11:55 UTC showed one proposed slot: **13 September 2026 at 18:00 Istanbul**,
stored as `2026-09-13T15:00:00Z`. The quorum was 2, with 14 eligible participants.
The current manager had not submitted an availability response. No meeting packet
had been created yet. Offering a time and submitting personal availability are
separate actions; participant responses are still required.

No live meeting command or availability response was submitted by this review.
The existing fixed simulation clock and dry-run outbound channel mode were not
changed. This presentation change does not resolve the previously observed stale
simulation deadline.

## Review and implementation

1. **Choose a date.** The old native `datetime-local` field delegated its appearance
   and language to the browser. The captured browser showed `mm/dd/yyyy`; the user
   reported Turkish controls in their browser. The replacement uses English month
   and weekday names, an explicit selected date, and the existing green theme.
2. **Enter a time.** A separate 24-hour field displays `Istanbul · UTC+03:00`.
   Four-digit mobile input is accepted (`1830` becomes `18:30`). Invalid clock
   values show an English error and prevent submission. Arrow keys, Home/End and
   Page Up/Down navigate calendar dates; Escape closes the calendar and restores
   focus without closing the containing case.
3. **Save and read the same instant.** Meeting offers, retry windows, minutes and
   assignment dates use explicit Istanbul-to-UTC conversion. Date display across
   the manager desk, resident invitations, evidence dates, quotes, appointments,
   personal requests and Telegram receipts uses English formatting in Istanbul.
   Browser locale and timezone no longer determine the submitted meeting timezone.
   Existing stored instants and operational policy settings are preserved.

The retry window no longer shares the participant list's height limit, so its
calendar controls are not clipped inside a small nested scroll area.

## Verification

- 30 frontend tests passed with the test process set to `America/Los_Angeles`.
  The six new date tests cover UTC conversion, midnight boundaries, English/24-hour
  rendering, invalid dates and times, leap days and bounded daily retry slots.
- TypeScript/Vite production build and formatting checks passed.
- Browser checks passed at desktop and 390 × 844 mobile size, including keyboard
  focus, invalid-time rejection and no horizontal page overflow.
- A synthetic local meeting was submitted through the UI for 20 September 2026 at
  18:00. Reading the API confirmed `Europe/Istanbul` and `2026-09-20T15:00:00Z`.
  The refreshed UI displayed `20 Sept 2026, 18:00`.
- Hosted update passed at 12:12 UTC: CloudFormation `UPDATE_COMPLETE`, API and
  worker each healthy at 1 running / 0 pending, anonymous case access 401 and
  authenticated reads 200. Served assets are `index-RXK3F8D0.js` and
  `index-B69UvX8x.css`. Deployment was restricted to static console assets.

Private screenshots are in `.scratch/date-time-review-20260911/`:
`01-date-fields-before.png`, `02-english-calendar-after.png`,
`03-mobile-calendar-after.png`, and `04-saved-istanbul-time.png`.
The visual and keyboard checks are a focused review, not screen-reader certification
or a comprehensive accessibility audit.

The final read-only meeting check confirmed that the existing proposal remains
`2026-09-13T15:00:00Z` (18:00 Istanbul), with the configured quorum unchanged.
