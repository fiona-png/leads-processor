"""Search-name builder and role/seniority abbreviation tests."""

from __future__ import annotations

from cole_leads.config import role_abbrev, search_name


class TestRoleAbbrev:
    def test_chief_sales(self):
        assert role_abbrev("Chief", "Sales") == "CRO"

    def test_chief_marketing(self):
        assert role_abbrev("Chief", "Marketing") == "CMO"

    def test_chief_customer_success(self):
        assert role_abbrev("Chief", "Customer Success") == "CCO"

    def test_chief_general_management(self):
        assert role_abbrev("Chief", "General Management") == "COO"

    def test_chief_sales_ops(self):
        assert role_abbrev("Chief", "Sales Ops") == "COO"

    def test_head_marketing(self):
        assert role_abbrev("Head", "Marketing") == "HOM"

    def test_head_sales(self):
        assert role_abbrev("Head", "Sales") == "HOS"

    def test_vp_sales(self):
        assert role_abbrev("VP", "Sales") == "VPS"

    def test_director_marketing(self):
        assert role_abbrev("Director", "Marketing") == "DirectorM"

    def test_svp_sales(self):
        assert role_abbrev("SVP", "Sales") == "SVPS"


class TestSearchName:
    def test_basic(self):
        assert search_name("Acme Robotics", "Chief", "Sales") == "Acme Robotics CRO"

    def test_vp(self):
        assert search_name("Northwind", "VP", "Marketing") == "Northwind VPM"

    def test_head(self):
        assert search_name("Globex", "Head", "Customer Success") == "Globex HOC"
