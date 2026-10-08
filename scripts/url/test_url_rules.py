#!/usr/bin/env python3
"""Plain-assert checks for url_rules.classify(). Run: python3 scripts/url/test_url_rules.py"""
from url_rules import classify, doi_suffix_intact

CASES = [
    # (identifier, type, url)
    ("10.1000/ABC.def", "doi", "https://doi.org/10.1000/ABC.def"),
    ("  doi: 10.1000/xyz  ", "doi", "https://doi.org/10.1000/xyz"),
    ("DOI:10.5194/Soil-7-1", "doi", "https://doi.org/10.5194/Soil-7-1"),
    ("http://dx.doi.org/10.1000/AbC", "doi_url", "https://doi.org/10.1000/AbC"),
    ("HTTPS://DOI.ORG/10.1000/AbC.", "doi_url", "https://doi.org/10.1000/AbC"),
    ("10.1002/(SICI)1099-1530(199809)9:5<3::AID-X>2.0.CO;2-L", "doi",
     "https://doi.org/10.1002/(SICI)1099-1530(199809)9:5%3C3::AID-X%3E2.0.CO;2-L"),
    ("10.1000/abc)", "doi", "https://doi.org/10.1000/abc"),          # unbalanced )
    ("10.1000/abc(1)", "doi", "https://doi.org/10.1000/abc(1)"),     # balanced )
    ("10.1000/a#b", "doi", "https://doi.org/10.1000/a%23b"),
    ("HTTP://Example.ORG/Path/X?Q=1,", "url", "http://example.org/Path/X?Q=1"),
    ("https://hdl.handle.net/10013/epic.1", "url", "https://hdl.handle.net/10013/epic.1"),
    ("arXiv:2101.01234v2", "arxiv", "https://arxiv.org/abs/2101.01234v2"),
    ("arxiv:hep-th/9901001", "arxiv", "https://arxiv.org/abs/hep-th/9901001"),
    ("hdl:10013/epic.1", "handle", "https://hdl.handle.net/10013/epic.1"),
    ("urn:nbn:de:0001-2345", "urn", "https://nbn-resolving.org/urn:nbn:de:0001-2345"),
    ("123e4567-e89b-12d3-a456-426614174000", "uuid", None),
    ("50|RECOLECTA___::abc123", "oai_recolecta", None),
    ("ISBN 978-3-16-148410-0", "isbn", None),
    ("ISBN 978-3-16-148410-1", "other", None),                       # bad checksum
    ("ISSN 0378-5955", "issn", None),
    ("9783161484100", "other", None),                                # bare digits
    ("some free text", "other", None),
    ("", "empty", None),
    (None, "empty", None),
]


RAW_CASES = [
    ("6630dd2fb77296f6fbf2437f", {"distribution": {"landingPage": {"url": "HTTPS://EuFarmBook.eu/en/contributions/6630"}}},
     "landing_page", "https://eufarmbook.eu/en/contributions/6630"),
    ("c9680019-eb3e-4a45-b750-9a10ed2e65f0", {"sources": ["bonares"], "links": [
        '{"url":"https%3A%2F%2Fmaps.bonares.de%2Fmapapps%2Findex.html%3Fmid%3Dc968","name":null,"format":"link-0"}']},
     "bonares_landing", "https://maps.bonares.de/mapapps/index.html?mid=c968"),
    # non-bonares links are never used
    ("32623E89-0C8A-4925-B86D-C47B1F7DA333", {"sources": ["inspire"], "links": [
        '{"url":"https%3A%2F%2Fdownload.bgr.de%2Fx.zip","name":"x","format":"link-0"}']}, "uuid", None),
    ("oai:estudogeral.uc.pt:10316/122644", None, "oai_handle", "https://hdl.handle.net/10316/122644"),
    ("oai:ruc.udc.es:2183/1234", {}, "oai_handle", "https://hdl.handle.net/2183/1234"),
    ("oai:arXiv.org:2101.01234", None, "other", None),                 # not a numeric handle
    # identifier rules still win over raw_metadata
    ("10.1000/abc", {"distribution": {"landingPage": {"url": "https://x.org/"}}}, "doi", "https://doi.org/10.1000/abc"),
]


def main() -> None:
    for ident, raw, typ, url in RAW_CASES:
        got = classify(ident, raw)
        assert (got.type, got.url) == (typ, url), f"{ident!r}: got {got}, want {(typ, url)}"
    for ident, typ, url in CASES:
        got = classify(ident)
        assert (got.type, got.url) == (typ, url), f"{ident!r}: got {got}, want {(typ, url)}"
    for ident in ("10.1000/ABC.def", "10.1002/(SICI)<x>", "https://doi.org/10.1000/a b"):
        r = classify(ident)
        if r.url:
            assert doi_suffix_intact(ident, r.url), ident
    print(f"ok: {len(CASES) + len(RAW_CASES)} cases")


if __name__ == "__main__":
    main()
