import type { Metadata } from "next";
import { Suspense } from "react";
import { ChatClient } from "@/components/ask/ChatClient";
import { LiveAsk } from "@/components/live/pages";
import { browserEngine } from "@/lib/api";
import { askExamples } from "@/lib/data";

export const metadata: Metadata = { title: "Ask" };

function Head() {
  return (
    <header className="page-head">
      <h1 className="h1">Ask</h1>
      <p className="lead">Ask anything or give me a task: find and send documents, build reports, check a supplier. I answer from your email, bank and documents, and nothing is sent until you tap Send.</p>
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
        <Suspense fallback={<ChatClient examples={askExamples} />}>
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
      <ChatClient key={initialQuestion ?? "empty"} initialQuestion={initialQuestion} examples={askExamples} />
    </div>
  );
}
