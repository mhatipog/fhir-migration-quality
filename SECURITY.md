# Security policy

Please do not open public issues containing patient data, credentials, API tokens,
private endpoints, hospital names, or exploitable details of a potential security bug.

Report sensitive vulnerabilities privately using the repository's GitHub Security
Advisory / "Report a vulnerability" feature when enabled. If not enabled, contact
the repository owner using a private channel before publishing details.

Only completely synthetic test fixtures may be included in pull requests.
Avoid attaching real-world medical payloads even after removing names: clinical
combinations and timestamps may remain identifying.

The project is a data quality / migration demonstration, not an approved
healthcare production security boundary. Operators must perform independent
security, privacy, safety and clinical validation before deployment.
