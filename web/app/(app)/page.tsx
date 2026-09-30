import { Suspense } from "react";
import { HomeView } from "@/components/home/HomeView";
import { Loading } from "@/components/live/Loading";
import { LiveHome } from "@/components/live/pages";
import { clientRendered, getHome, getNeedsYou } from "@/lib/api";

export default async function HomePage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  if (clientRendered) {
    return (
      <Suspense fallback={<Loading narrow={false} />}>
        <LiveHome />
      </Suspense>
    );
  }
  const [params, home, needs] = await Promise.all([searchParams, getHome(), getNeedsYou()]);
  return <HomeView home={home} needsIds={needs.map((n) => n.id)} demoStale={params.demo === "stale"} />;
}
