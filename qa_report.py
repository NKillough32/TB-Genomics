from pypdf import PdfReader

reader = PdfReader("exports/outbreaker_investigation_report.pdf")
print(f"Pages: {len(reader.pages)}")
full_text = ""
for page in reader.pages:
    full_text += page.extract_text() or ""
print(f"Total chars extracted: {len(full_text)}")

checks = [
    ("No [Error generating interpretation]", "[Error generating interpretation" not in full_text),
    ("Manual interpretation paragraph present", "Current interpretation" in full_text),
    ("Model-prioritised wording", "model-prioritised" in full_text),
    ("Table A1 present", "Table A1" in full_text),
    ("Table A2 present", "Table A2" in full_text),
    ("High-confidence count footnote", "Counts may differ" in full_text),
    ("Valid expected gene", "Valid expected gene" in full_text),
    ("Top Actions Due Now", "Top Actions Due Now" in full_text),
    ("Excluded from operational interpretation", "Excluded from operational interpretation" in full_text),
    ("Reproducibility metadata (Random seed)", "Random seed" in full_text),
    ("Reproducibility metadata (Reference genome)", "Reference genome" in full_text),
    ("No triple caveat blocks (max 1 per type)", full_text.count("OPERATIONAL SAFETY NOTICE") <= 4),
]

print()
all_pass = True
for label, result in checks:
    status = "PASS" if result else "FAIL"
    if not result:
        all_pass = False
    print(f"  [{status}] {label}")

print()
if all_pass:
    print("ALL CHECKS PASSED")
else:
    print("SOME CHECKS FAILED - see above")

# Show snippets for failures
print("\n--- Snippets for investigation ---")
snippets = [
    ("Current interpretation", 200),
    ("model-prioritised", 200),
    ("Table A1", 100),
    ("Table A2", 100),
    ("Top Actions Due Now", 150),
    ("Random seed", 100),
]
for phrase, ctx in snippets:
    idx = full_text.find(phrase)
    if idx >= 0:
        print(f"\n>> Found '{phrase}' at char {idx}:")
        print(full_text[max(0, idx-50):idx+ctx])
    else:
        print(f"\n>> '{phrase}' NOT FOUND in PDF text")
