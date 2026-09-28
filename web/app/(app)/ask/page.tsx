import type { Metadata } from "next";
import { Suspense } from "react";
import { AskClient } from "@/components/ask/AskClient";
import { LiveAsk } from "@/components/live/pages";
import { browserEngine } from "@/lib/api";
import { askExamples } from "@/lib/data";

export const metadata: Metadata = { title: "Ask" };

function Head() {
  return (
    <header className="page-head">
      <h1 className="h1">Ask</h1>
      <p className="lead">Anything about your business. I answer from your email, bank and documents, and show you where.</p>
    </header>
  );
}

export default async function AskPage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  if (browserEngine) {
    return (
      <div className="container-narrow page">
        <Head />
        <Suspense fallback={<AskClient examples={askExamples} />}>
          <LiveAsk examples={askExamples} />
        </Suspense>
      </div>
    );
  }
  const { q } = await searchParams;
  const initialQuestion = typeof q === "string" && q.trim() ? q.trim().slice(0, 500) : undefined;
  return (
    <div className="container-narrow page">
      <Head />
      <AskClient key={initialQuestion ?? "empty"} initialQuestion={initialQuestion} examples={askExamples} />
    </div>
  );
}
