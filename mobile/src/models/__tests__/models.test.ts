import { describe, expect, it } from "@jest/globals";
import { sampleActivity, sampleHome, sampleNeedsYou } from "../../api/sample";
import type { NeedsYouApprovalItem, NeedsYouChoiceItem } from "../../api/types";
import { activityTone, groupActivity } from "../activity";
import { buildHomeView, handledToday, reconnectMessage } from "../home";
import { approvalAction, choiceDoneMessage, openItems, rememberFlag, rememberLabel, rememberSentence } from "../needs";
import { sourceNote } from "../source";

const NOW = new Date("2026-10-02T09:30:00+01:00");
const ikea = sampleNeedsYou[0] as NeedsYouChoiceItem;
const vodafone = sampleNeedsYou[1] as NeedsYouApprovalItem;

describe("Home view (§41, §69)", () => {
  it("matches the spec's home: I need 2 things, Needs you 2, September 94%, Handled today 11", () => {
    const view = buildHomeView(sampleHome, 2, NOW);
    expect(view.status).toEqual({ text: "I need two things from you.", tone: "attention" });
    expect(view.tiles.map((t) => [t.label, t.value])).toEqual([
      ["Needs you", "2"],
      ["September", "94%"],
      ["Handled today", "11"],
    ]);
    expect(view.banner).toBeNull();
    expect(view.companies).toHaveLength(3);
  });

  it("says everything is under control only when nothing is pending and sync is healthy", () => {
    expect(buildHomeView(sampleHome, 0, NOW).status).toEqual({ text: "Everything is under control.", tone: "good" });
    expect(buildHomeView(sampleHome, 1, NOW).status.text).toBe("I still need one thing.");
  });

  it("never shows green while a connection is stale (§47-48)", () => {
    const stale = {
      ...sampleHome,
      currentMonth: { key: "2026-09", label: "September", percentClosed: 100 },
      connections: [{ ...sampleHome.connections[0]!, status: "stale" as const, lastSyncedAt: "2026-10-01T14:42:00+01:00" }],
    };
    const view = buildHomeView(stale, 0, NOW);
    expect(view.status).toEqual({ text: "Action required.", tone: "attention" });
    expect(view.tiles.find((t) => t.id === "month")?.tone).toBe("neutral");
    expect(view.banner?.text).toBe("Gmail needs reconnecting. Your email has not synced since 14:42 yesterday.");
  });

  it("uses emerald for the month only when it is fully closed", () => {
    expect(buildHomeView(sampleHome, 0, NOW).tiles[1]!.tone).toBe("neutral");
    const closed = { ...sampleHome, currentMonth: { key: "2026-09", label: "September", percentClosed: 100 } };
    expect(buildHomeView(closed, 0, NOW).tiles[1]!.tone).toBe("good");
  });

  it("derives handled-today from the stats when the period is today, and hides it otherwise", () => {
    const { handledToday: _omit, ...rest } = sampleHome;
    expect(handledToday(rest)).toBe(11);
    expect(handledToday({ ...rest, handledPeriodLabel: "This week" })).toBeNull();
    expect(buildHomeView({ ...rest, handledPeriodLabel: "This week" }, 0, NOW).tiles).toHaveLength(2);
  });

  it("prefers the server's own reconnect label", () => {
    const c = { ...sampleHome.connections[1]!, status: "stale" as const, lastSyncedLabel: "yesterday evening" };
    expect(reconnectMessage(c, NOW)).toBe("CaixaBank needs reconnecting. Your bank has not synced since yesterday evening.");
  });
});

describe("Needs You decisions", () => {
  it("labels the remember checkbox before and after a choice", () => {
    expect(rememberLabel(ikea.remember)).toBe("Always use this answer for IKEA paid with card •••• 4817");
    expect(rememberLabel(undefined)).toBe("Remember this next time");
    expect(rememberSentence(ikea.remember!, "hazel-tree", "Hazel Tree")).toBe("Always use Hazel Tree for IKEA paid with card •••• 4817");
    expect(rememberSentence(ikea.remember!, "personal", "Personal")).toBe("Always treat IKEA paid with card •••• 4817 as personal");
    expect(choiceDoneMessage(ikea, true)).toBe("Done. I will remember this.");
    expect(choiceDoneMessage(ikea, false)).toBe("Done.");
  });

  it("never releases a changed-IBAN payment without the phone check, and always re-checks identity", () => {
    expect(approvalAction(vodafone, "release", false)).toBeNull();
    expect(approvalAction(vodafone, "release", true)).toEqual({
      kind: "release",
      optionId: "confirmed_by_phone",
      message: "Done. The payment will go to the new account.",
      needsIdentityCheck: true,
    });
    expect(approvalAction(vodafone, "keep", false)).toMatchObject({ kind: "keep_blocked", optionId: "keep_blocked" });
  });

  it("never learns from approvals", () => {
    expect(rememberFlag(vodafone, true)).toBe(false);
    expect(rememberFlag(ikea, true)).toBe(true);
    expect(rememberFlag(ikea, false)).toBe(false);
  });

  it("hides answered items", () => {
    expect(openItems(sampleNeedsYou, new Set(["nd_ikea_418"])).map((i) => i.id)).toEqual(["nd_vodafone_iban"]);
  });
});

describe("Activity", () => {
  it("groups by local day, newest first", () => {
    const groups = groupActivity(sampleActivity.items, "2026-10-02");
    expect(groups.map((g) => g.label)).toEqual(["Today", "Yesterday", "Wednesday 30 September", "Tuesday 29 September"]);
    expect(groups[0]!.items[0]!.id).toBe("a01");
  });

  it("drops unreadable timestamps and colours only closures and protections", () => {
    expect(groupActivity([{ id: "x", at: "nope", kind: "checked", text: "t" }], "2026-10-02")).toEqual([]);
    expect(activityTone("closed")).toBe("good");
    expect(activityTone("protected")).toBe("attention");
    expect(activityTone("collected")).toBe("neutral");
  });
});

describe("source note", () => {
  it("is silent for live data and plain otherwise", () => {
    expect(sourceNote({ source: "live", asOf: 1 }, NOW)).toBeNull();
    expect(sourceNote({ source: "sample", asOf: null, reason: "demo" }, NOW)).toBe(
      "Example data. Connect the app to your account to see your business.",
    );
    expect(sourceNote({ source: "sample", asOf: null, reason: "unreachable" }, NOW)).toBe(
      "I can't reach your business right now. These are example figures.",
    );
    expect(sourceNote({ source: "cached", asOf: new Date("2026-10-02T09:12:00+01:00").getTime(), reason: "unreachable" }, NOW)).toBe(
      "You're offline. Showing what I knew at 09:12 today.",
    );
  });
});
