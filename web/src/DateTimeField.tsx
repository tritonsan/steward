import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { CalendarDays, ChevronLeft, ChevronRight, Clock3 } from "lucide-react";
import {
  addCalendarDays,
  DISPLAY_TIME_LABEL,
  MONTHS,
  toWallDateTime,
  validDate,
} from "./dateTime";

export function DateTimeField({
  label,
  value,
  onChange,
  disabled = false,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const id = useId();
  const [date = "", time = "18:00"] = value.split("T");
  const today = toWallDateTime(new Date().toISOString()).slice(0, 10);
  const [open, setOpen] = useState(false);
  const [view, setView] = useState(
    (validDate(date) ? date : today).slice(0, 7),
  );
  const [focused, setFocused] = useState(validDate(date) ? date : today);
  const [timeTouched, setTimeTouched] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const calendar = useRef<HTMLDivElement>(null);
  const [year, month] = view.split("-").map(Number);
  const first = `${view}-01`;
  const offset = (new Date(first + "T00:00:00Z").getUTCDay() + 6) % 7;
  const days = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const timeValid = /^([01]\d|2[0-3]):[0-5]\d$/.test(time);
  const dateLabel = validDate(date)
    ? `${Number(date.slice(8))} ${MONTHS[Number(date.slice(5, 7)) - 1]} ${date.slice(0, 4)}`
    : "Choose date";

  useEffect(() => {
    if (open)
      calendar.current
        ?.querySelector<HTMLButtonElement>(`[data-date="${focused}"]`)
        ?.focus();
  }, [open, focused, view]);

  function close() {
    setOpen(false);
    trigger.current?.focus();
  }
  function moveMonth(delta: number) {
    const next = new Date(Date.UTC(year, month - 1 + delta, 1));
    const nextView = next.toISOString().slice(0, 7);
    if (next.getUTCFullYear() < 1900 || next.getUTCFullYear() > 2100) return;
    setView(nextView);
    setFocused(nextView + "-01");
  }
  function moveDay(event: KeyboardEvent<HTMLButtonElement>, day: string) {
    const shifts: Record<string, number> = {
      ArrowLeft: -1,
      ArrowRight: 1,
      ArrowUp: -7,
      ArrowDown: 7,
    };
    if (event.key === "PageUp" || event.key === "PageDown") {
      event.preventDefault();
      moveMonth(event.key === "PageUp" ? -1 : 1);
      return;
    }
    const weekday = (new Date(day + "T00:00:00Z").getUTCDay() + 6) % 7;
    const delta =
      event.key === "Home"
        ? -weekday
        : event.key === "End"
          ? 6 - weekday
          : shifts[event.key];
    if (delta !== undefined) {
      event.preventDefault();
      const next = addCalendarDays(day, delta);
      if (!validDate(next)) return;
      setFocused(next);
      setView(next.slice(0, 7));
    }
  }
  return (
    <fieldset className="date-time-field" disabled={disabled}>
      <legend>{label}</legend>
      <div className="date-time-row">
        <div className="date-control">
          <span id={`${id}-date-label`} className="date-part-label">
            Date
          </span>
          <button
            type="button"
            ref={trigger}
            className="date-trigger"
            aria-label={`${label}: ${dateLabel}`}
            aria-expanded={open}
            aria-controls={`${id}-calendar`}
            onClick={() => {
              if (open) close();
              else {
                const day = validDate(date) ? date : today;
                setView(day.slice(0, 7));
                setFocused(day);
                setOpen(true);
              }
            }}
          >
            <CalendarDays size={18} aria-hidden="true" />
            <span>{dateLabel}</span>
          </button>
        </div>
        <label className="time-control" htmlFor={`${id}-time`}>
          <span className="date-part-label">Time · 24-hour</span>
          <span className="time-input-wrap">
            <Clock3 size={17} aria-hidden="true" />
            <input
              id={`${id}-time`}
              type="text"
              inputMode="numeric"
              autoComplete="off"
              maxLength={5}
              aria-label={`${label} (24-hour)`}
              placeholder="18:00"
              value={time}
              aria-invalid={timeTouched && !timeValid}
              aria-describedby={`${id}-hint${timeTouched && !timeValid ? ` ${id}-error` : ""}`}
              onBlur={() => setTimeTouched(true)}
              onChange={(e) => {
                const raw = e.target.value;
                onChange(
                  `${date}T${/^\d{4}$/.test(raw) ? raw.slice(0, 2) + ":" + raw.slice(2) : raw}`,
                );
              }}
            />
          </span>
        </label>
      </div>
      <p id={`${id}-hint`} className="date-time-hint">
        {DISPLAY_TIME_LABEL}
      </p>
      {timeTouched && !timeValid && (
        <p id={`${id}-error`} role="alert" className="date-time-error">
          Use 24-hour time, for example 18:30.
        </p>
      )}
      {open && (
        <div
          id={`${id}-calendar`}
          className="date-calendar"
          role="group"
          aria-label={`${label} calendar`}
          ref={calendar}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              e.stopPropagation();
              close();
            }
          }}
        >
          <div className="calendar-heading">
            <button
              type="button"
              className="calendar-nav"
              aria-label="Previous month"
              onClick={() => moveMonth(-1)}
            >
              <ChevronLeft size={19} />
            </button>
            <span aria-live="polite">
              {MONTHS[month - 1]} {year}
            </span>
            <button
              type="button"
              className="calendar-nav"
              aria-label="Next month"
              onClick={() => moveMonth(1)}
            >
              <ChevronRight size={19} />
            </button>
          </div>
          <div className="calendar-weekdays" aria-hidden="true">
            {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => (
              <span key={d}>{d}</span>
            ))}
          </div>
          <div className="calendar-days">
            {Array.from({ length: offset }, (_, i) => (
              <span key={`blank-${i}`} />
            ))}
            {Array.from({ length: days }, (_, i) => {
              const day = `${view}-${String(i + 1).padStart(2, "0")}`;
              return (
                <button
                  type="button"
                  key={day}
                  data-date={day}
                  aria-label={`${i + 1} ${MONTHS[month - 1]} ${year}`}
                  aria-pressed={date === day}
                  aria-current={day === today ? "date" : undefined}
                  tabIndex={focused === day ? 0 : -1}
                  onKeyDown={(e) => moveDay(e, day)}
                  onClick={() => {
                    onChange(`${day}T${time}`);
                    close();
                  }}
                >
                  {i + 1}
                </button>
              );
            })}
          </div>
          <div className="calendar-footer">
            <button
              type="button"
              onClick={() => {
                onChange(`${today}T${time}`);
                close();
              }}
            >
              Today
            </button>
            <button
              type="button"
              onClick={() => {
                onChange("");
                close();
              }}
            >
              Clear
            </button>
            <button type="button" onClick={close}>
              Close calendar
            </button>
          </div>
        </div>
      )}
    </fieldset>
  );
}
