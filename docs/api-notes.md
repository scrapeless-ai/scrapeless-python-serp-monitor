# API contract checked on October 26, 2026

This implementation was checked against these official pages before coding:

- [Google Search quickstart](https://docs.scrapeless.com/en/docs/scraping-api/google-search/quickstart/?utm_source=github&utm_medium=referral&utm_campaign=scrapeless-python-serp-monitor&utm_content=repository-readme)
- [Google Search endpoints](https://docs.scrapeless.com/en/docs/scraping-api/google-search/endpoints/?utm_source=github&utm_medium=referral&utm_campaign=scrapeless-python-serp-monitor&utm_content=repository-readme)
- [API parameters](https://apidocs.scrapeless.com/doc-800321?utm_source=github&utm_medium=referral&utm_campaign=scrapeless-python-serp-monitor&utm_content=repository-readme)
- [Results and polling](https://docs.scrapeless.com/en/docs/scraping-api/results-and-polling/?utm_source=github&utm_medium=referral&utm_campaign=scrapeless-python-serp-monitor&utm_content=repository-readme)
- [Account information](https://apidocs.scrapeless.com/api-11949851?utm_source=github&utm_medium=referral&utm_campaign=scrapeless-python-serp-monitor&utm_content=repository-readme)

## Implemented contract

| Item | Implementation |
| --- | --- |
| Create search | `POST https://api.scrapeless.com/api/v1/scraper/request` |
| Authentication | `x-api-token` header |
| Actor | `scraper.google.search` |
| Web search | Omit `tbm` |
| Coverage | `start: 0`; no pagination; do not set `num` |
| Request settings | `q`, `gl`, `hl`, `google_domain`, `device: desktop`, optional `location` |
| Completed response | HTTP 200, top-level `organic_results` |
| Pending response | HTTP 201, `taskId` |
| Read existing task | `GET /api/v1/scraper/result/{taskId}` with the same authentication |
| Observed position | `organic_results[].position`; lowest matching domain position |
| Destination URL | `organic_results[].link` |

The endpoint page links to the parameter reference; there is no assumption that a separate `/google-search/parameters/` route exists. The shared polling guide is under `/scraping-api/results-and-polling/`.

The parameter reference warns that requesting `num` may affect latency and specialized result sections. First-page organic counts varied from 6 to 9 in the six retained live searches. A requested count or page size must not be interpreted as guaranteed coverage.

## Validation boundaries

- The six live responses completed synchronously with HTTP 200. The asynchronous path is covered by deterministic HTTP-response tests, including pending timeout and resume, not by a live HTTP 201 observation.
- Search settings were fixed in requests. Returned rankings can still vary because Google results and service behavior change.
- An API business error or unusable schema is an error snapshot. It is never a successful missing-domain snapshot.
- The API may return sections besides organic results. This project deliberately ignores their contents for ranking calculations.
- The live database retains response JSON. Public sample files contain selected ranking fields and explicitly identify that selection.
- Credentials are supplied locally at execution time. They are not written into the project, public examples, or logs.

## Documentation readback on October 28, 2026

The official quickstart and polling guide were read again for this documentation revision. They continue to document the create endpoint, token header, HTTP 200/201 distinction and existing-task result endpoint used here. This public documentation readback is not an additional live API validation.

## October 29 contract readback

The official quickstart and polling guide were checked again. The request endpoint, actor, header and HTTP 200/201 task contract remain the basis of the revised implementation. This is a public-documentation check, not a live request by the fixed code. Empty arrays are quarantined locally; request intervals are client-side ordering bounds, not provider collection timestamps.

## Current Google redirect handling

Recognized relative /goto and /url links are normalized before storage. Plain-text targets are decoded locally; opaque links use bounded Google Location lookups without API credentials or destination-site visits. Unknown or failed links remain errors. Original provider links and the normalized-link provenance stay in the private SQLite records. The current successful three-query evidence is documented in validation.md.
