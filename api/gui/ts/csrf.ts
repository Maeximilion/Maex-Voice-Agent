// The CSRF token every write of the operations view sends as X-CSRF-Token
// (docs/06 section 9). Pure, so node can test it (api/tests/csrf_cookie.mjs).
//
// Source: api/gui/ts/csrf.ts. The file of the same name in api/gui/static/ is its
// compiled output (`npm run build`, docs/22); edit the source, never the output.

// The cookie's exact names (api/gui/session.py cookie_name): `__Host-csrf`
// outside dev and test, `maex_csrf` there. A `__Host-` cookie can only be set by
// this host, Secure, for path / and without a Domain, so a cookie a sibling host
// sets for the whole domain can never stand in for it (#888 S2). It is looked
// for first: where it exists, a planted `maex_csrf` is never read.
const NAMES = ["__Host-csrf", "maex_csrf"] as const;

// `cookies` is `document.cookie`: "name=value" pairs joined by "; ".
export function csrfToken(cookies: string): string {
  const pairs = cookies.split("; ");
  for (const name of NAMES) {
    const pair = pairs.find((p) => p.startsWith(`${name}=`));
    if (pair !== undefined) return pair.slice(name.length + 1);
  }
  return "";
}
