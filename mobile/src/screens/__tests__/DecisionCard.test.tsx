import { describe, expect, it, jest } from "@jest/globals";
import { fireEvent, render, screen } from "@testing-library/react-native";
import type { ApiClient } from "../../api/client";
import { sampleNeedsYou } from "../../api/sample";
import { ServicesContext, type Services } from "../../app/servicesContext";
import { AppLock, type AuthResult } from "../../security/lock";
import { DecisionCard } from "../DecisionCard";

const ikea = sampleNeedsYou[0]!;
const vodafone = sampleNeedsYou[1]!;

function setup(options: { answerOk?: boolean; auth?: AuthResult[] } = {}) {
  const answer = jest.fn(async (_id: string, _option: string, _remember: boolean) => ({ ok: options.answerOk ?? true }));
  const results = [...(options.auth ?? [])];
  const authenticate = jest.fn(async (_prompt: string): Promise<AuthResult> => results.shift() ?? { ok: true });
  const lock = new AppLock({ securityLevel: async () => "biometric", authenticate }, () => 0);
  const services = { api: { answer } as unknown as ApiClient, lock, offline: {} } as unknown as Services;
  const onResolved = jest.fn();
  const renderCard = (item = ikea) =>
    render(
      <ServicesContext.Provider value={services}>
        <DecisionCard item={item} onResolved={onResolved} />
      </ServicesContext.Provider>,
    );
  return { answer, authenticate, onResolved, renderCard };
}

describe("choice card (one tap + remember)", () => {
  it("shows the decision plainly and answers in one tap, remembering by default", async () => {
    const { answer, renderCard } = setup();
    await renderCard();
    expect(screen.getByText("IKEA")).toBeTruthy();
    expect(screen.getByText("€418.00")).toBeTruthy();
    expect(screen.getByText("29 September · paid with card •••• 4817")).toBeTruthy();
    const box = screen.getByRole("checkbox", { name: "Always use this answer for IKEA paid with card •••• 4817" });
    expect(box.props.accessibilityState).toMatchObject({ checked: true });

    await fireEvent.press(screen.getByRole("button", { name: "Hazel Tree" }));
    expect(answer).toHaveBeenCalledWith("nd_ikea_418", "hazel-tree", true);
    expect(await screen.findByText("Done. I will remember this.")).toBeTruthy();
  });

  it("sends remember=false when the box is unticked", async () => {
    const { answer, renderCard } = setup();
    await renderCard();
    await fireEvent.press(screen.getByRole("checkbox"));
    await fireEvent.press(screen.getByRole("button", { name: "Personal" }));
    expect(answer).toHaveBeenCalledWith("nd_ikea_418", "personal", false);
    expect(await screen.findByText("Done.")).toBeTruthy();
  });

  it("opens follow-up choices inline and answers with the chosen company", async () => {
    const { answer, renderCard } = setup();
    await renderCard();
    expect(screen.queryByText("Company B")).toBeNull();
    await fireEvent.press(screen.getByRole("button", { name: "Another company" }));
    expect(answer).not.toHaveBeenCalled();
    await fireEvent.press(screen.getByRole("button", { name: "Company B" }));
    expect(answer).toHaveBeenCalledWith("nd_ikea_418", "company-b", true);
  });

  it("keeps the card and says so plainly when the answer could not be sent", async () => {
    const { renderCard, onResolved } = setup({ answerOk: false });
    await renderCard();
    await fireEvent.press(screen.getByRole("button", { name: "Hazel Tree" }));
    expect(await screen.findByText("I couldn't send that. Check your connection and try again.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Hazel Tree" })).toBeTruthy();
    expect(onResolved).not.toHaveBeenCalled();
  });

  it("explains why on request", async () => {
    const { renderCard } = setup();
    await renderCard();
    expect(screen.queryByText(ikea.why[0]!)).toBeNull();
    await fireEvent.press(screen.getByText("Why am I seeing this?"));
    expect(screen.getByText(ikea.why[0]!)).toBeTruthy();
  });
});

describe("approval card (changed bank details, §26)", () => {
  it("cannot release until the phone check is ticked, then asks for identity and never remembers", async () => {
    const { answer, authenticate, renderCard } = setup({ auth: [{ ok: true }] });
    await renderCard(vodafone);
    expect(screen.getByText("LT61 3250 •••• •••• 1187")).toBeTruthy();

    await fireEvent.press(screen.getByRole("button", { name: "Confirm with Vodafone by phone" }));
    const release = screen.getByRole("button", { name: "They confirmed it. Release the payment." });
    expect(release.props.accessibilityState).toMatchObject({ disabled: true });
    await fireEvent.press(release);
    expect(answer).not.toHaveBeenCalled();

    await fireEvent.press(screen.getByRole("checkbox", { name: "I called and Vodafone confirmed the account ending in 1187." }));
    await fireEvent.press(screen.getByRole("button", { name: "They confirmed it. Release the payment." }));
    expect(authenticate).toHaveBeenCalledWith("Confirm it's you");
    expect(answer).toHaveBeenCalledWith("nd_vodafone_iban", "confirmed_by_phone", false);
    expect(await screen.findByText("Done. The payment will go to the new account.")).toBeTruthy();
  });

  it("does not release when the identity check is cancelled", async () => {
    const { answer, renderCard } = setup({ auth: [{ ok: false, reason: "cancelled" }] });
    await renderCard(vodafone);
    await fireEvent.press(screen.getByRole("button", { name: "Confirm with Vodafone by phone" }));
    await fireEvent.press(screen.getByRole("checkbox"));
    await fireEvent.press(screen.getByRole("button", { name: "They confirmed it. Release the payment." }));
    expect(answer).not.toHaveBeenCalled();
  });

  it("keeps the payment blocked in one tap without an identity check", async () => {
    const { answer, authenticate, renderCard } = setup();
    await renderCard(vodafone);
    await fireEvent.press(screen.getByRole("button", { name: "Keep blocked" }));
    expect(authenticate).not.toHaveBeenCalled();
    expect(answer).toHaveBeenCalledWith("nd_vodafone_iban", "keep_blocked", false);
    expect(await screen.findByText("Done. It stays blocked. I will ask Vodafone for a corrected invoice.")).toBeTruthy();
  });
});
