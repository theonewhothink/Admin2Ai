# iOS Share Extension (§12)

The Share Extension is generated at `expo prebuild` by the
[`expo-share-intent`](https://github.com/achorein/expo-share-intent) config
plugin (v8, Expo SDK 57). Nothing in this folder is compiled into the app. It
records what the plugin covers, the one gap we know of, and how to close it
without hand-editing a generated project.

## What the plugin generates

Configured in `mobile/app.json` under `plugins → expo-share-intent`:

| Piece | Value |
| --- | --- |
| Extension target | `ShareExtension` (display name "Back Office") |
| App Group | `group.<ios.bundleIdentifier>`, used to hand files to the app |
| Activation rule | `iosActivationRules` SUBQUERY accepting `public.url`, `public.plain-text`, `public.image`, `com.adobe.pdf`, `public.email-message`, `public.xml` |
| Hand-off | The extension copies the shared item into the App Group and opens `backoffice://dataUrl=backofficeShareKey…`; `app/+native-intent.tsx` keeps that link away from the router and `src/app/ShareHandler.tsx` reads the payload |

The extension never uploads anything itself. The main app seals each item into
the encrypted offline queue (§43) and deletes the App Group copy once sealed.

Apple Developer portal: register the App Group for both the app and the
extension identifiers. With EAS Build, keep a single extension target (see the
plugin's FAQ, "iOS Extension Target").

## Known gap: email messages shared as data

The generated `ShareExtensionViewController.swift` dispatches attachments by
type: image, movie, vCard, file URL, PassKit pass, PDF, property list, URL,
text. An attachment that conforms to `public.email-message` but is **not** a
file URL (some mail apps share the message itself, not an `.eml` file) matches
none of these branches, and the extension shows an error.

`.eml` files shared from Files, or saved from Mail first, arrive as file URLs
and work today.

XML e-invoices shared as data (not as files) conform to `public.text`, so they
arrive as text. `src/share/route.ts` then queues them as `text` evidence; the
server should sniff XML content in text evidence (§13 structured data first).

### How to close the gap (when needed)

Patch the generated controller with `patch-package` (or a small local config
plugin using `withDangerousMod`) so the change survives `expo prebuild --clean`.
Add, before the final `else`, a branch that writes the data to the App Group as
an `.eml` file and reuses the plugin's file hand-off. Sketch (not compiled, not
wired in this repository):

```swift
} else if attachment.hasItemConformingToTypeIdentifier(UTType.emailMessage.identifier) {
  // Load the raw RFC 822 bytes and store them as <uuid>.eml in the App Group
  // container, then append the same dictionary the plugin builds for files:
  // path, fileName, mimeType "message/rfc822", size.
}
```

Validate on a device: Share → Back Office from Mail, Gmail and Outlook, then
confirm the item appears as `format = eml` in the upload (`hints` unaffected).
