"use client";

import { useSession } from "@/components/session/context";
import { AppHeader } from "@/components/shell/AppHeader";
import { BottomNav } from "@/components/shell/BottomNav";
import { getNeedsYou } from "@/lib/api";
import { ownerFrom } from "@/lib/owner";
import type { NeedsYouItem, Owner } from "@/lib/types";
import { useData } from "./useData";

// The badge is a hint: if the list can't load, the page itself says so.
const loadNeedsIds = async () => (await getNeedsYou().catch((): NeedsYouItem[] => [])).map((i) => i.id);
const NONE: string[] = [];

/**
 * The app header and bottom bar when pages load their data in the browser:
 * the needs-you count comes from the in-browser engine (demo) or the API
 * (production). In production the owner is the signed-in user.
 */
export function LiveHeader({ owner }: { owner?: Owner }) {
  const ids = useData(loadNeedsIds) ?? NONE;
  const session = useSession();
  const who = owner ?? (session ? ownerFrom(session) : undefined);
  return <AppHeader needsIds={ids} owner={who} sampleData={false} />;
}

export function LiveBottomNav() {
  const ids = useData(loadNeedsIds) ?? NONE;
  return <BottomNav needsIds={ids} />;
}
