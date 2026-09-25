import type { ReactNode } from "react";

type StatePanelProps = {
  title: string;
  description: string;
  kind?: "empty" | "loading" | "error" | "default";
  children?: ReactNode;
};

export function StatePanel({ title, description, kind = "default", children }: StatePanelProps) {
  const role = kind === "error" ? "alert" : "status";
  return (
    <section className="state-panel" role={role} aria-busy={kind === "loading" || undefined}>
      <h1>{title}</h1>
      <p>{description}</p>
      {children}
    </section>
  );
}
