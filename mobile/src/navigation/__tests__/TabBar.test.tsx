import { describe, expect, it, jest } from "@jest/globals";
import { fireEvent, render, screen } from "@testing-library/react-native";
import type { ApiClient } from "../../api/client";
import { sampleNeedsYou } from "../../api/sample";
import { NeedsProvider } from "../../app/needs";
import { ServicesContext, type Services } from "../../app/servicesContext";
import { TabBar } from "../TabBar";
import { badgeText, TABS } from "../tabs";

type Props = Parameters<typeof TabBar>[0];

function props(index = 0) {
  const navigate = jest.fn();
  const emit = jest.fn(() => ({ defaultPrevented: false }));
  const routes = TABS.map((t) => ({ key: `${t.route}-key`, name: t.route, params: undefined }));
  const p = {
    state: { index, routes },
    navigation: { emit, navigate },
    insets: { top: 0, bottom: 20, left: 0, right: 0 },
    descriptors: {},
  } as unknown as Props;
  return { p, navigate, emit };
}

async function renderBar(index = 0) {
  const api = {
    getNeedsYou: async () => ({ data: sampleNeedsYou, source: "sample" as const, asOf: null }),
  } as unknown as ApiClient;
  const services = { api, lock: {}, offline: {} } as unknown as Services;
  const { p, navigate, emit } = props(index);
  await render(
    <ServicesContext.Provider value={services}>
      <NeedsProvider>
        <TabBar {...p} />
      </NeedsProvider>
    </ServicesContext.Provider>,
  );
  return { navigate, emit };
}

describe("TabBar (§40)", () => {
  it("shows exactly Home, Needs You, Scan, Activity, Ask with the Scan button in the centre", async () => {
    await renderBar();
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.props.accessibilityLabel)).toEqual(["Home", "Needs You, 2", "Scan", "Activity", "Ask"]);
    expect(TABS.findIndex((t) => t.center)).toBe(2);
  });

  it("navigates on press and marks the current tab", async () => {
    const { navigate, emit } = await renderBar(0);
    expect(screen.getByRole("tab", { name: "Home" }).props.accessibilityState).toMatchObject({ selected: true });
    await fireEvent.press(screen.getByRole("tab", { name: "Scan" }));
    expect(emit).toHaveBeenCalledWith({ type: "tabPress", target: "scan-key", canPreventDefault: true });
    expect(navigate).toHaveBeenCalledWith("scan", undefined);
  });

  it("caps the badge and hides it at zero", () => {
    expect(badgeText(0)).toBeNull();
    expect(badgeText(3)).toBe("3");
    expect(badgeText(42)).toBe("9+");
  });
});
