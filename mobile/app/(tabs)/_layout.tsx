/** Bottom tabs (§40): Home, Needs You, Scan (centre), Activity, Ask. */
import { Tabs } from "expo-router";
import { TabBar } from "../../src/navigation/TabBar";
import { TABS } from "../../src/navigation/tabs";

export default function TabsLayout() {
  return (
    <Tabs screenOptions={{ headerShown: false }} tabBar={(props) => <TabBar {...props} />}>
      {TABS.map((tab) => (
        <Tabs.Screen key={tab.route} name={tab.route} options={{ title: tab.label }} />
      ))}
    </Tabs>
  );
}
