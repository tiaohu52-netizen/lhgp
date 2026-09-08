import re
import pathlib
files = [
    "tests/unit/test_context.py",
    "tests/integration/test_contract_visibility.py",
    "tests/integration/test_p5_repair_loop.py",
]
for f in files:
    p = pathlib.Path(f)
    text = p.read_text(encoding="utf-8")
    new_text = re.sub(
        r"input_, _\s*=\s*build_attempt_input\(",
        "input_, _, _ = build_attempt_input(",
        text,
    )
    new_text = re.sub(
        r"input_, _consumed\s*=\s*build_attempt_input\(",
        "input_, _consumed, _consumed_ids = build_attempt_input(",
        new_text,
    )
    new_text = re.sub(
        r"probe, _consumed\s*=\s*build_attempt_input\(",
        "probe, _consumed, _consumed_ids = build_attempt_input(",
        new_text,
    )
    if new_text != text:
        p.write_text(new_text, encoding="utf-8")
        print("updated:", f)
    else:
        print("no change:", f)
