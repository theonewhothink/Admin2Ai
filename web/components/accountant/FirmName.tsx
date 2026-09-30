"use client";

import { useEffect, useState } from "react";
import { useSession } from "@/components/session/context";
import { getAccountantFirm, liveData } from "@/lib/api";
import { accountantFirm } from "@/lib/data";
import { production } from "@/lib/mode";
import styles from "./accountant.module.css";

type Firm = { name: string; person: string };

/**
 * The accountant's firm. Production: the signed-in person's practice. With the engine or a backend: the
 * accountant the business named (never the sample firm). Sample data only: the sample firm.
 */
function useFirm(): Firm | null {
  const session = useSession();
  const [firm, setFirm] = useState<Firm | null>(liveData ? null : accountantFirm);
  useEffect(() => {
    if (!liveData || production) return;
    let live = true;
    void getAccountantFirm().then((f) => {
      if (live && f) setFirm(f);
    });
    return () => {
      live = false;
    };
  }, []);
  if (production) return session ? { name: session.tenant.name || session.user.name, person: session.user.name } : null;
  return firm;
}

function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((p) => p[0]?.toUpperCase() ?? "")
    .join("");
}

export function FirmName() {
  const firm = useFirm();
  return firm ? <>{firm.name}</> : null;
}

/** The header badge outside production (production shows the session's own badge). */
export function FirmBadge() {
  const firm = useFirm();
  if (!firm) return null;
  return (
    <span className={styles.firm}>
      <span className={styles.firmAvatar} aria-hidden="true">
        {initials(firm.person || firm.name)}
      </span>
      <span className={styles.firmName}>{firm.name}</span>
    </span>
  );
}
