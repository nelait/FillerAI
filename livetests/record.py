"""Re-record the fixtures the offline tests replay.

``tests/fixtures/llm/*.json`` is what makes a non-deterministic feature
testable in a deterministic suite. The fixtures shipped with phase 1 were
written **by hand**, because no API key was available when the feature was
built; they exercise every branch of the validation gate and they are not
evidence about what a model actually proposes.

This script replaces one with a real exchange::

    FILLERAI_LLM_KEY=sk-... python livetests/record.py examples/member_enrollment.fields.json
    OPENAI_API_KEY=sk-... python livetests/record.py examples/member_enrollment.fields.json

Each provider gets its own file, because each answers in its own shape and
``tests/test_llm_providers.py`` asserts the two reach the same verdicts.

The recording is keyed on a digest of the request, so the offline test will
fail loudly the next time somebody edits a prompt - which is the behaviour
that makes the fixture worth having. Re-run this, read the diff, and commit
the new recording like any other test data.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fillerai
from fillerai.llm import prompts, providers
from fillerai.llm.client import Client
from fillerai.llm.config import Settings
from fillerai.llm.transport import digest

FIXTURES = ROOT / "tests" / "fixtures" / "llm"


def record(form: Path, provider: str | None = None) -> Path:
    schema = (fillerai.extract_html(form) if form.suffix.lower() in (".html", ".htm")
              else fillerai.extract_spec(form))
    settings = Settings.resolve("rules", provider=provider)
    client = Client(settings)

    request = client.build(
        system=prompts.RULES_SYSTEM,
        user=prompts.rules_user(schema),
        output_schema=prompts.RULES_OUTPUT_SCHEMA,
    )
    print(f"asking {settings.api.label}'s {settings.model} about {schema.name} "
          f"({len(schema.fields)} fields)…", file=sys.stderr)
    response = client.transport.send(request)

    # The default provider keeps the plain name the tests already look for;
    # anything else is suffixed, so recording one never clobbers the other.
    suffix = "" if settings.provider == providers.DEFAULT else f".{settings.provider}"
    out = FIXTURES / f"rules_{schema.name}{suffix}.json"
    out.write_text(json.dumps({
        "_note": (
            f"Recorded from the real {settings.api.label} API. Re-record with "
            "livetests/record.py when a prompt in fillerai/llm/prompts.py changes."
        ),
        "form": schema.name,
        "provider": settings.provider,
        "model": settings.model,
        "recordings": [{"digest": digest(request), "response": response}],
    }, indent=2) + "\n", encoding="utf-8")
    return out


if __name__ == "__main__":
    if not 2 <= len(sys.argv) <= 3:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    chosen = sys.argv[2] if len(sys.argv) == 3 else None
    print(f"wrote {record(Path(sys.argv[1]), chosen)}", file=sys.stderr)
