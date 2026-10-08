"use client";

import { useOpenCount } from "@/lib/resolved-store";
import styles from "./shell.module.css";

export function NeedsBadge({ ids, variant = "inline" }: { ids: string[]; variant?: "inline" | "corner" }) {
  const count = useOpenCount(ids);
  if (count === 0) return null;
  return (
    <span className={variant === "corner" ? styles.badgeCorner : styles.badge}>
      <span className="visually-hidden">, </span>
      {count}
      <span className="visually-hidden"> waiting</span>
    </span>
  );
}
