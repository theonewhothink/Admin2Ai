import type { Metadata } from "next";
import { SourcesView } from "@/components/SourcesView";
import { LiveSources } from "@/components/live/pages";
import { browserEngine, getSources } from "@/lib/api";

export const metadata: Metadata = { title: "Sources" };

export default async function SourcesPage() {
  if (browserEngine) return <LiveSources />;
  return <SourcesView data={await getSources()} />;
}
