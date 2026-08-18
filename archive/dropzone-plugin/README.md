# archive: dropzone dashboard plugin (retired 2026-08-18)

Archived byte-for-byte from the Render disk (`/opt/data/plugins/dropzone`)
before removal. Never wired into the image build — this directory exists so
the work is preserved (CODE_SHAPE rule 7: archive, never delete), not so it
can be re-enabled.

## What it was

An agent-authored dashboard plugin ("Dropzone", Howe Agency, MIT) that added
drag/drop + paste file upload to the Chat tab via the `chat:top` slot, with
its own upload backend at `POST /api/plugins/dropzone/upload`
([plugin_api.py](dashboard/plugin_api.py)).

## Why it was retired

1. **Its auth predates the OIDC gate.** `plugin_api.py` re-implements the
   legacy loopback token check (`X-Hermes-Session-Token` /
   `web_server._SESSION_TOKEN`) as a router-level dependency. Under Hermes
   v0.20.0's gated OIDC mode that token is never injected into the SPA, so
   the check rejects **every** browser request — including fully
   authenticated ones — with `401 {"detail":"Unauthorized"}`. This was the
   red `Unauthorized` under the attach bar. (Verified live: with a valid
   session cookie, `/api/dashboard/plugins/hub` → 200 while
   `/api/plugins/dropzone/upload` → 401.)
2. **Its final form hid the working control.** The last deployed bundle
   ([dist/index.js](dashboard/dist/index.js)) is an "AttachmentUiSuppressor"
   that `display:none`s the native `Attach files` paperclip via a
   MutationObserver, on the (stale) belief that both attach surfaces were
   broken. The native paperclip (PR #3, `scripts/patch-chat-attachments.py`)
   was verified working: `POST /api/chat/image-upload` → 200.

## Replacement

The native paperclip from PR #3 is the single attach path. `chat:top` should
stay empty — do not reintroduce an attach plugin there.
