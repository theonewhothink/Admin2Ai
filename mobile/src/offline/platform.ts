/**
 * Pure rules used by the Expo adapters, kept here so they are unit-tested.
 */

/** expo-network state: online only when connected and not known to be unreachable. */
export function isReachable(state: { isConnected?: boolean; isInternetReachable?: boolean }): boolean {
  return state.isConnected === true && state.isInternetReachable !== false;
}

/** Blob keys become file names, so only allow a safe character set (UUIDs pass). */
export function isSafeKey(key: string): boolean {
  return /^[A-Za-z0-9_-]{1,80}$/.test(key);
}

function withSlash(uri: string): string {
  return uri.endsWith("/") ? uri : `${uri}/`;
}

/**
 * True when `uri` is a plain file inside one of the app's temporary roots
 * (scanner output, share-extension copies). Only such copies are removed after
 * encryption; anything else (Photos, Files, other apps) is never touched.
 */
export function isOwnedTemporary(uri: string, roots: readonly string[]): boolean {
  const decoded = safeDecode(uri);
  if (!decoded || !decoded.startsWith("file://") || /(^|\/)\.\.?(\/|$)/.test(decoded.slice("file://".length))) {
    return false;
  }
  return roots.some((root) => {
    const decodedRoot = safeDecode(root);
    if (!decodedRoot || !decodedRoot.startsWith("file://")) return false;
    const prefix = withSlash(decodedRoot);
    return decoded.startsWith(prefix) && decoded.length > prefix.length;
  });
}

/** Decode percent-escapes and fold iOS's /private/var alias onto /var. */
function safeDecode(value: string): string | null {
  try {
    return decodeURIComponent(value).replace(/^file:\/\/\/private\/var\//, "file:///var/");
  } catch {
    return null;
  }
}

/** Page files written by react-native-document-scanner-plugin on iOS. */
const IOS_SCANNER_FILE = /^DOCUMENT_SCAN_\d+_\d{8}_\d{6}\.jpg$/;

/**
 * The iOS scanner saves plaintext pages directly in Documents (persistent and
 * backed up by default), so they must be removed once sealed. Only files with
 * the scanner's own name pattern at the top of Documents qualify.
 */
export function isScannerOutput(uri: string, documentRoot: string): boolean {
  const decoded = safeDecode(uri);
  const root = safeDecode(documentRoot);
  if (!decoded || !root || !root.startsWith("file://")) return false;
  const prefix = withSlash(root);
  return decoded.startsWith(prefix) && IOS_SCANNER_FILE.test(decoded.slice(prefix.length));
}
