import type { Metadata } from "next";
import { SourcesView } from "@/components/SourcesView";
import { LiveSources } from "@/components/live/pages";
import { clientRendered, getSources } from "@/lib/api";

export const metadata: Metadata = { title: "Sources" };

export default async function SourcesPage() {
  if (clientRendered) return <LiveSources />;
  return <SourcesView data={await getSources()} />;
}
