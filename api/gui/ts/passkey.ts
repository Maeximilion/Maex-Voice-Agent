// Passkey ceremonies of the operator dashboard (docs/23 section 5): registering a
// passkey, signing in with one, and the step-up before a dangerous action.
//
// Source: api/gui/ts/passkey.ts. The file of the same name in api/gui/static/ is its
// compiled output (`npm run build`, docs/22); edit the source, never the output.
//
// A button `data-passkey="create"` or `data-passkey="get"` inside a form starts
// one: the page posts the form's CSRF token to the address in
// `data-passkey-options`, the server answers with a challenge, the device signs
// it, and the answer goes into the form's hidden field `passkey`. The form is
// then posted like any other. Nothing here decides anything: the server checks
// the answer (api/dashboard/passkeys.py).
//
// shortcut: the browser's own JSON helpers (parseCreationOptionsFromJSON,
// parseRequestOptionsFromJSON, toJSON; Chrome 129, Firefox 119, Safari 18.4) do
// the base64url work. A browser without them keeps the buttons hidden and the
// operator signs in with a code; write the conversion by hand when such a
// browser must use a passkey.

// Marks the file as a module, so its helpers stay out of the page's global scope.
export {};

function supported(): boolean {
  return (
    typeof PublicKeyCredential !== "undefined" &&
    typeof PublicKeyCredential.parseCreationOptionsFromJSON === "function" &&
    typeof PublicKeyCredential.parseRequestOptionsFromJSON === "function"
  );
}

// The options come from our own server as JSON; the browser's parser refuses
// anything that is not a set of WebAuthn options.
async function ceremony(kind: string, options: unknown): Promise<string | null> {
  const credential =
    kind === "create"
      ? await navigator.credentials.create({
          publicKey: PublicKeyCredential.parseCreationOptionsFromJSON(
            options as PublicKeyCredentialCreationOptionsJSON,
          ),
        })
      : await navigator.credentials.get({
          publicKey: PublicKeyCredential.parseRequestOptionsFromJSON(
            options as PublicKeyCredentialRequestOptionsJSON,
          ),
        });
  if (!(credential instanceof PublicKeyCredential)) return null;
  return JSON.stringify(credential.toJSON());
}

async function run(button: HTMLButtonElement): Promise<void> {
  const form = button.form;
  if (!form) return;
  const answer = form.elements.namedItem("passkey");
  const token = form.elements.namedItem("csrf_token");
  if (!(answer instanceof HTMLInputElement) || !(token instanceof HTMLInputElement)) return;
  // Name and password first: the sign-in sends them with the passkey's answer.
  if (!form.reportValidity()) return;
  const message = form.querySelector("[data-passkey-message]");
  if (message instanceof HTMLElement) message.hidden = true;
  button.disabled = true;
  try {
    const response = await fetch(button.dataset["passkeyOptions"] ?? "", {
      method: "POST",
      body: new URLSearchParams({ csrf_token: token.value, back: location.pathname }),
      credentials: "same-origin",
    });
    // The session needs a fresh second factor: the server sent us to the page
    // that deals with it.
    if (response.redirected) {
      location.assign(response.url);
      return;
    }
    // The session ended: a post behind the sign-in gets 401, not a redirect.
    if (response.status === 401) {
      location.assign("/login");
      return;
    }
    if (!response.ok) throw new Error(`options refused: ${response.status}`);
    const value = await ceremony(button.dataset["passkey"] ?? "", await response.json());
    if (value === null) throw new Error("no credential");
    answer.value = value;
    form.submit();
  } catch {
    // Cancelled, timed out, no such passkey on the device: the browser tells
    // them apart on purpose as little as it can, and so do we.
    if (message instanceof HTMLElement) message.hidden = false;
    button.disabled = false;
  }
}

if (supported()) {
  for (const button of document.querySelectorAll("button[data-passkey]")) {
    if (button instanceof HTMLButtonElement) button.hidden = false;
  }
  document.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    const button = event.target.closest("button[data-passkey]");
    if (button instanceof HTMLButtonElement) void run(button);
  });
}
