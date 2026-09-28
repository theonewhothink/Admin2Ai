/**
 * The iOS Share Extension opens the app with `<scheme>://dataUrl=<key>…`.
 * That is not a screen, so expo-router must not try to route it; the root
 * layout's share handler reads the payload instead (§12).
 */
export function redirectSharePath(path: string): string {
  return path.includes("dataUrl=") ? "/" : path;
}
