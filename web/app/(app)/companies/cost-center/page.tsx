import type { Metadata } from "next";
import { Suspense } from "react";
import { CostCenterView } from "@/components/companies/CostCenterView";
import { Loading } from "@/components/live/Loading";

export const metadata: Metadata = { title: "Job, property or vehicle" };

/**
 * One cost center (`?id=`): ids are made at run time when the owner adds one, so the page reads its id
 * in the browser (the static demo cannot pre-build a page per id).
 */
export default function CostCenterPage() {
  return (
    <Suspense fallback={<Loading />}>
      <CostCenterView />
    </Suspense>
  );
}
