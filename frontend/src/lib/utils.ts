import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * Parse a server timestamp to epoch ms, treating a timezone-less string as UTC.
 * The backend emits UTC datetimes but may serialize them without a `Z`/offset
 * (e.g. "2026-09-18T20:33:07.113655"); the browser would otherwise parse those as
 * LOCAL time, skewing every duration by the local UTC offset (a fresh run showed
 * "60:03 elapsed" one hour east of UTC). Returns NaN for empty/invalid input.
 */
export function parseServerTime(s: string | null | undefined): number {
  if (!s) return NaN
  const hasTz = /(Z|[+-]\d{2}:?\d{2})$/.test(s)
  return new Date(hasTz ? s : s + "Z").getTime()
}
