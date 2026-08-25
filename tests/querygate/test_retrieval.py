"""BM25 retrieval, tokenizer, and glossary expansion."""

from __future__ import annotations

from querygate.retrieval.index import CatalogIndex, tokenize


class TestTokenizer:
    def test_splits_snake_and_camel(self):
        assert tokenize("monthly_revenue") == ["monthly", "revenue"]
        assert tokenize("MonthlyRevenue") == ["monthly", "revenue"]


class TestSearch:
    def test_finds_by_exact_name(self, catalog):
        idx = CatalogIndex.build(catalog)
        assert "plan_catalog" in [m.name for m in idx.search("plan catalog", 3)]

    def test_finds_by_glossary_synonym(self, catalog):
        # "money" is a glossary synonym of "revenue" → should reach the revenue model.
        idx = CatalogIndex.build(catalog)
        names = [m.name for m in idx.search("how much money did we make", 3)]
        assert "monthly_revenue" in names

    def test_churn_reaches_active_customers(self, catalog):
        idx = CatalogIndex.build(catalog)
        assert "active_customers" in [m.name for m in idx.search("which customers are churning", 3)]

    def test_empty_query_returns_nothing(self, catalog):
        idx = CatalogIndex.build(catalog)
        assert idx.search("", 3) == []
