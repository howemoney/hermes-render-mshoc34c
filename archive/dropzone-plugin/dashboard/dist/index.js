/**
 * Dropzone UI suppression.
 *
 * The dashboard currently exposes two non-working attachment surfaces:
 * the legacy dropzone card in `chat:top` and the built-in attachment button
 * over the lower-left corner of the terminal. Keep this enabled, slot-only
 * plugin as a durable UI override until dashboard attachments are reliable.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  var React = SDK.React;
  var useEffect = SDK.hooks.useEffect;

  function hideBuiltInAttachmentButton() {
    var button = document.querySelector('button[aria-label="Attach files"]');
    if (!button) return null;
    button.style.setProperty("display", "none", "important");
    button.setAttribute("data-dropzone-hidden", "true");
    return button;
  }

  function AttachmentUiSuppressor() {
    useEffect(function () {
      hideBuiltInAttachmentButton();

      var observer = new MutationObserver(function () {
        hideBuiltInAttachmentButton();
      });
      observer.observe(document.body, { childList: true, subtree: true });

      return function () {
        observer.disconnect();
        var button = document.querySelector('button[data-dropzone-hidden="true"]');
        if (button) {
          button.style.removeProperty("display");
          button.removeAttribute("data-dropzone-hidden");
        }
      };
    }, []);

    // Returning no DOM removes the old top-of-chat attachment card.
    return null;
  }

  window.__HERMES_PLUGINS__.registerSlot("dropzone", "chat:top", AttachmentUiSuppressor);
})();
