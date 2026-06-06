# Vendored frontend dependencies

These files are checked into the repo so the app has no runtime CDN dependency
and works fully offline. Refresh occasionally; verify against upstream changelogs.

| File | Library | Version | Source |
|---|---|---|---|
| `purify.min.js` | DOMPurify | 3.2.4 | https://cdn.jsdelivr.net/npm/dompurify@3.2.4/dist/purify.min.js |
| `marked.min.js` | marked | 15.0.7 | https://cdn.jsdelivr.net/npm/marked@15.0.7/marked.min.js |
| `hljs.min.js` | highlight.js | 11.10.0 | https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.10.0/build/highlight.min.js |
| `hljs-github-dark.min.css` | highlight.js theme | 11.10.0 | https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.10.0/build/styles/github-dark.min.css |

## Refresh procedure

```bash
cd backend/app/static/vendor
curl -fsSL -o purify.min.js  https://cdn.jsdelivr.net/npm/dompurify@<VER>/dist/purify.min.js
curl -fsSL -o marked.min.js  https://cdn.jsdelivr.net/npm/marked@<VER>/marked.min.js
curl -fsSL -o hljs.min.js    https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@<VER>/build/highlight.min.js
curl -fsSL -o hljs-github-dark.min.css \
    https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@<VER>/build/styles/github-dark.min.css
```

Then update the table above and commit together.
