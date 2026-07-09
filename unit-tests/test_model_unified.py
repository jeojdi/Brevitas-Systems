"""Both LLMLingua-2 configs must reference the same (light) model — bert-base-multilingual —
so the API microservice and the local optimizer behave identically and load reliably."""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MODEL = "microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank"


def _model_names(path: Path):
    text = path.read_text()
    return re.findall(r'model_name="([^"]+)"', text)


def test_prompt_optimizer_uses_bert_base():
    names = _model_names(_ROOT / "token_efficiency_model/lossless/prompt_optimizer.py")
    assert names and all(n == _MODEL for n in names), names


def test_compress_service_uses_bert_base():
    names = _model_names(_ROOT / "services/compress/app.py")
    assert names and all(n == _MODEL for n in names), names


def test_no_xlm_roberta_large_left_behind():
    for rel in ("token_efficiency_model/lossless/prompt_optimizer.py",
                "services/compress/app.py"):
        assert "xlm-roberta-large" not in (_ROOT / rel).read_text()
