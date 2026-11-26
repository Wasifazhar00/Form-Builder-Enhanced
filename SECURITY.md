# Security notes

This desktop app is designed to process bank form files and master data locally. It has no network client, telemetry, update checker or cloud storage integration.

- Do not add real customer data, bank forms, account numbers, CNICs, or signature files to Git.
- Treat source PDFs, templates that contain private file paths, master workbooks, exports and logs as sensitive local records.
- The sample workbook contains fictional data only.
- Review every auto-detected box before using a template operationally.
- The signature-image feature is not a cryptographic/digital signature.
- Store production files on encrypted, access-controlled disks and follow the institution's retention and deletion policies.

To report a security issue, open a private security advisory in the GitHub repository rather than placing sensitive form data in a public issue.
