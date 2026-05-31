import pytest
from app import name_matches, clean_name, parse_shopify, _extract_tcg_price

# ── name_matches ──────────────────────────────────────────────────────────────

def test_name_matches_exact():
    assert name_matches("Stock Up", "Stock Up")

def test_name_matches_case_insensitive():
    assert name_matches("stock up", "Stock Up")

def test_name_matches_substring_in_longer_title():
    assert name_matches("Stock Up [IKO] NM", "Stock Up")

def test_name_matches_word_boundary_no_false_positive():
    # "Stock" should not match "Livestock"
    assert not name_matches("Livestock", "Stock")

def test_name_matches_multiword_all_present():
    assert name_matches("Lightning Bolt [M10]", "Lightning Bolt")

def test_name_matches_multiword_partial_fails():
    assert not name_matches("Lightning Helix", "Lightning Bolt")

def test_name_matches_empty_query_returns_false():
    assert not name_matches("Lightning Bolt", "")

# ── clean_name ────────────────────────────────────────────────────────────────

def test_clean_name_strips_set_bracket():
    assert clean_name("Stock Up [IKO]") == "Stock Up"

def test_clean_name_no_bracket_unchanged():
    assert clean_name("Stock Up") == "Stock Up"

def test_clean_name_strips_surrounding_whitespace():
    assert clean_name("  Stock Up [IKO]  ") == "Stock Up"

# ── _extract_tcg_price ────────────────────────────────────────────────────────

def test_extract_price_lowest_listing():
    assert _extract_tcg_price({"lowestListingPrice": 1.50}) == 1.50

def test_extract_price_market():
    assert _extract_tcg_price({"marketPrice": 2.00}) == 2.00

def test_extract_price_string_dollar_sign():
    assert _extract_tcg_price({"price": "$3.25"}) == 3.25

def test_extract_price_nested_pricing_dict():
    assert _extract_tcg_price({"pricing": {"low": 0.75}}) == 0.75

def test_extract_price_zero_skipped():
    # A zero price should not be returned; fall through to next field
    assert _extract_tcg_price({"lowestListingPrice": 0, "marketPrice": 1.00}) == 1.00

def test_extract_price_no_price_returns_none():
    assert _extract_tcg_price({"name": "Stock Up"}) is None

# ── parse_shopify ─────────────────────────────────────────────────────────────

def _make_product(title, variants):
    return {
        "title": title,
        "handle": title.lower().replace(" ", "-"),
        "variants": variants,
    }

def _variant(price, available=True, title="Default Title"):
    return {"price": price, "available": available, "title": title}

def test_parse_shopify_returns_matching_available():
    products = [_make_product("Stock Up [IKO]", [_variant("1.50")])]
    results = parse_shopify(products, "https://example.com", "Stock Up")
    assert len(results) == 1
    assert results[0]["price"] == 1.50

def test_parse_shopify_skips_unavailable_variant():
    products = [_make_product("Stock Up [IKO]", [_variant("1.50", available=False)])]
    results = parse_shopify(products, "https://example.com", "Stock Up")
    assert results == []

def test_parse_shopify_all_variants_captured():
    variants = [_variant("1.00", title="NM"), _variant("0.75", title="LP")]
    products = [_make_product("Stock Up [IKO]", variants)]
    results = parse_shopify(products, "https://example.com", "Stock Up")
    assert len(results) == 2

def test_parse_shopify_non_matching_title_excluded():
    products = [_make_product("Lightning Bolt [M10]", [_variant("0.50")])]
    results = parse_shopify(products, "https://example.com", "Stock Up")
    assert results == []

def test_parse_shopify_mixed_products():
    products = [
        _make_product("Stock Up [IKO]", [_variant("1.50")]),
        _make_product("Lightning Bolt [M10]", [_variant("0.50")]),
    ]
    results = parse_shopify(products, "https://example.com", "Stock Up")
    assert len(results) == 1
    assert "Stock Up" in results[0]["name"]
