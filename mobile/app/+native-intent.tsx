/**
 * expo-router hook for incoming system URLs. Share Extension links
 * (`<scheme>://dataUrl=…`) are not screens; send them Home, where the share
 * handler reads the payload (§12).
 */
import { redirectSharePath } from "../src/share/nativeIntent";

export function redirectSystemPath({ path }: { path: string; initial: boolean }): string {
  return redirectSharePath(path);
}
