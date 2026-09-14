# Seed data: Northgate Residence

Everything in this directory is fiction. There are no real residents, no real
vendors, no real prices, and no real messages from any real community anywhere.
The property, the people, and the companies were written by hand for this
project so that Steward's memory has something to remember.

## Why it is hand-written instead of generated

Steward's central claim is that it can look at a new problem and say something
specific and useful about the last time that problem happened. A randomly
generated archive cannot support that claim, because random history has no
story in it: every vendor ends up roughly average, and "here is what we learned
last time" degrades into "here are some numbers."

So the archive is authored. It contains a real argument, laid out over twenty
months, about a community that kept choosing the cheapest elevator quote and
kept paying for it twice. That argument is what makes the demo's central
decision non-obvious, and it is the reason a reviewer can check Steward's
reasoning instead of taking it on faith.

## The reference date

The archive runs from January 2025 to August 2026, and the demo's present is
**2026-09-10**. In demo mode the `VirtualClock` starts there and can be pushed
forward to show the follow-up loop.

## Files

| File | Contents |
| --- | --- |
| `property.json` | The property, its blocks, its assets, and its residents |
| `vendors.json` | Service companies, with the disclosure flag for simulated ones |
| `policies.json` | The initial state of the Approvals screen |
| `history.json` | Closed cases, with quotes, timings, costs, and outcomes |
| `live_messages.json` | The group chat traffic the demo replays |
| `threads/` | Full email threads for the cases the demo actually opens on screen |

## The shape of the argument in `history.json`

Three companies service the elevators, and their records differ in a way that a
single price comparison cannot see.

**Meridian Lift Services** answers in under four hours on average, charges in
the middle of the market, and none of its repairs on this property have come
back. Six unplanned jobs, zero recurrences.

**Coastline Elevator Co.** is consistently the cheapest quote and takes about
thirty-four hours to answer. Two of its three unplanned repairs failed again on
the same asset within ninety days, which means its average invoice understates
what it actually costs.

**Pinnacle Vertical Systems** has done one job, did it well, and charged a
premium for it. One data point is not a track record, and Steward is expected
to say so rather than pretend the number means something.

When the demo opens a new A Block elevator case, Coastline quotes lowest.
Choosing it would repeat a decision this community already made twice, in
November 2025 and September 2025, and paid for both times. The interesting part
of the demo is not that Steward automates the email. It is that Steward has a
reason not to take the cheap quote, and can point at the cases that reason
comes from.

## Deliberate gaps

Two omissions are intentional and are meant to be exercised.

`lobby-hvac` is a real asset with no approval policy configured for its
category. A problem raised against it escalates to a human, demonstrating that
an unconfigured category fails closed rather than falling back to some default
authority.

`live_messages.json` contains a message attempting a prompt injection against
Steward's spending authority. It is there so the demo can show what happens to
it, which is that it is classified and stored like any other text and never
reaches the policy engine as anything resembling permission.
