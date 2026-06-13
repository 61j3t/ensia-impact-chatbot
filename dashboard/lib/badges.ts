/**
 * Badge metadata mirrors `chatbot/gamification.py` BADGES list. Kept in
 * sync by hand — there are 12 entries and they don't change often.
 *
 * The dashboard doesn't re-compute badge eligibility (the bot does that
 * and persists into `users.badges` JSONB). We just look up name + emoji
 * + description here.
 */

export type Badge = {
  key: string;
  emoji: string;
  name: string;
  description: string;
};

export const BADGES: Badge[] = [
  { key: "curious", emoji: "🎯", name: "Curious Mind", description: "Asked their first question" },
  { key: "scholar_25", emoji: "🎓", name: "Scholar", description: "Asked 25+ questions" },
  { key: "scholar_100", emoji: "📚", name: "Veteran Scholar", description: "Asked 100+ questions" },
  { key: "scholar_500", emoji: "🧠", name: "Knowledge Seeker", description: "Asked 500+ questions" },
  { key: "polyglot", emoji: "🌍", name: "Polyglot", description: "Asked in English, French, and Arabic" },
  { key: "deep_diver", emoji: "🤿", name: "Deep Diver", description: "Used /deep 5 times for careful answers" },
  { key: "night_owl", emoji: "🦉", name: "Night Owl", description: "Asked 10 questions between midnight and 5 AM" },
  { key: "early_bird", emoji: "🌅", name: "Early Bird", description: "Asked 10 questions before 8 AM" },
  { key: "researcher", emoji: "🔬", name: "Researcher", description: "Got 10 answers backed by PDF sources" },
  { key: "helper", emoji: "🗣️", name: "Helper", description: "Gave 10 helpful 👍 reactions" },
  { key: "on_fire", emoji: "🔥", name: "On Fire", description: "Maintained a 7-day streak" },
  { key: "topic_explorer", emoji: "🗺️", name: "Topic Explorer", description: "Got answers from 5+ ENSIA channels" },
];

export const BADGES_BY_KEY: Record<string, Badge> =
  Object.fromEntries(BADGES.map((b) => [b.key, b]));
