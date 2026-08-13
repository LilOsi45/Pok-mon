"""Keyword pinger: which Discord messages raise an alert.

The rules follow the pinger services the operator already uses, so an existing
keyword set can be pasted over: ";" separates alternatives, "#123" restricts
the source channel, negatives veto, ranges constrain.
"""

from __future__ import annotations

import pytest

from app.keyword_match import matches, parse_rules, prices_in

MSG = "Pokémon 30 Jahre Top-Trainer-Box Display jetzt für 59,99 € verfügbar"


def rules(positive: str, negative: str = "", ranges: str = ""):
    return parse_rules(positive, negative, ranges)


class TestPositives:
    def test_any_alternative_is_enough(self):
        assert matches(rules("charizard;top-trainer-box"), MSG)

    def test_no_alternative_no_alert(self):
        assert not matches(rules("charizard;mewtwo"), MSG)

    def test_plus_requires_both(self):
        assert matches(rules("pokemon+display"), MSG)
        assert not matches(rules("pokemon+booster"), MSG)

    def test_case_does_not_matter(self):
        assert matches(rules("TOP-TRAINER-BOX"), MSG)

    def test_an_empty_alert_never_fires(self):
        """Otherwise it would forward every message in the channel."""
        assert not matches(rules(""), MSG)


class TestNegatives:
    def test_one_unwanted_word_beats_the_positives(self):
        assert not matches(rules("display", negative="sold out"), MSG + " — sold out")

    def test_unrelated_negatives_do_not_interfere(self):
        assert matches(rules("display", negative="hoodie;shirt"), MSG)


class TestChannels:
    def test_only_the_listed_channels_count(self):
        r = rules("#111;#222;display")
        assert matches(r, MSG, channel_id="222")
        assert not matches(r, MSG, channel_id="999")

    def test_without_channel_entries_every_channel_counts(self):
        assert matches(rules("display"), MSG, channel_id="999")


class TestPrices:
    def test_min_rejects_cheaper(self):
        assert not matches(rules("display", ranges="min100€"), MSG)

    def test_min_accepts_dearer(self):
        assert matches(rules("display", ranges="min50€"), MSG)

    def test_max_rejects_dearer(self):
        assert not matches(rules("display", ranges="max50€"), MSG)

    def test_a_range_takes_both_ends(self):
        assert matches(rules("display", ranges="50-70€"), MSG)
        assert not matches(rules("display", ranges="80-100€"), MSG)

    def test_a_message_without_a_price_still_fires(self):
        """Drops that post a picture and a link are the ones worth having, so a
        price rule only ever rejects a price it actually saw."""
        assert matches(rules("display", ranges="min100€"), "Pokémon Display live!")

    def test_german_and_english_decimals(self):
        assert prices_in("1.299,00 € und €49.99") == [1299.0, 49.99]

    def test_a_bare_number_is_not_a_price(self):
        """Otherwise a product code or a size decides the price rule."""
        assert prices_in("Artikel 4599 Größe 44") == []


class TestSizes:
    def test_a_size_token_must_appear(self):
        assert matches(rules("display", ranges="us9;uk8.5"), MSG + " US9")
        assert not matches(rules("display", ranges="us9"), MSG)


class TestParsing:
    def test_channels_are_split_off_from_keywords(self):
        r = rules("#123;#456;pokemon")
        assert r.channels == ("123", "456")
        assert r.positives == (("pokemon",),)

    def test_ranges_and_sizes_are_told_apart(self):
        r = rules("x", ranges="min50€;max200€;42-45;US9")
        assert (r.price_min, r.price_max) == (50.0, 200.0)
        assert r.sizes == ("42-45", "us9")

    @pytest.mark.parametrize("raw", ["", "   ", ";;;"])
    def test_blank_input_is_harmless(self, raw: str):
        r = rules(raw, raw, raw)
        assert r.positives == () and r.negatives == () and r.sizes == ()


class TestAccents:
    """Shops write "Pokémon", people type "pokemon". A keyword that misses on
    an accent is a missed drop with no visible cause."""

    def test_a_keyword_without_the_accent_still_hits(self):
        assert matches(rules("pokemon"), "Pokémon Display")

    def test_a_keyword_with_the_accent_hits_plain_text(self):
        assert matches(rules("pokémon"), "POKEMON Display")

    def test_negatives_fold_the_same_way(self):
        assert not matches(rules("display", negative="grösse"), "Display in Größe 44")
