/** Compose a friendly user display name. Falls back gracefully. */
export function userDisplayName(u: {
  username?: string | null;
  first_name?: string | null;
  last_name?: string | null;
  user_id: number;
}): string {
  const parts = [u.first_name, u.last_name].filter(Boolean) as string[];
  if (parts.length) return parts.join(" ");
  if (u.username) return `@${u.username}`;
  return `user ${u.user_id}`;
}

/** "3 days ago", "just now", … */
export function relTime(d: Date | string): string {
  const date = typeof d === "string" ? new Date(d) : d;
  const diffMs = Date.now() - date.getTime();
  const sec = Math.floor(diffMs / 1000);
  if (sec < 30) return "just now";
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} min ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} hr ago`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day} day${day === 1 ? "" : "s"} ago`;
  const month = Math.floor(day / 30);
  if (month < 12) return `${month} mo ago`;
  return `${Math.floor(month / 12)} yr ago`;
}

/** Local date+time, short. */
export function dateTime(d: Date | string): string {
  const date = typeof d === "string" ? new Date(d) : d;
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** YYYY-MM-DD, useful for the activity feed groupings. */
export function dayKey(d: Date | string): string {
  const date = typeof d === "string" ? new Date(d) : d;
  return date.toISOString().slice(0, 10);
}
