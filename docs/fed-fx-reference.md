# Board H.10 CNY reference conversion

`fed_fx_reference.normalize_h10(html)` validates and normalizes the official
2000-onward Chinese renminbi historical table. It performs no network requests
or file writes. Its caller archives acquisition, source policy and raw/converted
hashes privately. Numeric precision is preserved with Decimal; `ND` remains an
explicit missing observation. Duplicate, backward, future, nonpositive,
nonfinite and ambiguous-table inputs fail validation.

The exact source unit is Chinese yuan per US dollar. It must not be inverted
implicitly. The observations are New York noon buying references for cable
transfers. They are not an individual's executable retail quote, conversion
spread, commission or legal access to exchange currency.

This is a latest-vintage snapshot. H.10 can be corrected after publication;
today's historical table cannot establish what was known on a historical day.
`historical_known_at` stays null and the original S02 input-ready flag stays
false. Observation date must not be promoted to a publication timestamp. The
converter supplies an independently reviewed reference input, not a completed
four-ETF historical study, funding path or cash benchmark.

The [Board reuse policy](https://www.federalreserve.gov/disclaimer.htm) permits
copying and distribution of its own public-domain material with attribution,
unless a third-party notice indicates otherwise. Review the acquired source
for such notices; do not extend this policy to other providers. Archive the
policy with the actual acquired [CNY history](https://www.federalreserve.gov/releases/h10/hist/dat00_ch.htm).
Raw source observations remain outside this public repository.
