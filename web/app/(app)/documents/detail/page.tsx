import type { Metadata } from "next";
import { Suspense } from "react";
import { DocumentDetailView } from "@/components/documents/DocumentDetail";
import { Loading } from "@/components/live/Loading";

export const metadata: Metadata = { title: "Document" };

/** One document (`?id=`), read in the browser: new documents arrive at run time. */
export default function DocumentPage() {
  return (
    <Suspense fallback={<Loading />}>
      <DocumentDetailView />
    </Suspense>
  );
}
