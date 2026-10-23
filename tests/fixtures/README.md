Steam reviews fixtures captured anonymously from the official GetAppReviews/v1
endpoint on 2026-10-23, using the filters in steam_reviews.py.

- steam_reviews_portal2.json: AppID 620; only response.query_summary is retained.
  Review text, author identifiers and pagination data are deliberately omitted.
- steam_reviews_unavailable.json: empty response body for AppID 228980. HTTP 200
  alone is insufficient to establish a valid summary or zero review count.

Zero-count summaries and malformed/error responses in the tests are synthetic
variants. They are not claimed to be live captures.
