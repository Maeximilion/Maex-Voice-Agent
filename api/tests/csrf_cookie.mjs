// Checks which cookie the operations view sends as X-CSRF-Token (#888 S2)
// without a browser.
// Usage: node api/tests/csrf_cookie.mjs api/gui/static/csrf.js
// (test_gui_csrf_cookie.py runs it). Prints "ok" or throws.
import assert from 'node:assert'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const { csrfToken } = await import(pathToFileURL(path.resolve(process.argv[2])).href)

// Production: the `__Host-` cookie, also when a sibling host's cookies of the old
// or the dev name come first, as a cookie set with a Domain does.
assert.equal(csrfToken('__Host-csrf=own'), 'own')
assert.equal(csrfToken('csrf_token=planted; __Host-csrf=own'), 'own')
assert.equal(csrfToken('maex_csrf=planted; csrf_token=planted; __Host-csrf=own'), 'own')
assert.equal(csrfToken('__Host-csrf=own; maex_csrf=planted'), 'own')
// Dev and test: the plain name.
assert.equal(csrfToken('maex_csrf=dev'), 'dev')
assert.equal(csrfToken('csrf_token=old; maex_csrf=dev'), 'dev')
// Exact names only: neither the old name nor a name that merely contains one.
assert.equal(csrfToken('csrf_token=old'), '')
assert.equal(csrfToken('x__Host-csrf=a; __Host-csrfx=b; xmaex_csrf=c; maex_csrf2=d'), '')
assert.equal(csrfToken(''), '')
// The value is taken whole, an "=" in it included.
assert.equal(csrfToken('__Host-csrf=a=b'), 'a=b')
assert.equal(csrfToken('__Host-csrf='), '')

console.log('ok')
