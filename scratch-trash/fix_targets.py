import pathlib
p = pathlib.Path("tests/unit/test_plan_signoff_principal.py")
text = p.read_text(encoding="utf-8")
new_text = text.replace('"target": "/tmp/x"', '"target": "build/c1"').replace(
    '"target": "/tmp/c1"', '"target": "build/c1"'
)
p.write_text(new_text, encoding="utf-8")
print("done", new_text != text)
