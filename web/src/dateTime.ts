/** Northgate's demo presentation zone. Persisted instants remain UTC. */
export const DISPLAY_TIME_ZONE = "Europe/Istanbul";
export const DISPLAY_TIME_LABEL = "Istanbul · UTC+03:00";
export const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];
const pad = (n: number) => String(n).padStart(2, "0");

function zonedParts(instant: Date) {
  return Object.fromEntries(
    new Intl.DateTimeFormat("en-GB", {
      timeZone: DISPLAY_TIME_ZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(instant)
      .map((p) => [p.type, p.value]),
  );
}

export function toWallDateTime(iso: string): string {
  const instant = new Date(iso);
  if (!Number.isFinite(instant.getTime())) return "";
  const p = zonedParts(instant);
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}

export function validDate(date: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return false;
  const [y, m, d] = date.split("-").map(Number);
  const check = new Date(Date.UTC(y, m - 1, d));
  return (
    y >= 1900 &&
    y <= 2100 &&
    check.getUTCFullYear() === y &&
    check.getUTCMonth() === m - 1 &&
    check.getUTCDate() === d
  );
}

export function validDateTime(value: string): boolean {
  const [date, time, extra] = value.split("T");
  return (
    extra === undefined &&
    validDate(date || "") &&
    /^([01]\d|2[0-3]):[0-5]\d$/.test(time || "")
  );
}

export function toUtcDateTime(value: string): string {
  if (!validDateTime(value))
    throw new Error("Choose a valid date and enter a time as HH:MM.");
  const [y, mo, d, h, mi] = value.split(/[-T:]/).map(Number);
  const wall = Date.UTC(y, mo - 1, d, h, mi);
  let instant = wall;
  for (let i = 0; i < 3; i++) {
    const p = zonedParts(new Date(instant));
    const delta =
      wall - Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute);
    instant += delta;
    if (!delta) break;
  }
  const iso = new Date(instant).toISOString();
  if (toWallDateTime(iso) !== value)
    throw new Error("This local time does not exist. Choose another time.");
  return iso;
}

export function addCalendarDays(date: string, days: number): string {
  if (!validDate(date)) throw new Error("Invalid calendar date");
  const [y, mo, d] = date.split("-").map(Number);
  const next = new Date(Date.UTC(y, mo - 1, d + days));
  return `${next.getUTCFullYear()}-${pad(next.getUTCMonth() + 1)}-${pad(next.getUTCDate())}`;
}

export function retryMeetingSlots(start: string, end: string) {
  if (!validDateTime(start) || !validDateTime(end) || start > end) return [];
  const slots = [];
  let next = start;
  while (next <= end && slots.length < 12) {
    slots.push({
      slot_id: `retry-${slots.length + 1}`,
      starts_at: toUtcDateTime(next),
    });
    next = addCalendarDays(next.slice(0, 10), 1) + next.slice(10);
  }
  return slots;
}

export const formatDateTime = (
  value: string,
  options: Intl.DateTimeFormatOptions = {},
) =>
  new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    ...options,
    timeZone: DISPLAY_TIME_ZONE,
    hourCycle: "h23",
  }).format(new Date(value));
export const formatDate = (value: string) =>
  new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: DISPLAY_TIME_ZONE,
  }).format(new Date(value));
