/**
 * Every sentence the owner reads, in one place (§36, §48, §69-70).
 * Short, calm, precise. No accounting jargon, no raw errors, no ids, never
 * "Great job!". Success is quiet.
 */
import type { FailureKind, QualityIssue } from "./offline/types";

export function countWord(n: number): string {
  const words = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"];
  return words[n] ?? String(n);
}

function plural(n: number, one: string, many: string): string {
  return n === 1 ? one : many;
}

export const copy = {
  done: "Done.",
  doneRemember: "Done. I will remember this.",
  offlineNote: "You're offline. Showing what I knew at",
  sampleNote: "Example data. Connect the app to your account to see your business.",
  unreachableSample: "I can't reach your business right now. These are example figures.",
  couldNotSend: "I couldn't send that. Check your connection and try again.",

  status: {
    allGood: "Everything is under control.",
    actionRequired: "Action required.",
    needs(n: number): string {
      return n === 1 ? "I still need one thing." : `I need ${countWord(n)} things from you.`;
    },
  },

  tiles: {
    needsYou: "Needs you",
    handledToday: "Handled today",
  },

  needs: {
    title: "Needs you",
    empty: "Nothing needs you. I'll let you know.",
    why: "Why am I seeing this?",
    rememberFallback: "Remember this next time",
    confirmIdentity: "Confirm it's you",
  },

  activity: {
    title: "Activity",
    empty: "Nothing yet today. I'm keeping an eye on things.",
  },

  ask: {
    title: "Ask",
    placeholder: "Ask your business anything",
    send: "Ask",
    thinking: "Looking…",
    noEvidence: "I couldn't point to a document for this.",
    offline: "I can't check right now. Try again when you're online.",
    examples: [
      "Did we pay Vodafone?",
      "Is September complete?",
      "What still needs my attention?",
      "Show subscriptions that increased.",
    ],
  },

  scan: {
    title: "Scan",
    action: "Scan a document",
    hint: "Point at the paper. I'll find the edges, straighten it and send it.",
    noTyping: "No need to type anything. I'll read it.",
    unavailable: "I can't open the scanner. Check that Back Office can use the camera in Settings.",
    saved: "Got it. I'll take it from here.",
    savedOffline: "Saved on this phone. I'll send it when you're online.",
    alreadyHave: "I already have this one.",
    tooLarge: "This file is too large to send from the phone.",
    failed: "I couldn't save that scan. Please try again.",
    useAnyway: "Use as is",
    cancel: "Don't send",
    retake(page: number): string {
      return `Retake page ${page}`;
    },
    issue(issue: QualityIssue, page: number, pageCount: number): string {
      const where = pageCount > 1 ? `Page ${page}` : "The page";
      if (issue === "blurry") return `${where} looks blurry.`;
      if (issue === "glare") return pageCount > 1 ? `There's glare on page ${page}.` : "There's glare on the page.";
      return `${where} is too dark.`;
    },
    waiting(n: number): string {
      return `${n} ${plural(n, "document", "documents")} waiting to send.`;
    },
    sentToday(n: number): string {
      return `${n} sent today.`;
    },
  },

  held: {
    summary(n: number): string {
      return `I couldn't send ${countWord(n)} ${plural(n, "document", "documents")}. ${plural(n, "It's", "They're")} still on this phone.`;
    },
    reason(kind: FailureKind): string {
      switch (kind) {
        case "rejected":
          return "The server didn't accept this file.";
        case "hash_mismatch":
          return "I couldn't confirm it arrived intact.";
        case "unreadable":
          return "This copy on the phone is damaged. Please scan it again.";
        default:
          return "It hasn't gone through yet.";
      }
    },
    retry: "Try again",
    remove: "Remove from phone",
    removeConfirm: "It was never sent. Remove it from this phone?",
    cancel: "Keep it",
  },

  share: {
    received(n: number): string {
      return n === 1 ? "Got it. I'll take it from here." : `Got ${countWord(n)} items. I'll take it from here.`;
    },
    unsupported: "I can't use this kind of file. Share a photo, PDF, email or link instead.",
    tooLarge: "This file is too large to send from the phone.",
    nothing: "There was nothing I could use in that share.",
  },

  auth: {
    title: "Sign in",
    lead: "Welcome back. I kept going while you were away.",
    email: "Email",
    password: "Password",
    show: "Show",
    hide: "Hide",
    submit: "Sign in",
    expired: "You were signed out. Sign in again to continue.",
    signedOut: "You're signed out.",
    newHere: "New here?",
    createAccount: "Create an account",
    createAccountHint: "Opens the sign-up page in your browser.",
    forgot: "Forgot password?",
    forgotBody(support: string): string {
      return support ? `Ask support at ${support} to reset it.` : "Ask support to reset it.";
    },
  },

  push: {
    title: "Turn on notifications?",
    body: "I only notify you when I need a decision, a connection stops working, or a month is closed.",
    enable: "Turn on",
    later: "Not now",
    denied: "Notifications are off. You can turn them on in the phone's Settings.",
  },

  account: {
    title: "Account",
    signedInAs: "Signed in as",
    notifications: "Notifications",
    notificationsOn: "On. Only what needs you.",
    notificationsOff: "Off",
    turnOn: "Turn on",
    openSettings: "Open Settings",
    signOut: "Sign out",
    signOutConfirm: "Sign out of Back Office on this phone?",
    signOutWaiting(n: number): string {
      if (n === 1) return "One document hasn't been sent yet. It stays on this phone and goes when you sign in again.";
      const word = countWord(n);
      return `${word.charAt(0).toUpperCase()}${word.slice(1)} documents haven't been sent yet. They stay on this phone and go when you sign in again.`;
    },
    cancel: "Cancel",
    back: "Back",
    demo: "This is the demo. Nothing leaves this phone.",
  },

  connections: {
    title: "Connections",
    healthy: "Connected",
    stale: "Needs reconnecting",
    reconnectHint: "Reconnecting opens the page where you signed in to it, on the web.",
    reconnect: "Reconnect on the web",
    empty: "Nothing is connected yet. Connect your email and bank on the web.",
  },

  lock: {
    title: "Back Office is locked",
    body: "Unlock to see your business.",
    unlock: "Unlock",
    prompt: "Unlock Back Office",
    lockout: "Too many attempts. Unlock your phone first, then try again.",
    failed: "That didn't work. Try again.",
    noPasscode: "Set a passcode on this phone to keep your business private.",
    continue: "Continue",
  },
} as const;
