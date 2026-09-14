"""Create honest normal-speed selects from the uninterrupted OBS coverage takes.

Only media derivatives are written. No application/OBS/runtime interaction.
"""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import re
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "artifacts/video/coverage-session"
OUT = BASE / "selects"
RAW = {
    "history": BASE / "raw/2026-09-14 02-53-05.mp4",
    "minutes": BASE / "raw/2026-09-14 02-55-11.mp4",
    "confirm": BASE / "raw/2026-09-14 02-58-04.mp4",
    "outcome": BASE / "raw/2026-09-14 02-59-44.mp4",
    "quotes": BASE / "raw/2026-09-14 03-02-38.mp4",
    "approval": BASE / "raw/2026-09-14 03-06-43.mp4",
    "appointment": BASE / "raw/2026-09-14 03-08-26.mp4",
    "completion": BASE / "raw/2026-09-14 03-11-36.mp4",
    "resident": BASE / "raw/2026-09-14 03-12-53.mp4",
    "maintenance_memory": BASE / "raw/2026-09-14 03-13-49.mp4",
    "settings": BASE / "raw/2026-09-14 03-16-17.mp4",
    "clarification_warranty": BASE / "raw/2026-09-14 03-19-44.mp4",
    "quorum_wait": BASE / "raw/2026-09-14 03-24-27.mp4",
    "deadline_review": BASE / "raw/2026-09-14 03-25-38.mp4",
    "recorded_sources": BASE / "raw/2026-09-14 03-27-30.mp4",
    "message_to_case": BASE / "raw/2026-09-14 03-32-07.mp4",
}

# Seconds are source-relative, end-exclusive; every retained frame plays at 1x.
SPECS = {
    "history": {
        "title": "Parking preparation and historical source access",
        "ranges": [(0, 7, "Open parking case and read prepared topic/agenda"),
                   (30.5, 37, "Expand What happened before and read historical context"),
                   (85.5, 96, "Read historical source; actual source opens and scrolls into view")],
        "result": "Previous parking decision source is opened inside the same case.",
        "limits": "Synthetic community history; no new decision made. This is source access, not original-report access.",
    },
    "minutes": {
        "title": "Written minutes, English calendar and candidate review task",
        "ranges": [(0, 5, "Open parking case with Record the meeting minutes next step"),
                   (9, 15, "Scroll to empty Written minutes form"),
                   (49, 55, "Actual synthetic minutes entry and readable result"),
                   (58, 62, "Open English September calendar"),
                   (66, 72, "Select 17 September; resulting date remains visible"),
                   (107.5, 110.5, "Select existing time text"),
                   (115, 120, "Replace with 13:00 and show completed form"),
                   (123, 131, "Prepare decisions for review; durable command acknowledgement"),
                   (137, 147, "Close drawer and refresh; next step becomes Confirm the extracted decisions")],
        "result": "Submitted minutes are processed into candidates awaiting manager confirmation.",
        "limits": "After-the-meeting local simulation. The green generic command acknowledgement is not approval. Ordinary worker continuation happened offscreen between acknowledgement and refresh. Calendar holds and input-focus troubleshooting are cut; processing time cannot be measured from this select. Red spellcheck marks remain authentic.",
    },
    "confirm": {
        "title": "Confirm selected decision and action, then inspect owner and due date",
        "ranges": [(0, 5.5, "Open case with Confirm the extracted decisions next step"),
                   (8, 11.5, "Scroll to unchecked candidate decision and action"),
                   (17.5, 22, "Select Adopt a 24-hour visitor parking limit"),
                   (26, 31, "Separately select Publish the written visitor parking rule"),
                   (45, 49.5, "Enter confirmation note"),
                   (52, 59, "Confirm selected decisions; observe Actions Tracking and publication next step"),
                   (62, 71, "Scroll to confirmed decision and agreed action: Simon O., due 20 September 2026")],
        "result": "Selected candidates become a confirmed rule and an assigned, dated action.",
        "limits": "Local synthetic meeting, not a real community adoption. Action is assigned, not completed in this clip.",
    },
    "outcome": {
        "title": "Action completion, separate verification and community-memory retrieval",
        "ranges": [(19, 25, "Enter demo completion evidence for the published rule"),
                   (27, 35, "Record completion; case now awaits community outcome verification"),
                   (41, 47, "Scroll to Completion Recorded action and a fresh empty evidence field"),
                   (64, 71, "Enter separate demo verification evidence"),
                   (74, 83, "Verify outcome; Closed and Verified outcome saved to community memory"),
                   (84, 89, "Close drawer; parking case is no longer in the open list"),
                   (95, 101, "Open Community memory through actual navigation"),
                   (114, 121, "Enter parking search; retained history and newly verified case appear"),
                   (126, 135.2, "Open the new memory record; work and verified outcome remain available")],
        "result": "A completion claim requires separate outcome verification; the verified case can then be searched and read in community memory.",
        "limits": "Entirely local simulation: both evidence entries explicitly say Demo. No real notice publication or independent field observation is claimed. The new memory record is opened; Open case record is visible but not clicked here.",
    },
    "quotes": {
        "title": "Maintenance recommendation and both quote source records",
        "ranges": [(0, 9, "Open service-order review and read recommendation, alternative and change condition"),
                   (207, 214, "Open Source 1: 705 USD including rail alignment correction"),
                   (216, 221, "Back to case returns to the unchanged recommendation"),
                   (224, 229.9, "Open Source 2: 540 USD guide-shoe replacement only")],
        "result": "The recommendation and narrower lower-price alternative can be checked against their original stored quote texts.",
        "limits": "Local synthetic vendor quotes. No approval or external send occurs in this clip. Technical inspection hold from roughly 10–203s is omitted. This clip proves visible source access, not live model execution or historical retrieval.",
    },
    "approval": {
        "title": "Approve service order, observe dispatch continuation and appointment wait",
        "ranges": [(11, 17, "Enter reason for approving the 705 USD rail-alignment proposal"),
                   (18.5, 38.5, "Approve this service order; Committed/send next step then Awaiting Appointment appears through normal worker continuation; close drawer"),
                   (43, 47, "Open Cases and see the vendor appointment proposal still awaited"),
                   (51, 63, "Reopen case and scroll to immutable vendor quotes"),
                   (67, 74, "Inspect all three quote versions, including incomplete 680 USD terms and locked-after-dispatch explanation")],
        "result": "Approval and queued dispatch are visibly distinct from the subsequent waiting-for-vendor-appointment state; quote sources remain available after dispatch.",
        "limits": "Isolated local simulation with dry-run delivery, not a real supplier email. Ordinary worker was advanced offscreen; the visible state changes in the still-open drawer before Cases navigation. No vendor appointment is confirmed in this clip. The incomplete third quote is shown without inventing missing validity/exclusions.",
    },
    "appointment": {
        "title": "Simulated vendor proposal, durable receipt and confirmed appointment",
        "role": "manager operating the explicitly labelled Simulation Studio",
        "ranges": [(0, 5, "Navigate from Awaiting Appointment case to Simulation Studio"),
                   (15, 21, "Select the elevator case in the vendor response form"),
                   (31, 37, "Select Meridian Lift Services as the responding vendor"),
                   (47, 53, "Enter explicit appointment times, unchanged price/scope and agreed access"),
                   (55.5, 62, "Add vendor reply; durable intake receipt appears and input clears"),
                   (74, 90, "Run one tick through the visible simulation control, then navigate to Scheduled case list"),
                   (93, 107, "Open the Scheduled case and scroll to the current confirmed appointment"),
                   (109, 118.7, "Expand Vendor evidence and read the preserved appointment source")],
        "result": "A vendor proposal entering the durable intake and normal worker is followed by a Scheduled case with an explicit confirmed appointment and source evidence.",
        "limits": "Synthetic vendor input and dry-run delivery; no actual supplier acceptance email is claimed. The Run one tick action is retained, including its temporary disabled state, but no nonexistent processing toast is added. Raw 19 September 2026 10:00–11:00 UTC equals 13:00–14:00 in the site's Europe/Istanbul display. This is appointment confirmation, not completion.",
    },
    "completion": {
        "title": "Manager records work completion and requests verification",
        "role": "manager",
        "temporal_context": {"demo_clock_advanced_hours_before_source": 48,
                             "advance_visible_in_source": False,
                             "required_edit_context": "Demo time advanced · 48 hours",
                             "between_sources": ["raw/2026-09-14 03-08-26.mp4", "raw/2026-09-14 03-11-36.mp4"]},
        "ranges": [(0, 3, "Read Scheduled and Attend the confirmed repair visit"),
                   (6, 12, "Enter the explicitly labelled demo vendor completion evidence"),
                   (15, 24, "Record completion; observe Awaiting Verification and Did the work solve the problem?")],
        "result": "The manager's completion claim creates a verification request; the case remains open.",
        "limits": "The isolated demo clock advanced 48 hours offscreen after the appointment take. That context must accompany any edit joining the two takes; this is not recorded real elapsed time. Synthetic completion evidence is entered by the manager, not received from a real supplier. No verification or closure occurs here.",
    },
    "resident": {
        "title": "Resident answers the personal verification request",
        "role": "reporting resident in Resident space",
        "ranges": [(0, 6, "Refresh the resident view and reveal the personal Did the work solve the problem? card"),
                   (19, 24, "Enter a separate, explicitly labelled demo observation"),
                   (26, 34, "Confirm the observation; the personal verification card disappears and No open cases appears")],
        "result": "A resident provides the separate outcome observation and submits Confirm through the resident-only response card.",
        "limits": "Kept separate from the manager completion clip to preserve the actor change. Sign-in and role transition occurred offscreen. The outcome is a synthetic observation. The visible result is No open cases; the closed source record and verified memory are proved by the subsequent manager memory clip.",
    },
    "maintenance_memory": {
        "title": "Search the verified repair, reopen its closed case and read the original report",
        "role": "manager",
        "ranges": [(0, 6, "Navigate from the clear manager case list to Community memory"),
                   (16, 22, "Search shudders and see the newly verified elevator memory"),
                   (25, 33, "Open the memory: separate work/outcome evidence, 705 USD, vendor and closed date"),
                   (37, 45, "Open case record; Closed and Verified outcome saved to community memory are visible"),
                   (47, 53, "Scroll to the preserved appointment and quote history"),
                   (57, 63, "Expand Activity history and sources"),
                   (67, 73, "Scroll to case-opening event and its original source link"),
                   (76, 86, "Open the Resident message source and read the original synthetic elevator report")],
        "result": "The verified repair can be searched, reopened as a closed case, and traced back through activity history to the original resident input.",
        "limits": "Same isolated synthetic scenario, now viewed as manager after the separate resident verification take. The source explicitly says DEMO RESIDENT INPUT; this is not evidence of a real Telegram message or physical repair. The retained cost is the selected 705 USD quote, not a paid invoice.",
    },
    "settings": {
        "title": "Read appointment rules and operational authority",
        "role": "manager, read-only inspection",
        "ranges": [(0, 5, "Expand Approved appointment hours and access from its collapsed state"),
                   (9, 16, "Scroll to all seven working days, UTC hours 8–20, one hour notice and access rules"),
                   (18, 23, "Scroll to Operational authority and the inactive pause control"),
                   (28, 35, "Scroll through category authority modes and budget limit fields")],
        "result": "The configured appointment constraints and category-level authority controls can be inspected together.",
        "limits": "Read-only local simulation inspection: no field changes, saves or pause action occur. Community Telegram group explicitly says the application's bot is not configured in this isolated local environment; this is not live Telegram connection evidence. Appointment rule hours use the property's UTC zone, independently of the UI's Istanbul date display.",
    },
    "clarification_warranty": {
        "title": "Extra case: resident clarification leads to a previous-repair check",
        "role": "resident response, then manager inspection; role switch visible as a different application view",
        "scenario_context": "Separate synthetic Demo extra · Elevator report needs a location case, not a reopening of the successfully verified primary repair.",
        "ranges": [(198, 203, "Read the personal question asking which block/elevator and what happens"),
                   (207, 213, "Enter the synthetic A Block elevator clarification and observed problem"),
                   (215, 223, "Send answer; personal question disappears while the extra case remains open"),
                   (227, 231, "Switch from resident space to the already signed-in manager view"),
                   (234, 240, "Navigate to Cases and see Check the previous repair first"),
                   (242, 253.5, "Open the extra case; read the recent verified repair reference and requirement to check warranty before another paid job")],
        "result": "Clarification is accepted in the same extra case, whose next step becomes a manager warranty check based on the recent verified repair.",
        "limits": "Roughly the first 198 seconds are a static input/focus hold and are omitted. Ordinary worker processing occurred offscreen, as reported by the recording director; no model call or tick button is visible here. The previous repair stays closed. No warranty entitlement, free rework, additional payment or completed warranty review is claimed; the manager evidence field remains empty and no procurement continuation is submitted.",
    },
    "quorum_wait": {
        "title": "Extra meeting: resident availability and the quorum wait",
        "role": "resident availability response, then manager inspection",
        "scenario_context": "Separate Demo extra · Parking meeting needs a quorum case; not the earlier confirmed and completed parking decision.",
        "ranges": [(5, 12, "Open My meetings from Community cases and read the invitation"),
                   (14, 19, "Select 22 September 2026 at 14:00 in the resident availability form"),
                   (21, 28, "Share availability and read Your response is recorded / Your availability has been shared"),
                   (29, 33, "Switch from resident receipt to the already signed-in manager Cases view"),
                   (36, 43, "Open the extra parking case and read Waiting for participant availability and prepared agenda"),
                   (44, 53.1, "Scroll through options, unresolved questions and Waiting for the configured quorum of 3 participants")],
        "result": "One resident's availability is recorded, while the meeting still awaits the configured quorum of three.",
        "limits": "Local synthetic invitation and response. The checked time is availability, not a confirmed meeting. No quorum reduction, invitation confirmation, agreed parking rule or managerial availability submission occurs. Manager and resident sessions were already signed in; the visible role switch does not represent sign-in.",
    },
    "deadline_review": {
        "title": "Extra meeting: expired response round reaches manager review",
        "role": "manager, read-only review",
        "scenario_context": "The same extra quorum-wait meeting as the preceding availability clip; independent from the earlier completed parking story.",
        "temporal_context": {"demo_clock_advanced_hours_before_source": 24,
                             "advance_visible_in_source": False,
                             "normal_worker_tick_visible_in_source": False,
                             "required_edit_context": "Demo time advanced · 24 hours",
                             "between_sources": ["raw/2026-09-14 03-24-27.mp4", "raw/2026-09-14 03-25-38.mp4"]},
        "ranges": [(15, 21, "Navigate to Decisions and inspect the Meeting Reschedule review card"),
                   (23, 29, "Open Review case; existing topic and prepared agenda remain available"),
                   (31, 40, "Scroll to This response round has ended, disabled old slots and the empty replacement-meeting reason form")],
        "result": "The expired availability round is represented by a manager rescheduling task, with the old response choices disabled.",
        "limits": "Demo time advanced 24 hours and the normal worker ran offscreen, as reported by the recording director. No new approved retry times were configured in this scenario. The task's generic wording also mentions the two-retry limit, but this recording does not prove two retries occurred. The case's generic next-step summary still says Waiting for participant availability; the specific deadline task and expired-round UI are the evidence. No replacement meeting is submitted or confirmed.",
    },
    "recorded_sources": {
        "title": "Recorded verification sources: AgentCore, restart, Telegram and controlled SES",
        "role": "read-only saved verification viewer, separate from product runtime",
        "scenario_context": "Retrospective inspection of four saved verification artifacts. No live event stream, regenerated response or current product-run execution is shown.",
        "ranges": [(4, 15, "Read Recorded verification / synthetic input, AgentCore source and human_review result; scroll to Nova model, tools, six calls and token usage"),
                   (21, 30, "Open Restart source; read selected preservation fields, same cases, three outbox records and replacement worker"),
                   (38, 44, "Open Telegram source and its 13 September hosted connection-test provenance heading"),
                   (47, 55, "Scroll to Telegram live delivery mode and delivered test result, while hosted mail mode remains dry_run"),
                   (62, 69, "Open SES source and the separate controlled-scenario provenance heading"),
                   (71, 80, "Scroll to recorded real SES/model/ingress flags, isolated runtime, three delivered management messages and two processed replies"),
                   (87, 94, "Scroll through the 11 September recorded SES ingress, quote and order stages"),
                   (97, 105.1, "Scroll to preserved order across restart, appointment confirmation, one logical order and one budget reservation")],
        "result": "The four original recorded-source views can be inspected with their provenance, retained key values and actual navigation/scrolling preserved.",
        "limits": "This select shows saved verification files, not new tests. AgentCore is a synthetic smoke input whose result asks for human review. Restart fields establish only recorded case/outbox preservation and worker replacement. Telegram is an independent hosted connection-test delivery recorded on 13 September 2026. SES is the separate 11 September controlled real-channel/model scenario using isolated local SQLite, with no actual supplier, physical repair or payment. Its final scheduled status is not verified completion. These sources do not change the filmed product lifecycle's simulated input/delivery scope. Private fields were already omitted in the source viewer; the select adds no replacement text, overlays or regenerated evidence. The first four seconds are omitted as an opening handle; inspected integer-second boundary frames showed no persistent F11 toast.",
    },
    "message_to_case": {
        "title": "Alternative opening: simulated resident message becomes a new meeting case",
        "role": "manager using the labelled Simulation Studio and inspecting the resulting case",
        "scenario_context": "New alternative parking case created after the primary parking story was already closed. Similar title and subject do not make it the original case; do not splice it into that earlier case's chronology.",
        "ranges": [(0, 5, "Navigate from Decisions to the labelled Simulation Studio"),
                   (10, 16, "Scroll to the resident input form, with Daniel K. already selected"),
                   (31, 38, "Enter DEMO OPENING resident message about 24-hour versus 72-hour visitor parking"),
                   (41, 47, "Add resident message; input clears through the actual application command"),
                   (52, 59, "Scroll to the top and read Simulation event processed receipt"),
                   (64, 70, "Scroll back to the normal runtime controls"),
                   (73, 79, "Run one tick; retain the actual button action and its temporary disabled state"),
                   (84, 90, "Navigate to Cases; three cases now include the new visitor-parking meeting case"),
                   (94, 103, "Open the new case and read Offer meeting times with its prepared topic and agenda"),
                   (107, 118.4, "Scroll to both options, tradeoffs, unresolved questions, quote-planning boundary and the blank meeting date field")],
        "result": "The visible synthetic resident message is accepted, the normal worker is ticked, and a new meeting case can be opened with prepared discussion context and an Offer meeting times next step.",
        "limits": "All input and delivery in this product recording remain simulated; this is not a real Telegram arrival. Daniel K. was the existing form selection, not visibly chosen in the retained action. The receipt and tick are shown as separate observed steps; the footage is not a measurement of processing latency. The new case is an alternative opening take, not the already completed primary parking case. No date, invitation, quorum or community decision is submitted. Potential quote requests are planning only; the visible draft identifies no external quote need.",
    },
}


def command(args):
    return subprocess.run(args, check=True, capture_output=True, text=True)


def frame(source, second, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(second),
             "-i", str(source), "-frames:v", "1", str(target)])


def sheet(paths, target, columns=4, crop=True):
    width, height = (420, 720) if crop else (480, 270)
    pad = 34
    result = Image.new("RGB", (width * columns, (height + pad) * ((len(paths) + columns - 1) // columns)), "#0a2935")
    draw = ImageDraw.Draw(result)
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 18)
    for idx, path in enumerate(paths):
        im = Image.open(path).convert("RGB")
        if crop:
            im = im.crop((1290, 0, 1920, 1080))
        im = im.resize((width, height), Image.Resampling.LANCZOS)
        x, y = idx % columns * width, idx // columns * (height + pad)
        result.paste(im, (x, y))
        draw.text((x + 8, y + height + 5), path.stem, fill="white", font=font)
    result.save(target, quality=88)


def probes(only=None):
    groups = {
        "history": [(32, 38), (78, 93)],
        "minutes": [(51, 57), (58, 71), (89, 99), (107, 117), (123, 132), (138, 147)],
        "confirm": [(16, 22), (26, 31), (47, 57), (61, 68)],
        "outcome": [(21, 31), (66, 80), (95, 102), (117, 131)],
        "quotes": [(203, 210), (216, 229)],
        "approval": [(12, 25), (29, 38), (40, 46), (50, 59), (65, 71)],
        "appointment": [(15, 22), (29, 36), (47, 61), (74, 82), (86, 96), (101, 114)],
        "completion": [(6, 10), (14, 20)],
        "resident": [(0, 6), (20, 31)],
        "maintenance_memory": [(16, 20), (26, 41), (56, 62), (66, 80)],
        "settings": [(0, 18), (20, 30)],
        "clarification_warranty": [(198, 215), (214, 220), (220, 236), (237, 252)],
        "quorum_wait": [(5, 10), (16, 25), (31, 40), (45, 51)],
        "deadline_review": [(15, 25), (29, 35)],
        "recorded_sources": [(0, 5), (7, 11), (21, 25), (35, 40), (46, 50), (60, 65), (72, 76), (87, 90), (97, 100)],
        "message_to_case": [(0, 2), (11, 15), (31, 35), (41, 46), (51, 55), (68, 76), (86, 100), (107, 112)],
    }
    for name, ranges in groups.items():
        if only and name != only:
            continue
        for start, end in ranges:
            paths = []
            for second in range(start, end + 1):
                path = OUT / "qa" / "source-boundaries" / name / f"{second:06.2f}s.png"
                if not path.exists():
                    frame(RAW[name], second, path)
                paths.append(path)
            target = OUT / "qa" / "source-boundaries" / f"{name}-{start}-{end}.jpg"
            full_frame = name in ("resident", "settings", "clarification_warranty", "recorded_sources") or (name == "message_to_case" and start < 107) or (name == "quorum_wait" and start < 45) or (name == "deadline_review" and start < 26) or (name == "appointment" and start < 100) or (name == "maintenance_memory" and start < 26) or (name == "outcome" and start > 90)
            sheet(paths, target, crop=not full_frame)
            print(target)


def overview(name):
    probe = json.loads(command(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(RAW[name])]).stdout)
    seconds = float(probe["format"]["duration"])
    folder = OUT / "qa" / "source-overview" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "probe.json").write_text(json.dumps(probe, indent=2) + "\n", encoding="utf-8")
    paths = []
    for second in list(range(0, int(seconds), 5)) + [round(seconds - .15, 3)]:
        path = folder / f"{second:07.3f}s.png"
        frame(RAW[name], second, path)
        paths.append(path)
    for idx in range(0, len(paths), 8):
        path = folder / f"page-{idx // 8 + 1:02d}.jpg"
        sheet(paths[idx:idx + 8], path, columns=2, crop=False)
    print(json.dumps({"name": name, "duration": seconds, "pages": (len(paths) + 7) // 8}), flush=True)


def render(name):
    spec = SPECS[name]
    folder = OUT / "segments" / name
    folder.mkdir(parents=True, exist_ok=True)
    prior_path = OUT / f"{name}-edit-manifest.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8")) if prior_path.exists() else {}
    shots = []
    elapsed = 0.0
    for idx, (start, end, context) in enumerate(spec["ranges"]):
        path = folder / f"{idx + 1:02d}.mp4"
        frames = round((end - start) * 30)
        old = prior.get("edits", [])[idx] if idx < len(prior.get("edits", [])) else {}
        reuse = path.exists() and old.get("source_start_seconds") == start and old.get("source_end_seconds") == end
        if not reuse:
            command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start),
                 "-i", str(RAW[name]), "-map", "0:v:0", "-an", "-vf", "setpts=PTS-STARTPTS,fps=30,setsar=1",
                 "-frames:v", str(frames), "-c:v", "libx264", "-threads", "2", "-preset", "fast", "-crf", "18",
                     "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)])
        shots.append({"source_start_seconds": start, "source_end_seconds": end,
                      "select_start_seconds": elapsed, "select_end_seconds": elapsed + frames / 30,
                      "frames": frames, "context": context})
        elapsed += frames / 30
    concat = folder / "concat.txt"
    concat.write_text("".join(f"file '{idx + 1:02d}.mp4'\n" for idx in range(len(shots))), encoding="utf-8")
    target = OUT / f"steward-{name}-action-result-1080p.mp4"
    command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
             "-c", "copy", "-map_metadata", "-1", "-metadata", f"title={spec['title']}", "-movflags", "+faststart", str(target)])
    probe = json.loads(command(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(target)]).stdout)
    decoded = command(["ffmpeg", "-hide_banner", "-v", "info", "-i", str(target), "-an", "-vf", "blackdetect=d=0.05:pix_th=0.10",
                       "-fps_mode", "passthrough", "-f", "null", "-"])
    qa = OUT / "qa" / name
    qa.mkdir(parents=True, exist_ok=True)
    (qa / "decode.log").write_text(decoded.stderr, encoding="utf-8")
    stream = next(s for s in probe["streams"] if s["codec_type"] == "video")
    expected = sum(s["frames"] for s in shots)
    assert int(stream["nb_frames"]) == expected, (name, stream["nb_frames"], expected)
    assert stream["width"] == 1920 and stream["height"] == 1080 and stream["avg_frame_rate"] == "30/1"
    assert abs(float(stream["duration"]) - elapsed) < 1 / 30
    assert not any(s["codec_type"] == "audio" for s in probe["streams"])
    assert "black_start:" not in decoded.stderr
    decoded_frames = [int(x) for x in re.findall(r"frame=\s*(\d+)", decoded.stderr)]
    assert decoded_frames[-1] == expected, (name, decoded_frames[-1], expected)
    paths = []
    timestamps = set()
    for shot in shots:
        timestamps.update([shot["select_start_seconds"], shot["select_start_seconds"] + .5,
                           (shot["select_start_seconds"] + shot["select_end_seconds"]) / 2,
                           shot["select_end_seconds"] - .1])
    for second in sorted(timestamps):
        path = qa / f"frame-{second:07.3f}s.png"
        frame(target, second, path)
        paths.append(path)
    pages = []
    for idx in range(0, len(paths), 8):
        page = qa / f"encoded-page-{idx // 8 + 1:02d}.jpg"
        sheet(paths[idx:idx + 8], page, columns=2, crop=False)
        pages.append(page.relative_to(OUT).as_posix())
    record = {
        "name": name, "title": spec["title"], "source": RAW[name].relative_to(BASE).as_posix(),
        "source_sha256": hashlib.sha256(RAW[name].read_bytes()).hexdigest(),
        "output": target.name, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "duration_seconds": elapsed, "width": 1920, "height": 1080, "fps": 30,
        "speed": 1, "audio": "Removed silent OBS audio track", "frames": expected,
        "edits": shots, "result": spec["result"], "limits": spec["limits"],
        "qa": {"full_decode": "passed", "decoded_frames": decoded_frames[-1], "black_intervals": [],
               "duration_dimensions_fps_frames": "passed", "audio_streams": 0, "contact_pages": pages,
               "visual_review": "pending"},
    }
    for key in ("role", "temporal_context", "scenario_context"):
        if key in spec:
            record[key] = spec[key]
    (OUT / f"{name}-edit-manifest.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"file": str(target), "duration": elapsed, "frames": expected}), flush=True)


def publish(reviewed):
    records = []
    for path in OUT.glob("*-edit-manifest.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["name"] in reviewed:
            record["qa"]["visual_review"] = "passed: source boundary frames and all encoded contact pages reviewed; successful actions and their resulting UI states retained"
            path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        records.append(record)
    records.sort(key=lambda r: list(RAW).index(r["name"]))
    package = {
        "type": "Per-flow action-to-result selects for editing; not a final montage",
        "source_session": "2026-09-14-continuous-coverage",
        "scope": "Product-flow clips are an isolated local Northgate simulation. The separate recorded_sources clip inspects saved independent verification artifacts; its provenance and limits must accompany use. No real community outcome or actual supplier work/payment is claimed.",
        "transform": "Normal speed, full 1920x1080 frame, straight cuts removing waits; no captions, slides, freezes, generated content or transitions. Silent OBS audio omitted.",
        "raw_preserved": True, "excluded_take": "2026-09-14 02-50-17.mp4: browser debug infobar",
        "clips": records,
    }
    (OUT / "edit-manifest.json").write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")
    lines = ["# Steward — işlem ve sonuç kesitleri", "", "Bu klasör final kurgu değildir. Kesintisiz OBS çekimlerinden, her iş akışını ayrı değerlendirmek için hazırlanmış normal hızlı önizlemelerdir. Ham dosyalar korunur.", "", "Ürün akışı görüntüleri aynı izole yerel simülasyondan gelir. Gerçek topluluk toplantısı, ilan yayımlanması veya firmaya teslim iddiası taşımaz. `recorded_sources` bunlardan ayrı, daha önce kaydedilmiş doğrulama dosyalarının okunmasıdır. Kaynaktaki Demo/Synthetic ve Recorded verification etiketleri korunur; ek başlık veya altyazı eklenmemiştir.", "", "| Kesit | Süre | Dosya |", "|---|---:|---|"]
    for record in records:
        lines.append(f"| {record['title']} | {record['duration_seconds']:g} s | [{record['name']}]({record['output']}) |")
    if any(r["name"] == "completion" for r in records):
        lines += ["", "Randevu ile bakım tamamlandı kaydı arasındaki demo saati ekran dışında 48 saat ilerletildi. Bu iki çekim birleştirilirse `Demo time advanced · 48 hours` bağlamı belirtilmelidir. Yönetici tamamlandı kaydı ve sakin doğrulaması ayrı dosyalardır; rol değişimi ve giriş ekran dışında yapıldı. Sonraki yönetici hafıza kesiti kapanmış kaydı gösterir. Gerçek 48 saat, firma ziyareti, ödeme veya sahada doğrulama iddiası yoktur."]
    if any(r["name"] == "deadline_review" for r in records):
        lines += ["", "`Demo extra` açıklama/garanti ve yeter sayı senaryoları, önceki başarıyla kapanmış vakaların devamı gibi birleştirilmemelidir. Ek toplantının uygunluk yanıtından sonraki son tarih incelemesinden önce demo saati ekran dışında 24 saat ilerletildi ve worker çalıştı; bağlam `Demo time advanced · 24 hours` olmalıdır. Uygunluk kaydı toplantı kesinleşmesi değildir. Son tarih kesitinde eski seçenekler devre dışıdır, yeni toplantı gönderilmez. Ayarlar kesiti salt okunurdur ve yerel Telegram bağlantısı yapılandırılmamıştır."]
    if any(r["name"] == "recorded_sources" for r in records):
        lines += ["", "Kaydedilmiş teknik kaynaklar canlı çalıştırma değildir: AgentCore sentetik smoke testi insan incelemesi sonucu üretmiştir; restart kaydı yalnız vaka/outbox korunması ve worker değişimini gösterir. Telegram kaydı 13 Eylül'deki ayrı gerçek hosted bağlantı teslim testidir. SES kaydı 11 Eylül'de izole yerel SQLite ile yapılan gerçek SES/model testidir; kontrollü adresler kullanılmış, gerçek firma/iş/ödeme yapılmamıştır. Bu bağımsız kanıtlar, yeni çekilen ürün akışını gerçek kanal koşusu hâline getirmez. Kaynak başlıkları ve kapsam açıklamaları kurgu sırasında korunmalıdır."]
    if any(r["name"] == "message_to_case" for r in records):
        lines += ["", "`message_to_case` sonradan çekilmiş alternatif açılıştır: sentetik sakin mesajı, alım bildirimi, normal worker tick'i ve yeni toplantı vakasının açılması görünür. Benzer başlıklı ilk otopark vakası daha önce kapanmıştır; bu yeni vaka onun önceki sahnesi gibi birleştirilmemelidir. Tarih alanı boştur; davet, yeter sayı veya karar onayı bu kesitte yoktur."]
    lines += ["", "Kesitler 1920×1080, 30 fps, sessiz H.264 MP4'tür. Yalnız bekleme/odak ayarlama aralıkları kesildi. Hızlandırma, dondurulmuş kare, sahte geçiş veya görüntü üzerinde veri değiştirme yoktur. Sayfa kaydırmaları ve gerçek işlem-sonuç ilişkileri tutuldu.", "", "Her kesitin JSON manifestinde kaynak dosya hash'i, başlangıç/bitiş saniyeleri, kesit içi karşılığı, görünür sonuç ve kanıt sınırı bulunur. Süreler işlemlerin performans ölçümü olarak kullanılamaz. Tam decode, kare sayısı, boyut, fps, ses yokluğu ve siyah kare aralıkları kontrol edilir. İncelenen kodlanmış kareler `qa/<ad>/` içindedir.", "", "Toplantı tutanağı sonrasındaki yeşil genel bildirim kararın onaylandığı anlamına gelmez. Sonraki inceleme görevi worker devamından sonra görünür. Karar onayı, görev tamamlandı kaydı ve sonuç doğrulaması ayrı hareketler olarak korunmuştur.", "", "Kaynak: `../raw/`; ana çekim kapsamı: `../coverage-manifest.json`. `../raw/2026-09-14 02-50-17.mp4` hata ayıklama çubuğu nedeniyle kullanılmadı. Eski reddedilmiş deneme kayıtları bu kesitlere dahil değildir.", "", "Yeniden üretim: proje kökünden, Pillow içeren Python ile `python tools/render_coverage_selects.py --render` çalıştırılır. `--only <ad>` tek kesiti işler. `--probe --only <ad>` kaynak sınır karelerini çıkarır. Görsel inceleme tamamlandıktan sonra `--publish --reviewed <adlar>` manifestleri toplar."]
    (OUT / "README_TR.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--only")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--reviewed", nargs="*", default=[])
    parser.add_argument("--overview", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.probe:
        probes(args.only)
    if args.render:
        for name in SPECS:
            if not args.only or name == args.only:
                render(name)
    if args.publish:
        publish(args.reviewed)
    if args.overview:
        overview(args.only)
