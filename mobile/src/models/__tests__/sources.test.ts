import { describe, expect, it } from "@jest/globals";
import { parseSources, parseUnderstood } from "../../api/guards";
import { sampleSources } from "../../api/sample";
import { buildSourcesView, readsLine } from "../sources";

describe("Sources view (what I read, and what it gave)", () => {
  it("says what is read in one line for Home, and keeps the server's summary and coverage", () => {
    const view = buildSourcesView(sampleSources);
    expect(view.reads).toBe("What I read: 1 mailbox, 3 bank accounts, 4 cards");
    expect(view.summary).toBe("I read 1 mailbox, 3 bank accounts and 4 cards for your 3 companies.");
    expect(view.coverage).toEqual({ text: sampleSources.coverage, tone: "attention" });
  });

  it("lists each source with its own coverage line, in the order the owner thinks of them", () => {
    const view = buildSourcesView(sampleSources);
    expect(view.sections.map((s) => s.id)).toEqual(["email", "banks", "cards", "accountant"]);
    const card = view.sections[2]!.rows.find((r) => r.id === "card-4817")!;
    expect(card).toEqual({
      id: "card-4817",
      name: "Card •••• 4817",
      where: "Company C · CaixaBank · personal card used for business",
      line: "1 payment since 1 September: it needs your answer.",
      status: { label: "Connected", tone: "good" },
    });
  });

  it("shows each company with its tax number and the sources that feed it", () => {
    expect(buildSourcesView(sampleSources).companies[0]).toEqual({
      id: "hazel-tree",
      name: "Hazel Tree",
      tax: "NIF B12345674",
      sources: "laura@hazeltree.es, CaixaBank •••• 0265, Card •••• 5530",
    });
  });

  it("is never green while a source is not read, whatever the summary says (§47)", () => {
    const allGood = { ...sampleSources, tone: "good" as const, coverage: "I checked all 3 payments: every one has its invoice." };
    expect(buildSourcesView(allGood).coverage.tone).toBe("good");
    const stale = {
      ...allGood,
      groups: allGood.groups.map((g) => (g.id === "email" ? { ...g, items: g.items.map((i) => ({ ...i, status: "stale" as const })) } : g)),
    };
    const view = buildSourcesView(stale);
    expect(view.coverage.tone).toBe("attention");
    expect(view.sections[0]!.rows[0]!.status).toEqual({ label: "Needs reconnecting", tone: "attention" });
  });

  it("says plainly when nothing is read yet", () => {
    const empty = { ...sampleSources, groups: [], companies: [] };
    expect(readsLine(empty)).toBe("");
    expect(buildSourcesView(empty).reads).toBe("Nothing is read yet. Connect your email and bank on the web.");
  });
});

describe("Sources from the server", () => {
  const engine = {
    groups: [
      {
        id: "cards",
        title: "Cards",
        description: "Card spending is matched to receipts.",
        items: [
          { id: "card-5530", name: "Card •••• 5530", company: "Hazel Tree", detail: "Millennium BCP", status: "healthy", coverage: { text: "1 payment since 1 September: it has its invoice.", counts: { payments: 1 } } },
          { id: "card-x", name: "Card •••• 9911", company: "Hazel Tree", detail: "Revolut", status: "something new" },
          { name: "no id" },
        ],
      },
    ],
    companies: [{ id: "hazel-tree", name: "Hazel Tree", taxIdLabel: "NIF", taxId: "516123459", sources: [{ id: "card-5530", kind: "card", name: "Card •••• 5530" }] }],
    summary: { text: "I read 2 cards for Hazel Tree.", coverage: "I checked your one payment since 15 September: it has its invoice.", tone: "good" },
  };

  it("keeps what is well formed, and reads an unknown status as needing attention", () => {
    const data = parseSources(engine)!;
    expect(data.summary).toBe("I read 2 cards for Hazel Tree.");
    expect(data.tone).toBe("good");
    expect(data.groups[0]!.items.map((i) => [i.id, i.status, i.coverage])).toEqual([
      ["card-5530", "healthy", "1 payment since 1 September: it has its invoice."],
      ["card-x", "stale", undefined],
    ]);
    expect(data.companies).toEqual([{ id: "hazel-tree", name: "Hazel Tree", taxIdLabel: "NIF", taxId: "516123459", sources: ["Card •••• 5530"] }]);
    expect(buildSourcesView(data).coverage.tone).toBe("attention");
  });

  it("refuses a reply without its summary", () => {
    expect(parseSources({ groups: [], companies: [] })).toBeNull();
    expect(parseSources({ ...engine, summary: { coverage: "x" } })).toBeNull();
  });

  it("reads what Something missing? understood", () => {
    expect(parseUnderstood({ kind: "card", fields: { last4: "4821", bank: "Revolut", n: 3 }, message: "Card •••• 4821 from Revolut." })).toEqual({
      kind: "card",
      fields: { last4: "4821", bank: "Revolut" },
      message: "Card •••• 4821 from Revolut.",
      already: false,
    });
    expect(parseUnderstood({ kind: "rocket", message: "x" })).toBeNull();
  });
});
