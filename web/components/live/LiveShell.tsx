"use client";

import { AppHeader } from "@/components/shell/AppHeader";
import { BottomNav } from "@/components/shell/BottomNav";
import { getNeedsYou } from "@/lib/api";
import type { Owner } from "@/lib/types";
import { useData } from "./useData";

const loadNeedsIds = async () => (await getNeedsYou()).map((i) => i.id);
const NONE: string[] = [];

/** The app header and bottom bar on the static site: the needs-you count comes from the in-browser engine. */
export function LiveHeader({ owner }: { owner: Owner }) {
  const ids = useData(loadNeedsIds) ?? NONE;
  return <AppHeader needsIds={ids} owner={owner} sampleData={false} />;
}

export function LiveBottomNav() {
  const ids = useData(loadNeedsIds) ?? NONE;
  return <BottomNav needsIds={ids} />;
}
