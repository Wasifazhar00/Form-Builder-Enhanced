# Bank Form Automation — Windows desktop app

A local-only Windows desktop tool for placing data fields over original bank PDFs or scanned form images, mapping those fields to an Excel/CSV master sheet, then exporting flattened PDFs or a ZIP batch.

**The source document is never edited.** The app reads the source bytes, renders them for the editor, and writes a separate output PDF. It does not upload documents or customer data, call a cloud API, or include analytics/telemetry.

## Features

- **Visual template editor:** open PDF or image forms, preview every page, add text/checkbox/signature boxes, then drag and resize each box on the page.
- **Field mapping:** map boxes to columns in a local Excel/CSV sheet. Save the page, coordinates, formatting and field mapping to a JSON template.
- **Assisted detection:** import existing AcroForm fields, ask AcroForge for vector-geometry suggestions, and use PyMuPDF's text layer / optional local Tesseract OCR for common bank labels. Each suggestion shows a confidence score and remains manually movable/resizable.
- **Fill pipeline:** select a customer row and one or more templates, generate flattened PDFs, and preview output before printing.
- **Batch mode:** create one PDF per customer/template and package all PDFs into a ZIP.
- **Banking field types:** text, checkboxes, dates, currency formatting and local signature-image placement. Handles multiple pages, alignment, shrink-to-fit and optional wrapping. Signature images are visual overlays, not cryptographic/digital signatures.
- **Existing interactive PDFs:** source pages that already contain AcroForm widgets are rendered at 300 DPI into the output first, preserving their visible state while removing interactive widgets. Standard flat/vector PDFs remain vector-based.
- **Print control:** reminds the operator to print at 100% / Actual Size and disable page scaling.

## Technology

- Python 3.12 on the Windows build runner
- PySide6 for the native desktop UI and drag/resize editor
- PyMuPDF for source rendering, text extraction, image-to-PDF conversion and transparent page-content overlays
- AcroForge for an in-memory AcroForm build → fill → flatten validation pass
- pandas + openpyxl for local master-data sheets

### Why the final overlay uses PyMuPDF

AcroForge's generated text widgets have a visible widget border. That border can change the appearance of fixed bank forms. The app therefore runs AcroForge's field injection, fill and flatten pipeline as a validation pass, then writes the same values as transparent text/line/signature content over a fresh in-memory copy of the original pages. This preserves the original form artwork and page dimensions while keeping the final PDF flat (no active form widgets).

AcroForge's geometry detector is heuristic and vector-only. Scanned/image-only forms need OCR to detect labels; the app uses local Tesseract through PyMuPDF when installed. Detection is only a starting point: inspect every box and correct its position before saving a production template.

## Run from source

1. Install Python 3.11 or newer (Python 3.12 recommended).
2. Open PowerShell in this folder.
3. Create and activate a virtual environment:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```

4. Launch:

   ```powershell
   python app.py
   ```

## Download the Windows build

Every push to `main`, pull request to `main`, tag matching `v*`, or manual `workflow_dispatch` runs `.github/workflows/build.yml` on `windows-latest`. The job compiles the source, runs tests and creates `FormBuilder.exe` with PyInstaller. Open the run in GitHub → **Actions** → **Windows build and tests** → **Artifacts** to download the executable. A version tag such as `v0.2.0` also publishes the executable in a GitHub Release.

## Add a new bank form

1. Keep the original PDF/image in a controlled local folder. Do not overwrite it. For repeat use, keep the file at a stable path so its saved template can find it.
2. Open the app and choose **Open Form**. Navigate through pages with the page selector.
3. Choose **Auto-detect** to propose boxes. Review each box and its confidence. Detection can miss fields or suggest false positives; it is not a substitute for visual verification.
4. Choose **Add Text**, **Add Checkbox** or **Add Signature** to create a manual box. Drag the box to the field and drag its lower-right handle to resize it.
5. Select the box in the **Fields** list. Set its label, master-data column, type, font size, minimum font size, alignment and formatting. For dates, use a Python date format such as `%d-%m-%Y`; for currency, set the prefix such as `Rs. `.
6. Load the local master-data Excel/CSV file. Each row represents one customer. Map every field to the correct column. Store account numbers, CNICs, phone numbers and other identifiers as **Text** in Excel so leading zeroes are preserved. For a signature field, select a local PNG/JPG file or map a column containing a local signature-image path.
7. Save the template as JSON. The template records the original source fingerprint and page coordinates; it does not contain customer rows or copy the source PDF into the repository.
8. Select a customer row and one or more saved templates, then choose **Generate Selected Row**. Review the PDF preview. For all rows, choose **Batch ZIP**.
9. Print using **Actual Size / 100%**. Turn off **Fit to Page**, **Shrink oversized pages** and similar scaling options.

## Template format

A template JSON contains the source path and SHA-256 fingerprint, original page sizes, plus an array of field objects. Coordinates are PDF points in PyMuPDF's top-left coordinate system: `[x0, y0, x1, y1]`. Each field stores its page number, type, mapped column, font size, alignment, format, wrap setting and detection confidence.

The example `samples/sample_template.json` is connected to `samples/sample_application_form.pdf` by a relative path. Relative source paths are resolved against the template's folder, so the sample pair remains portable when copied together.

## Sample files

- `samples/sample_application_form.pdf` — fictional demonstration form; not an actual bank document.
- `samples/sample_master.xlsx` — synthetic customer data only; do not use it as real customer information.
- `samples/sample_template.json` — example coordinate and mapping file.

## Local OCR (optional)

Install the Tesseract OCR executable locally and ensure `tesseract.exe` is on the Windows `PATH`. The app calls OCR through PyMuPDF only when the PDF page has no text layer. The OCR path is offline; language data is stored on the local machine. Without Tesseract, the app still supports AcroForm/vector/text detection and manual field placement.

## Data handling and safety

- Load only master-data files stored on the local computer. Supported formats are `.xlsx`, `.xlsm` and `.csv`.
- Do not commit private forms, customer workbooks, generated PDFs or signature images. The `.gitignore` excludes common local data/output folders and spreadsheets except the synthetic sample workbook.
- Use Windows disk encryption and access controls for production banking data. Delete generated output and local source copies according to your bank's retention policy.
- The app does not digitally sign documents; a signature image is a visual image overlay only.
- Always compare the preview with the source form and print one test page at Actual Size before processing a large batch.

## Tests

```powershell
python -m pip install pytest
python -m pytest -q
```
