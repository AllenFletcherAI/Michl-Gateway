import sys
import os

# Add project root to path
sys.path.insert(0, os.path.abspath("."))

from fastapi.testclient import TestClient
from app.main import app
from app.config import settings
from app.services.audit import calculate_iso6346_check_digit, run_deterministic_audit
from app.services.reconciler import reconcile_documents
from app.services.sql_generator import (
    generate_mssql_insert,
    generate_mssql_ddl,
    generate_reconcile_mssql_ddl,
    generate_csharp_snippet
)
from app.models.schemas import (
    ExtractedDocument,
    LineItem
)

def test_configuration():
    print("[TEST 1] Configuration Settings:")
    assert settings.COMPANY_NAME == "Karl Michl Speditions GmbH", f"Bad COMPANY_NAME: {settings.COMPANY_NAME}"
    assert settings.PARTNER_NAME == "Michl Gruppe Kontraktlogistik", f"Bad PARTNER_NAME: {settings.PARTNER_NAME}"
    assert "michl_gateway.db" in settings.DATABASE_URL, f"Bad DATABASE_URL: {settings.DATABASE_URL}"
    print(f"  [OK] Company: {settings.COMPANY_NAME}")
    print(f"  [OK] Partner: {settings.PARTNER_NAME}")
    print(f"  [OK] Database URL: {settings.DATABASE_URL}")

def test_container_iso6346():
    print("\n[TEST 2] ISO 6346 Modulo-11 Checksum Validation:")
    # MSKU2264800 is valid (check digit 0)
    is_valid, digit, msg = calculate_iso6346_check_digit("MSKU2264800")
    assert is_valid is True, f"MSKU2264800 should pass: {msg}"
    print(f"  [OK] Valid container MSKU2264800 passed: digit={digit}, {msg}")

    # Corrupted check digit: MSKU2264802 (declared 2, expected 0)
    is_valid_bad, digit_bad, msg_bad = calculate_iso6346_check_digit("MSKU2264802")
    assert is_valid_bad is False, f"MSKU2264802 should fail: {msg_bad}"
    print(f"  [OK] Corrupted container MSKU2264802 caught: declared=2, calculated={digit_bad}")

def test_deterministic_audit():
    print("\n[TEST 3] Deterministic Audit Engine:")
    doc = ExtractedDocument(
        document_type="Ocean Bill of Lading",
        document_number="MCL-HAM-2026-001",
        shipper="Bavarian Precision Machinery GmbH, Augsburg",
        consignee="Nordic Industrial Supply A/S, Gothenburg",
        container_number="MSKU2264800",
        gross_weight_kg=12500.0,
        net_weight_kg=11000.0,
        total_amount=45000.0,
        currency="EUR",
        incoterm="CIF",
        line_items=[
            LineItem(item_description="Industrial Drive Units", quantity=100, unit_price=450.0, total_price=45000.0, weight_kg=11000.0)
        ]
    )
    report = run_deterministic_audit(doc)
    assert report.status == "VERIFIED_CLEAN", f"Expected VERIFIED_CLEAN: {report}"
    print(f"  [OK] Shipment audit result: {report.status} ({report.passed_checks}/{report.total_checks} checks passed, atlas_ready={report.atlas_ready})")

def test_reconciliation_and_claim_memo():
    print("\n[TEST 4] 3-Way Match Reconciliation & ADSp 2017 Claim Notice:")
    client = TestClient(app)
    resp = client.get("/api/v1/reconcile-samples")
    assert resp.status_code == 200, f"Failed to get samples: {resp.text}"
    recon_result = resp.json()
    print(f"  [OK] Triad reconciliation status: {recon_result['overall_status']}")
    print(f"  [OK] Prevented Demurrage: EUR {recon_result['demurrage_prevented_eur']}")
    notice = recon_result.get("discrepancy_notice_text")
    if notice:
        assert "Karl Michl Speditions GmbH" in notice or "Michl Gruppe" in notice, f"Missing Michl in notice: {notice}"
        assert "ADSp 2017" in notice, f"Missing ADSp 2017 in notice: {notice}"
        print("  [OK] ADSp 2017 Claim notice correctly branded for Karl Michl Speditions GmbH")
    else:
        print("  [OK] Reconciliation clean or no discrepancies flagged in sample")

def test_sql_generation():
    print("\n[TEST 5] T-SQL Staging Scripts & C# SDK Snippets:")
    doc = ExtractedDocument(
        document_type="Ocean Bill of Lading",
        document_number="MCL-HAM-2026-999",
        container_number="MSKU2264800",
        gross_weight_kg=14000.0,
        net_weight_kg=12800.0,
        total_amount=60000.0,
        currency="EUR"
    )
    audit = run_deterministic_audit(doc)
    tsql = generate_mssql_insert("MCL-2026-EXP-TEST01", doc, audit)
    assert "[dbo].[Michl_Staging_Shipments]" in tsql, f"Missing Michl_Staging_Shipments in T-SQL: {tsql}"
    print("  [OK] T-SQL INSERT targets [dbo].[Michl_Staging_Shipments]")

    ddl = generate_mssql_ddl()
    assert "Michl_Staging_Shipments" in ddl, "Missing Michl_Staging_Shipments in DDL"
    print("  [OK] DDL creates table dbo.Michl_Staging_Shipments")

    recon_ddl = generate_reconcile_mssql_ddl()
    assert "Michl_Staging_ReconciliationTriad" in recon_ddl, "Missing Michl_Staging_ReconciliationTriad in DDL"
    print("  [OK] DDL creates table dbo.Michl_Staging_ReconciliationTriad")

    cs = generate_csharp_snippet("http://localhost:8000")
    assert "Michl" in cs, "Missing Michl in C# snippet"
    print("  [OK] C# SDK Driver is configured for Michl Logistics Gateway")

def test_favicon_suite():
    print("\n[TEST 6] Favicon Suite & Brand Assets:")
    static_dir = os.path.join("app", "static")
    required_files = [
        "favicon.ico",
        "favicon.svg",
        "favicon-16x16.png",
        "favicon-32x32.png",
        "favicon-512x512.png",
        "apple-touch-icon.png",
        os.path.join("img", "michl_icon.png")
    ]
    for rf in required_files:
        path = os.path.join(static_dir, rf)
        assert os.path.exists(path), f"Missing icon asset: {path}"
        size = os.path.getsize(path)
        assert size > 100, f"Asset too small / corrupted: {path} ({size} bytes)"
        print(f"  [OK] Verified asset {rf} ({size} bytes)")

def test_http_endpoints():
    print("\n[TEST 7] HTTP Endpoints (GET and HEAD methods):")
    client = TestClient(app)
    
    # 1. /health
    r_health_get = client.get("/health")
    assert r_health_get.status_code == 200, f"Health GET failed: {r_health_get.status_code}"
    assert r_health_get.json()["service"] == "Michl Gateway"
    print("  [OK] GET /health -> 200 OK")

    r_health_head = client.head("/health")
    assert r_health_head.status_code == 200, f"Health HEAD failed: {r_health_head.status_code}"
    print("  [OK] HEAD /health -> 200 OK")

    # 2. /
    r_root_get = client.get("/")
    assert r_root_get.status_code == 200, f"Root GET failed: {r_root_get.status_code}"
    assert "Michl Gateway" in r_root_get.text
    print("  [OK] GET / -> 200 OK")

    r_root_head = client.head("/")
    assert r_root_head.status_code == 200, f"Root HEAD failed: {r_root_head.status_code}"
    print("  [OK] HEAD / -> 200 OK")

    # 3. /favicon.ico
    r_fav_get = client.get("/favicon.ico")
    assert r_fav_get.status_code == 200, f"Favicon GET failed: {r_fav_get.status_code}"
    assert len(r_fav_get.content) > 1000
    print("  [OK] GET /favicon.ico -> 200 OK")

    r_fav_head = client.head("/favicon.ico")
    assert r_fav_head.status_code == 200, f"Favicon HEAD failed: {r_fav_head.status_code}"
    print("  [OK] HEAD /favicon.ico -> 200 OK")

def main():
    print("=" * 65)
    print("  MICHL GATEWAY - INTEGRATED COMPLIANCE & BRAND VALIDATION")
    print("=" * 65)
    test_configuration()
    test_container_iso6346()
    test_deterministic_audit()
    test_reconciliation_and_claim_memo()
    test_sql_generation()
    test_favicon_suite()
    test_http_endpoints()
    print("\n" + "=" * 65)
    print("  ALL 7 TEST SUITES PASSED WITH ZERO ERRORS (100% CLEAN)")
    print("=" * 65)

if __name__ == "__main__":
    main()
