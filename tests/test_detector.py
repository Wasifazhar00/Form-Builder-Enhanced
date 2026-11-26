from pathlib import Path

from formbuilder.detector import detect_fields


def test_text_layer_detector_proposes_reviewable_boxes_with_confidence() -> None:
    sample = Path(__file__).resolve().parents[1] / "samples" / "sample_application_form.pdf"
    fields, messages = detect_fields(sample)
    assert fields
    assert any(field["data_key"] == "customer_name" for field in fields)
    assert all(0.0 <= float(field["confidence"]) <= 1.0 for field in fields)
    assert any(field["detection_source"] == "PDF text layer" for field in fields)
    assert any("Review every proposed field" in message for message in messages)
    # Footer text mentioning a signature must not be mistaken for a signature field.
    assert not any(field["page"] == 1 and field["rect"][1] > 700 for field in fields)
