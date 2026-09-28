import type { ReactNode } from "react";

type StatePanelProps = {
  title: string;
  description: string;
  kind?: "empty" | "loading" | "error" | "default";
  headingLevel?: 1 | 2 | 3;
  children?: ReactNode;
};

export function StatePanel({ title, description, kind = "default", headingLevel = 2, children }: StatePanelProps) {
  const role = kind === "error" ? "alert" : "status";
  const Heading = headingLevel === 1 ? "h1" : headingLevel === 3 ? "h3" : "h2";
  return (
    <section className="state-panel" role={role} aria-busy={kind === "loading" || undefined}>
      <Heading>{title}</Heading>
      <p>{description}</p>
      {children}
    </section>
  );
}
