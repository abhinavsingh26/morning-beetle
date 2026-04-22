import pytest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.beetle.instrument_master import strip_suffixes, load_instruments
from src.beetle.news_fetcher import fetch_all_headlines, _headline_id
from src.beetle.entity_shield import find_ticker, _is_generic, _keyword_boost
from src.beetle.finbert_scorer import score_headline
from src.beetle.sector_heatmap import get_heatmap, _classify


# ── instrument_master tests ──────────────────────────────────────────
class TestInstrumentMaster:

    def test_strip_suffixes_removes_ltd(self):
        assert strip_suffixes("RELIANCE INDUSTRIES LIMITED") == "RELIANCE"

    def test_strip_suffixes_removes_multiple(self):
        assert strip_suffixes("TATA MOTORS LIMITED") == "TATA"

    def test_strip_suffixes_preserves_short(self):
        result = strip_suffixes("HDFC BANK")
        assert "HDFC" in result

    def test_load_instruments_returns_dict(self):
        instruments = load_instruments()
        assert isinstance(instruments, dict)
        assert len(instruments) > 1000

    def test_load_instruments_has_required_keys(self):
        instruments = load_instruments()
        sample = next(iter(instruments.values()))
        assert "symbol" in sample
        assert "name" in sample
        assert "search_anchor" in sample
        assert "instrument_token" in sample


# ── news_fetcher tests ───────────────────────────────────────────────
class TestNewsFetcher:

    def test_headline_id_is_deterministic(self):
        h = "HDFC Bank Q3 beats estimates"
        assert _headline_id(h) == _headline_id(h)

    def test_headline_id_differs_for_different_headlines(self):
        assert _headline_id("headline one") != _headline_id("headline two")

    def test_fetch_returns_list(self):
        headlines = fetch_all_headlines(max_per_source=5)
        assert isinstance(headlines, list)

    def test_fetch_no_duplicates(self):
        headlines = fetch_all_headlines(max_per_source=10)
        ids = [h["id"] for h in headlines]
        assert len(ids) == len(set(ids))

    def test_headlines_have_required_fields(self):
        headlines = fetch_all_headlines(max_per_source=3)
        if headlines:
            h = headlines[0]
            assert "title" in h
            assert "source" in h
            assert "id" in h


# ── entity_shield tests ──────────────────────────────────────────────
class TestEntityShield:

    @pytest.fixture
    def instruments(self):
        return load_instruments()

    def test_known_alias_hdfc(self, instruments):
        match = find_ticker("HDFC Bank Q3 beats estimates NII up 15%", instruments)
        assert match is not None
        assert match["symbol"] == "HDFCBANK"

    def test_known_alias_nestle(self, instruments):
        match = find_ticker("Nestle India Q4 results revenue jumps", instruments)
        assert match is not None
        assert match["symbol"] == "NESTLEIND"

    def test_known_alias_infosys(self, instruments):
        match = find_ticker("Infosys raises revenue guidance after strong Q3", instruments)
        assert match is not None
        assert match["symbol"] == "INFY"

    def test_known_alias_reliance(self, instruments):
        match = find_ticker("Reliance Industries Q4 results net profit rises", instruments)
        assert match is not None
        assert match["symbol"] == "RELIANCE"

    def test_generic_rbi_returns_none(self, instruments):
        match = find_ticker("RBI maintains hawkish stance on inflation", instruments)
        assert match is None

    def test_generic_market_returns_none(self, instruments):
        match = find_ticker("Markets rally on global cues Sensex up 500 points", instruments)
        assert match is None

    def test_keyword_boost_applied(self):
        boost = _keyword_boost("HDFC Bank declares dividend of Rs 10")
        assert boost == 0.2

    def test_no_boost_for_neutral_headline(self):
        boost = _keyword_boost("Company opens new office in Mumbai")
        assert boost == 0.0

    def test_is_generic_rbi(self):
        assert _is_generic("RBI policy meeting today") is True

    def test_is_generic_false_for_stock(self):
        assert _is_generic("HDFC Bank beats Q3 estimates") is False


# ── finbert_scorer tests ─────────────────────────────────────────────
class TestFinBERTScorer:

    def test_bullish_headline(self):
        result = score_headline("HDFC Bank Q3 beats estimates NII up 15%")
        assert result["label"] == "BULLISH"
        assert result["score"] > 0.15

    def test_bearish_headline(self):
        result = score_headline("Vedanta share price slips ahead of results")
        assert result["label"] in ["BEARISH", "NEUTRAL"]

    def test_score_in_valid_range(self):
        result = score_headline("Infosys raises guidance after strong quarter")
        assert -1.0 <= result["score"] <= 1.0

    def test_score_has_required_keys(self):
        result = score_headline("Test headline")
        assert "score" in result
        assert "label" in result
        assert "raw" in result

    def test_label_is_valid(self):
        result = score_headline("Markets close flat")
        assert result["label"] in ["BULLISH", "BEARISH", "NEUTRAL"]


# ── sector_heatmap tests ─────────────────────────────────────────────
class TestSectorHeatmap:

    def test_classify_bullish(self):
        assert _classify(0.5) == "BULLISH"

    def test_classify_bearish(self):
        assert _classify(-0.5) == "BEARISH"

    def test_classify_neutral_positive(self):
        assert _classify(0.1) == "NEUTRAL"

    def test_classify_neutral_negative(self):
        assert _classify(-0.1) == "NEUTRAL"

    def test_classify_boundary_bullish(self):
        assert _classify(0.31) == "BULLISH"

    def test_classify_boundary_bearish(self):
        assert _classify(-0.31) == "BEARISH"

    def test_heatmap_returns_12_sectors(self):
        heatmap = get_heatmap(use_mock_if_closed=True)
        assert len(heatmap) == 12

    def test_heatmap_has_required_keys(self):
        heatmap = get_heatmap(use_mock_if_closed=True)
        for sector, data in heatmap.items():
            assert "change_pct" in data
            assert "bias" in data

    def test_heatmap_bias_values_valid(self):
        heatmap = get_heatmap(use_mock_if_closed=True)
        valid = {"BULLISH", "BEARISH", "NEUTRAL"}
        for sector, data in heatmap.items():
            assert data["bias"] in valid