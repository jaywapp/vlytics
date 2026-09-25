import type { ReactNode } from "react";

export type BadgeTone = "neutral" | "warning";

export function StatusBadge({ children, tone = "neutral" }: { children: ReactNode; tone?: BadgeTone }) {
  return <span className={`status-badge status-badge--${tone}`}>{children}</span>;
}
