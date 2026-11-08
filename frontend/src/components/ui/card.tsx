// shadcn/ui-style components (MIT), vendored and trimmed.
import * as React from "react";
import { cn } from "@/lib/utils";
import type { Classification } from "@/lib/api";

export function Card({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("rounded-lg border border-border bg-white shadow-sm", className)} {...props} />;
}

export function Badge({ className, ...props }: React.HTMLAttributes<HTMLSpanElement>) {
  return (
    <span
      className={cn("inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium", className)}
      {...props}
    />
  );
}

const CLS: Record<Classification, string> = {
  public: "border-public/40 bg-public/10 text-public",
  internal: "border-internal/40 bg-internal/10 text-internal",
  confidential: "border-confidential/40 bg-confidential/10 text-confidential",
};

export function ClassBadge({ value }: { value: Classification }) {
  return <Badge className={CLS[value]}>{value}</Badge>;
}

export function Table({ className, ...props }: React.TableHTMLAttributes<HTMLTableElement>) {
  return (
    <div className="overflow-x-auto">
      <table className={cn("w-full text-sm [&_td]:px-3 [&_td]:py-2 [&_th]:px-3 [&_th]:py-2", className)} {...props} />
    </div>
  );
}

export function Th(props: React.ThHTMLAttributes<HTMLTableCellElement>) {
  return <th className="text-left text-xs font-medium text-muted-foreground" {...props} />;
}
