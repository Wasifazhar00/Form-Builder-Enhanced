from pathlib import Path

import json

from formbuilder.template import build_template, load_template, make_field, save_template

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def test_sample_template_is_portable_and_contains_two_pages() -> None:
    payload = json.loads((SAMPLES / "sample_template.json").read_text(encoding="utf-8"))
    assert payload["template_version"] == 1
    assert payload["source_path"] == "sample_application_form.pdf"
    assert len(payload["page_sizes_pt"]) == 2
    assert {field["type"] for field in payload["fields"]} >= {"text", "checkbox", "signature"}
    loaded = load_template(SAMPLES / "sample_template.json")
    assert Path(loaded["source_path"]).resolve() == (SAMPLES / "sample_application_form.pdf").resolve()


def test_save_and_load_template_validates_source_hash(tmp_path: Path) -> None:
    source = SAMPLES / "sample_application_form.pdf"
    target = tmp_path / "template.json"
    field = make_field(page=0, rect=[10, 10, 100, 30], label="Customer name", data_key="customer_name")
    save_template(target, source, [field])
    loaded = load_template(target)
    assert loaded["fields"][0]["data_key"] == "customer_name"
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["source_sha256"] == build_template(source, [field])["source_sha256"]


def test_template_rejects_modified_source(tmp_path: Path) -> None:
    from shutil import copyfile
    from formbuilder.template import load_template
    import pytest

    source = tmp_path / "copy.pdf"
    copyfile(SAMPLES / "sample_application_form.pdf", source)
    target = tmp_path / "template.json"
    save_template(target, source, [])
    source.write_bytes(source.read_bytes() + b"\nchanged")
    with pytest.raises(ValueError, match="SHA-256"):
        load_template(target)
