import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";

const MESSAGES = [
  "Agent is working…",
  "Reading the request…",
  "Reaching for market data…",
  "Checking exchange coverage…",
  "Cross-referencing timestamps…",
  "Warming up the model…",
  "Assembling the strategy…",
  "Running the numbers…",
  "Weighing the evidence…",
  "Almost there…",
];

const ROTATE_MS = 2600;

/**
 * Cycles through churn phrases while the agent is thinking/fetching/executing
 * with no more specific status to show yet. Purely cosmetic -- the underlying
 * wait time (real ReAct iterations, real tool calls) is unchanged.
 */
export function RotatingWorkingStatus() {
  const [index, setIndex] = useState(0);

  useEffect(() => {
    const id = window.setInterval(() => {
      setIndex((prev) => (prev + 1) % MESSAGES.length);
    }, ROTATE_MS);
    return () => window.clearInterval(id);
  }, []);

  return (
    <div className="flex items-center gap-2 text-xs text-muted-foreground pt-1">
      <Loader2 className="h-3 w-3 animate-spin text-primary shrink-0" />
      <span>{MESSAGES[index]}</span>
    </div>
  );
}
