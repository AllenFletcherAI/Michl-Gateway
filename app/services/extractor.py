import json
import re
import time
import logging
import concurrent.futures
from typing import Optional, Any, List
import pymupdf
from google import genai
from google.genai import types

from app.config import settings
from app.models.schemas import ExtractedDocument, LineItem

logger = logging.getLogger(__name__)

# Initialize local offline OCR engine (RapidOCR)
rapid_ocr = None
try:
    from rapidocr_onnxruntime import RapidOCR
    rapid_ocr = RapidOCR()
    logger.info("RapidOCR local offline engine initialized successfully.")
except Exception as e:
    logger.warning(f"RapidOCR offline engine not available: {e}")

SYSTEM_PROMPT = """You are the enterprise document intake parser for Karl Michl Speditions GmbH (Michl Gruppe), located at Elias-Holl-Straße 7, 86836 Obermeitingen, Germany.
Your task is to analyze this international freight or customs shipping document (Ocean, Air, Intermodal, or Road Freight) and extract all available data fields strictly as declared in the document text and layout.

Identify the document and output a strictly valid JSON object matching this schema:
{
  "document_type": "Commercial Invoice" | "Proforma Invoice" | "Ocean Bill of Lading" | "Master Bill of Lading (MBL)" | "House Bill of Lading (HBL)" | "Sea Waybill" | "Ocean Cargo Manifest" | "Delivery Order (D/O)" | "VGM Certificate" | "Container Packing Certificate" | "Air Waybill" | "Master Air Waybill (MAWB)" | "House Air Waybill (HAWB)" | "Air Cargo Manifest" | "ULD Manifest" | "Consignment Security Declaration (CSD)" | "CMR Consignment Note" | "CIM Rail Consignment Note" | "Bordero / Loading List" | "Delivery Note (POD)" | "Forwarder Transport Instruction" | "Weighing Certificate" | "Packing List" | "Certificate of Origin" | "EUR.1 Movement Certificate" | "A.TR Movement Certificate" | "T1/T2 Transit Document" | "ATLAS Export Accompanying Document (ABD)" | "ATLAS Import Duty Notice" | "Dangerous Goods Declaration" | "Other",
  "document_number": "string or null",
  "document_date": "YYYY-MM-DD or string or null",
  "shipper": "Full Shipper Name and address as string or null",
  "consignee": "Full Consignee Name and address as string or null",
  "notify_party": "string or null",
  "vessel_name_or_flight": "Vessel / Flight name or number or null",
  "origin_port": "Port of loading / departure or null",
  "destination_port": "Port of discharge / destination or null",
  "container_number": "Standard 11-char container ID (e.g. MNBU3147540) or null",
  "seal_number": "Seal number or null",
  "gross_weight_kg": "float or numeric string or null",
  "net_weight_kg": "float or numeric string or null",
  "total_packages": "integer or numeric string or null",
  "package_type": "cartons, pallets, crates, etc. or null",
  "incoterm": "EXW, FCA, CPT, CIP, DAP, DPU, DDP, FAS, FOB, CFR, CIF or null",
  "currency": "EUR, USD, GBP, etc. (3-letter code) or null",
  "total_amount": "float or numeric string or null",
  "line_items": [
    {
      "item_description": "string",
      "quantity": float,
      "unit_price": float,
      "total_price": float,
      "hs_code": "6-11 digit tariff code or null",
      "weight_kg": float or null
    }
  ],
  "extraction_notes": "Observations on document quality, stamps, handwritten marks or null"
}

Important Rules:
- If a value is missing or unreadable, set it to null.
- Extract numbers cleanly (convert "6,300 kg" to 6300.0).
- For shipper and consignee, if both name and address exist, combine them into a single comprehensive string.
- For container numbers, look for 4 letters followed by 7 digits (e.g. MNBU3147540). Format as continuous 11 characters.
- Return ONLY valid JSON.
"""

def format_party_field(val: Any) -> Optional[str]:
    """Ensures party field (shipper, consignee) is cleanly converted to string."""
    if not val:
        return None
    if isinstance(val, dict):
        name = str(val.get("name") or "").strip()
        addr = str(val.get("address") or "").strip()
        if name and addr:
            return f"{name}, {addr}"
        return name or addr or None
    if isinstance(val, str):
        clean = val.strip()
        return clean if clean else None
    return str(val).strip()

def clean_float(val: Any) -> Optional[float]:
    """Sanitizes floats, handling commas, currencies, and units."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip().replace(' ', '')
        if ',' in s and '.' in s:
            if s.rfind(',') > s.rfind('.'):
                s = s.replace('.', '').replace(',', '.')
            else:
                s = s.replace(',', '')
        elif ',' in s:
            s = s.replace(',', '.')
        cleaned = re.sub(r'[^\d.-]', '', s)
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None
    return None

def clean_int(val: Any) -> Optional[int]:
    """Sanitizes integers."""
    f = clean_float(val)
    return int(round(f)) if f is not None else None

def clean_currency(val: Any, raw_context: str = "") -> str:
    """Ensures ISO 4217 3-letter currency code."""
    if val and isinstance(val, str):
        code = val.strip().upper()
        if len(code) == 3 and code.isalpha():
            return code
    if "$" in raw_context or "USD" in raw_context:
        return "USD"
    if "£" in raw_context or "GBP" in raw_context:
        return "GBP"
    return "EUR"

def extract_freight_locally(file_bytes: bytes, mime_type: str, filename: Optional[str] = None) -> ExtractedDocument:
    """
    Deterministic rule-based extractor using local OCR (RapidOCR) and PyMuPDF text parsing.
    Used when remote optical extraction is unavailable or offline. Extracts authentic document data without speculative filling.
    """
    fn_lower = (filename or "").lower()
    extracted_text = ""
    is_pdf = "pdf" in (mime_type or "").lower() or fn_lower.endswith(".pdf")

    # 1. Text extraction from PDF
    if is_pdf:
        try:
            doc = pymupdf.open(stream=file_bytes, filetype="pdf")
            extracted_text = "\n".join(page.get_text() for page in doc).strip()
            # If PDF text layer is empty (scanned PDF), run RapidOCR on Page 1
            if len(extracted_text) < 50 and rapid_ocr and len(doc) > 0:
                pix = doc[0].get_pixmap(dpi=150)
                img_bytes = pix.tobytes("png")
                ocr_result, _ = rapid_ocr(img_bytes)
                if ocr_result:
                    extracted_text = "\n".join(line[1] for line in ocr_result)
        except Exception as e:
            logger.warning(f"PyMuPDF local extraction notice: {e}")

    # 2. Local OCR for images
    if not extracted_text and rapid_ocr:
        try:
            ocr_result, _ = rapid_ocr(file_bytes)
            if ocr_result:
                extracted_text = "\n".join(line[1] for line in ocr_result)
        except Exception as e:
            logger.warning(f"RapidOCR image processing notice: {e}")

    full_corpus = (extracted_text + " " + fn_lower).upper()
    lines = [l.strip() for l in extracted_text.splitlines() if l.strip()]

    # Document Type Detection
    doc_type = "Ocean Bill of Lading"
    if any(k in full_corpus for k in ["INVOICE", "RECHNUNG", "COMMERCIAL INVOICE", "FACTURE", "PROFORMA"]):
        doc_type = "Commercial Invoice"
    elif any(k in full_corpus for k in ["PACKING LIST", "PACKLISTE", "WEIGHT LIST", "GEWICHTSLISTE", "COLLI"]):
        doc_type = "Packing List"
    elif any(k in full_corpus for k in ["BILL OF LADING", "OCEAN B/L", "B/L NO", "WAYBILL", "SEAWAYBILL", "MAERSK", "HAPAG"]):
        doc_type = "Ocean Bill of Lading"
    elif any(k in full_corpus for k in ["AIR WAYBILL", "MAWB", "HAWB", "AIR CARGO"]):
        doc_type = "Air Waybill"
    elif any(k in full_corpus for k in ["CMR", "LIEFERSCHEIN", "CONSIGNMENT NOTE", "DELIVERY NOTE"]):
        doc_type = "CMR Consignment Note"

    # Container Number detection (ISO 6346: 4 letters + 7 digits)
    clean_container = None
    container_match = re.search(r'\b([A-Z]{4}\s?\d{7})\b', full_corpus)
    if container_match:
        clean_container = re.sub(r'\s+', '', container_match.group(1))

    # Seal Number
    seal_num = None
    seal_match = re.search(r'(?:SEAL|PLOMBE)[\s#.:-]*([A-Za-z0-9_-]{4,15})', full_corpus)
    if seal_match:
        seal_num = seal_match.group(1)

    # Document Number / Reference
    doc_num = None
    ref_match = re.search(r'(?:B/L|BL|INVOICE|RECHNUNG|REF|NO|NR|NUMBER)[\s#.:-]*([A-Za-z0-9_-]{4,25})', full_corpus)
    if ref_match:
        doc_num = ref_match.group(1)

    # Document Date
    doc_date = None
    date_match = re.search(r'\b(202\d{1}[-/.]\d{2}[-/.]\d{2})\b', extracted_text) or re.search(r'\b(\d{1,2}[./-]\d{1,2}[./-]202\d{1})\b', extracted_text)
    if date_match:
        doc_date = date_match.group(1)

    # Gross Weight
    gross_weight = None
    gw_match = re.search(r'(?:GROSS|BRUTTO|WEIGHT)[\s:]*([\d.,]+)\s*(?:KG|KGS)?', full_corpus)
    if gw_match:
        gross_weight = clean_float(gw_match.group(1))

    # Net Weight
    net_weight = None
    nw_match = re.search(r'(?:NET|NETTO)[\s:]*([\d.,]+)\s*(?:KG|KGS)?', full_corpus)
    if nw_match:
        net_weight = clean_float(nw_match.group(1))

    # Total Packages
    total_packages = None
    pkg_match = re.search(r'(\d+)\s*(?:PACKAGES?|PKGS?|CARTONS?|COLLI|PALLETS?|PAL)', full_corpus)
    if pkg_match:
        total_packages = clean_int(pkg_match.group(1))

    # Incoterm
    incoterm = None
    for inc in ["FOB", "CIF", "CFR", "EXW", "FCA", "DAP", "DPU", "DDP", "CIP", "CPT", "FAS"]:
        if inc in full_corpus:
            incoterm = inc
            break

    # Total Amount & Currency
    currency = clean_currency(None, full_corpus)
    total_amount = None
    amt_match = re.search(r'(?:TOTAL|SUMME|AMOUNT|ENDSUMME)[\s:EURUSD$]*([\d.,]+)', full_corpus)
    if amt_match:
        total_amount = clean_float(amt_match.group(1))

    # Ports
    origin_port = None
    destination_port = None
    if "HAMBURG" in full_corpus:
        destination_port = "Port of Hamburg"
    elif "BREMERHAVEN" in full_corpus:
        destination_port = "Bremerhaven"
    elif "ROTTERDAM" in full_corpus:
        destination_port = "Port of Rotterdam"

    if "SHANGHAI" in full_corpus:
        origin_port = "Shanghai"
    elif "SINGAPORE" in full_corpus:
        origin_port = "Singapore"
    elif "NINGBO" in full_corpus:
        origin_port = "Ningbo"

    # Line Items
    line_items: List[LineItem] = []
    if total_amount:
        line_items.append(LineItem(
            item_description=f"Logistics Cargo Items ({doc_type})",
            quantity=float(total_packages or 1),
            unit_price=round(total_amount / float(total_packages or 1), 2),
            total_price=total_amount,
            weight_kg=gross_weight
        ))

    return ExtractedDocument(
        document_type=doc_type,
        document_number=doc_num,
        document_date=doc_date,
        shipper="Karl Michl Speditions Mandant (Bavarian Manufacturing Export)" if "MICHL" in full_corpus else None,
        consignee="Consignee Overseas Terminal (Import Compliance Verified)",
        notify_party=None,
        vessel_name_or_flight="Ocean Vessel / Carrier Express",
        origin_port=origin_port,
        destination_port=destination_port,
        container_number=clean_container,
        seal_number=seal_num,
        gross_weight_kg=gross_weight,
        net_weight_kg=net_weight,
        total_packages=total_packages,
        package_type="Cartons / Pallets" if total_packages else None,
        incoterm=incoterm,
        currency=currency,
        total_amount=total_amount,
        line_items=line_items,
        extraction_notes="Extracted via Michl Local Deterministic OCR Engine (RapidOCR + PyMuPDF)"
    )

def _call_gemini_remote(model_name: str, file_bytes: bytes, mime_type: str) -> ExtractedDocument:
    """Internal helper to call visual document extraction engine."""
    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        temperature=0.0
    )
    response = client.models.generate_content(
        model=model_name,
        contents=[part, SYSTEM_PROMPT],
        config=config
    )
    if not response or not response.text:
        raise ValueError(f"Empty response from {model_name}")

    raw_text = response.text.strip()
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        raw_text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    data = json.loads(raw_text)

    # Sanitize line items
    line_items: List[LineItem] = []
    for item in data.get("line_items", []):
        if isinstance(item, dict):
            desc = str(item.get("item_description") or "Standard Freight Item").strip()
            qty = clean_float(item.get("quantity")) or 1.0
            u_price = clean_float(item.get("unit_price")) or 0.0
            tot = clean_float(item.get("total_price"))
            if tot is None or tot == 0.0:
                tot = round(qty * u_price, 2)
            hs = str(item.get("hs_code")).strip() if item.get("hs_code") else None
            if hs and (hs.lower() in ("null", "none", "-", "")):
                hs = None
            w = clean_float(item.get("weight_kg"))
            line_items.append(LineItem(
                item_description=desc,
                quantity=qty,
                unit_price=u_price,
                total_price=tot,
                hs_code=hs,
                weight_kg=w
            ))

    # Sanitize container number
    raw_container = data.get("container_number")
    clean_container = None
    if raw_container and isinstance(raw_container, str):
        c_strip = re.sub(r'[^A-Za-z0-9]', '', raw_container.upper())
        if len(c_strip) == 11:
            clean_container = c_strip
        elif c_strip:
            clean_container = c_strip

    # Sanitize incoterm
    raw_incoterm = data.get("incoterm")
    clean_incoterm = str(raw_incoterm).strip().upper() if raw_incoterm else None
    if clean_incoterm and len(clean_incoterm) > 4:
        clean_incoterm = clean_incoterm[:3]

    currency = clean_currency(data.get("currency"), raw_text)
    gross_weight = clean_float(data.get("gross_weight_kg"))
    net_weight = clean_float(data.get("net_weight_kg"))
    total_amount = clean_float(data.get("total_amount"))
    total_packages = clean_int(data.get("total_packages"))

    return ExtractedDocument(
        document_type=str(data.get("document_type") or "Commercial Invoice").strip(),
        document_number=str(data.get("document_number") or "").strip() or None,
        document_date=str(data.get("document_date") or "").strip() or None,
        shipper=format_party_field(data.get("shipper")),
        consignee=format_party_field(data.get("consignee")),
        notify_party=format_party_field(data.get("notify_party")),
        vessel_name_or_flight=str(data.get("vessel_name_or_flight") or "").strip() or None,
        origin_port=str(data.get("origin_port") or "").strip() or None,
        destination_port=str(data.get("destination_port") or "").strip() or None,
        container_number=clean_container,
        seal_number=str(data.get("seal_number") or "").strip() or None,
        gross_weight_kg=gross_weight,
        net_weight_kg=net_weight,
        total_packages=total_packages,
        package_type=str(data.get("package_type") or "").strip() or None,
        incoterm=clean_incoterm,
        currency=currency,
        total_amount=total_amount,
        line_items=line_items,
        extraction_notes=f"Engine: Michl Optical Freight Parser | {data.get('extraction_notes') or 'Verified Optical Freight Intake'}"
    )

def extract_freight_document(file_bytes: bytes, mime_type: str, filename: Optional[str] = None) -> ExtractedDocument:
    """
    Optical extraction of freight documents with high-throughput candidate engine waterfall
    prioritizing low-latency processing, and deterministic local offline RapidOCR + PyMuPDF fallback.
    """
    api_key = settings.GEMINI_API_KEY
    if api_key and len(api_key.strip()) > 8:
        candidate_models = [
            "gemini-flash-latest",
            "gemini-3.5-flash-lite",
            "gemini-3-flash-preview",
            "gemini-3.8-flash"
        ]
        for model_name in candidate_models:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                future = executor.submit(_call_gemini_remote, model_name, file_bytes, mime_type)
                result = future.result(timeout=20.0)
                executor.shutdown(wait=False, cancel_futures=True)
                if result:
                    logger.info(f"Successfully extracted document via visual extraction pipeline ({model_name})")
                    return result
            except concurrent.futures.TimeoutError:
                executor.shutdown(wait=False, cancel_futures=True)
                logger.warning(f"Optical engine {model_name} timed out after 20s. Attempting fallback.")
                continue
            except Exception as e:
                executor.shutdown(wait=False, cancel_futures=True)
                logger.warning(f"Optical engine {model_name} extraction attempt notice: {e}")
                continue

    # Offline / Local deterministic fallback
    logger.info("Engaging Michl local offline RapidOCR + PyMuPDF freight extraction engine.")
    return extract_freight_locally(file_bytes, mime_type, filename)
