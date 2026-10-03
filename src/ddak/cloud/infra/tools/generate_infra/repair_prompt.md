Repair only the Terraform files responsible for the supplied validation_error. Return JSON only and
match the supplied schema. Return a strict subset of the existing files and include only files whose
contents must change. Never return every file, add or rename a filename, or rewrite unrelated files.
The validation_error value may contain the ordered history of failures separated by commas. Fix the
latest failure without reintroducing any earlier failure.

Each returned file is a complete replacement HCL file, not a diff. Represent it with `name` and
`lines`, with exactly one HCL source line per lines item and no embedded newline. Preserve all valid
resource addresses and unrelated behavior. Obey every security and resource requirement from the
original generation prompt. Never weaken IAM, network, encryption, database, secret, or approval
requirements merely to pass validation. The code merges the replacements, rejects filename set
changes, and validates the complete bundle again before plan and human approval.
