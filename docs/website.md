# Project website

The public project homepage is hosted on GitHub Pages:
<https://aik8s.github.io/cubesandbox-agent-adapter/>.

It is a static, light-theme Chinese/English introduction, not an Adapter console.
It has no backend connection, credentials, analytics, third-party fonts or npm
dependencies. Client images reuse the redacted public evidence in this repository.

The `#trust` section puts permissions, independent approval, output policy,
cleanup, signed receipts and redacted audit ahead of client screenshots. Its
visual plan reuses four full-size light-theme acceptance-report captures from
`docs/assets/trusted-execution-acceptance/` (02–05), plus the historical
`docs/assets/readme/adapter-audit.jpg`. Each image has a bilingual readable
summary and an original-image link for mobile inspection. These are existing
evidence assets, not new test results or a product/security-console mockup.
The historical audit demo is explicitly distinguished from a production audit
service; HMAC verification is not hardware attestation or non-repudiation.

## Preview and publish

```sh
node scripts/build-site.mjs
python3 -m http.server 19120 --bind 127.0.0.1 --directory dist/site
```

Open <http://127.0.0.1:19120/>. Test language switching, all four client screenshot
tabs and five trust/audit tabs (including arrow keys), copy feedback, documentation links, and mobile
layout. The build reads `VERSION`; do not hardcode a new release in the page.

`.github/workflows/pages.yml` builds on relevant pull requests and deploys on
matching pushes to `main` (or manual dispatch). In repository Settings → Pages,
the source must be **GitHub Actions**. Only `dist/site` is uploaded: the five
static site files and nine explicitly allowlisted public screenshots. Use a
clean build directory when previewing after changing the allowlist; CI starts
from a fresh checkout. Never add `.env`, runtime logs or internal connection
details to the site or its build output.

## Optional custom domain

The default HTTPS URL needs no domain purchase. A domain or subdomain you
control can be added later through repository Settings → Pages → Custom domain.
For example, `adapter.example.com` would use a CNAME DNS record pointing to
`aik8s.github.io` (no scheme or project path), not directly to the Adapter.

Verify domain ownership first, configure the Pages custom domain and DNS using
[GitHub's custom-domain guide](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site),
then enable Enforce HTTPS when the certificate is available. Update the README
links after checking the final URL. No custom domain or DNS changes are part of
the initial publication. With an Actions publishing source, the custom domain
is managed in Pages settings; do not rely on a source-tree `CNAME` file.
