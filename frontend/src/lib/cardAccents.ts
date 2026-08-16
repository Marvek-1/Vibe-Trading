/**
 * Shared rotating color-accent palette for "liquid glass" cards, so every
 * page's card grid (Home features, Alpha Zoo library cards, Agent skill /
 * example cards, Runtime panels, ...) reads as one consistent design system
 * instead of each page inventing its own hues. Pairs with the base
 * `.glass-surface`/`.glass-card` classes in liquid-glass.css -- this only
 * supplies the per-card color identity on top of that shared glass base.
 */
export interface CardAccent {
  /** Tailwind gradient stop pair for a `bg-gradient-to-br` tint. */
  gradient: string;
  text: string;
  border: string;
  iconBg: string;
}

export const CARD_ACCENTS: CardAccent[] = [
  { gradient: "from-sky-500/20 to-sky-500/5", text: "text-sky-600 dark:text-sky-400", border: "hover:border-sky-500/50", iconBg: "bg-sky-500/10" },
  { gradient: "from-emerald-500/20 to-emerald-500/5", text: "text-emerald-600 dark:text-emerald-400", border: "hover:border-emerald-500/50", iconBg: "bg-emerald-500/10" },
  { gradient: "from-violet-500/20 to-violet-500/5", text: "text-violet-600 dark:text-violet-400", border: "hover:border-violet-500/50", iconBg: "bg-violet-500/10" },
  { gradient: "from-amber-500/20 to-amber-500/5", text: "text-amber-600 dark:text-amber-400", border: "hover:border-amber-500/50", iconBg: "bg-amber-500/10" },
  { gradient: "from-rose-500/20 to-rose-500/5", text: "text-rose-600 dark:text-rose-400", border: "hover:border-rose-500/50", iconBg: "bg-rose-500/10" },
  { gradient: "from-cyan-500/20 to-cyan-500/5", text: "text-cyan-600 dark:text-cyan-400", border: "hover:border-cyan-500/50", iconBg: "bg-cyan-500/10" },
];

export function cardAccent(index: number): CardAccent {
  return CARD_ACCENTS[index % CARD_ACCENTS.length];
}
